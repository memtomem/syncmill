"""Before/after for #117: one lock span per share vs the two-call path.

Runs the REAL ``CouncilStrategy`` (fake agents, so no provider CLI is invoked)
over a REAL ``SyncmillMemory`` + ``MemtomemBackend`` in a temporary store, and
times every ``share_result`` the council issues between its fan-outs. Each
measurement is made twice on identical state — once with the backend's
``single_span_share`` capability switched off (today's ``add_returning_ids`` +
per-chunk ``share_chunk``), once with it on (``add_and_share``) — alternating
which goes first so warm-up and day-file growth cannot favour either side.

Scenarios, chosen because a share's cost is dominated by ``index_file``, which
re-chunks the WHOLE target day file on every call:

* ``fresh``    — empty store; the floor.
* ``busy-day`` — today's ``shared`` day file already holds ``--preload``
  entries, as on a store that many runs have written to today.
* ``contended`` — a second process writes one note to ``shared`` every
  ``--writer-interval`` seconds for the whole run (another run / board loop
  on the same store), so lock acquisitions contend and pay memtomem's 50 ms+
  poll backoff. Its write count is reported: a span that held ``shared``
  longer would show up as fewer background writes.
* ``two-councils`` — a second council, in its own process and the same mode,
  runs against the same store at the same time; the symmetric case.

A tight-loop background writer is deliberately NOT a scenario: it re-takes
the lock within a millisecond of releasing it, so any poller with memtomem's
backoff is starved for a random length of time and the result measures luck.

Usage (all state below a fresh temp dir; nothing touches ``~/.memtomem/``'s DB):

    uv run python scripts/bench_share_span.py [--repeats 5] [--preload 400]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeAgent, FakeWorktreePool  # noqa: E402

from syncmill.config import CouncilConfig, MemoryConfig, SyncmillConfig  # noqa: E402
from syncmill.memory import SyncmillMemory, run_async  # noqa: E402
from syncmill.models import Task  # noqa: E402
from syncmill.strategies.council import CouncilStrategy  # noqa: E402

# A realistic agent reply: ~1000 chars with markdown sections, which memtomem
# splits into one chunk per section — so each share copies several chunks.
_OUTPUT = """Proposal from {agent}, pass {n}.

## Approach
Refactor the parser into a tokenizer and a recursive-descent core so error
recovery can resynchronise on statement boundaries instead of aborting.

## Risks
The public `parse()` signature must not change; downstream callers pin it.
Error messages are snapshot-tested, so their wording has to survive.

## Plan
1. Extract the tokenizer behind the existing interface.
2. Port the grammar rules one production at a time, keeping tests green.
3. Add recovery points after `;` and `}}`.

## Summary
Low risk if done incrementally; the snapshot tests are the safety net.
"""

_WRITER = r"""
import sys, time
sys.path.insert(0, sys.argv[2])
from syncmill.config import MemoryConfig
from syncmill.memory import run_async
from syncmill.memory_backend import MemtomemBackend
root = sys.argv[1]
b = MemtomemBackend(MemoryConfig(db_path=root + "/mm.db", memory_dir=root + "/mem"))
run_async(b.ensure_ready())
open(root + "/writer.ready", "w").close()
interval = float(sys.argv[3])
i = 0
import os
while not os.path.exists(root + "/writer.stop"):
    run_async(b.add(f"**Note**\n\nbackground note {i}", tags=["note"], namespace="shared"))
    i += 1
    time.sleep(interval)
print(i)
run_async(b.close())
"""


class _ScriptedAgent(FakeAgent):
    """A FakeAgent whose output changes per call (the council's phases differ)."""

    def __init__(self, agent_id: str):
        super().__init__(agent_id)
        self._n = 0

    async def aexecute(self, prompt, workdir, timeout):
        self._n += 1
        self._output = _OUTPUT.format(agent=self._agent_id, n=self._n)
        return await super().aexecute(prompt, workdir, timeout)


def _memory(root: Path) -> SyncmillMemory:
    return SyncmillMemory(
        MemoryConfig(db_path=str(root / "mm.db"), memory_dir=str(root / "mem"))
    )


def _set_single_span(memory: SyncmillMemory, on: bool) -> None:
    backend = memory._backend
    backend._caps = dataclasses.replace(backend._caps, single_span_share=on)


def _council_once(
    memory: SyncmillMemory, members: int, rounds: int, *, agent_prefix: str = "agent"
) -> dict:
    """One council run; returns wall-clock and summed share_result time."""
    spent: list[float] = []
    real_share = memory.share_result

    async def timed_share(*args, **kwargs):
        start = time.perf_counter()
        try:
            return await real_share(*args, **kwargs)
        finally:
            spent.append(time.perf_counter() - start)

    memory.share_result = timed_share
    try:
        agents = [_ScriptedAgent(f"{agent_prefix}{i}") for i in range(members)]
        start = time.perf_counter()
        result = CouncilStrategy().execute(
            task=Task(prompt="Refactor the parser for error recovery."),
            agents=agents,
            memory=memory,
            pool=FakeWorktreePool(),
            config=SyncmillConfig(council=CouncilConfig(rounds=rounds)),
        )
        wall = time.perf_counter() - start
    finally:
        del memory.share_result
    assert result.status.value == "completed", result
    assert not memory.degraded, memory.last_error
    return {"wall": wall, "share": sum(spent), "shares": len(spent)}


def _preload(memory: SyncmillMemory, count: int) -> None:
    backend = memory._backend
    for i in range(count):
        run_async(
            backend.add(
                f"**Note**\n\nearlier result {i}: " + "context " * 40,
                tags=["task-result"],
                namespace="shared",
            )
        )


def _child_council(root: Path, mode: str, members: int, rounds: int) -> None:
    """``two-councils``' second council: wait for the go marker, run, report."""
    memory = _memory(root)
    run_async(memory._backend.ensure_ready())
    _set_single_span(memory, mode == "single-span")
    (root / "child.ready").touch()
    while not (root / "go").exists():
        time.sleep(0.005)
    try:
        sample = _council_once(memory, members, rounds, agent_prefix="other")
    finally:
        run_async(memory.close())
    print(json.dumps(sample), flush=True)


def _wait_for(path: Path, proc: subprocess.Popen) -> None:
    while not path.exists():
        if proc.poll() is not None:
            raise RuntimeError(f"helper exited early: {proc.communicate()}")
        time.sleep(0.01)


def _one_sample(name: str, mode: str, args) -> dict:
    with tempfile.TemporaryDirectory(prefix="syncmill-bench-") as tmp:
        root = Path(tmp)
        (root / "mem").mkdir()
        memory = _memory(root)
        run_async(memory._backend.ensure_ready())
        if name == "busy-day":
            _preload(memory, args.preload)
        helper = None
        if name == "contended":
            helper = subprocess.Popen(
                [sys.executable, "-c", _WRITER, str(root), str(ROOT / "src"),
                 str(args.writer_interval)],
                stdout=subprocess.PIPE, text=True,
            )
            _wait_for(root / "writer.ready", helper)
        elif name == "two-councils":
            helper = subprocess.Popen(
                [sys.executable, __file__, "--child-council", str(root), mode,
                 "--members", str(args.members), "--rounds", str(args.rounds)],
                stdout=subprocess.PIPE, text=True,
            )
            _wait_for(root / "child.ready", helper)
            (root / "go").touch()
        _set_single_span(memory, mode == "single-span")
        try:
            sample = _council_once(memory, args.members, args.rounds)
        finally:
            if helper is not None:
                (root / "writer.stop").touch()
                out, _ = helper.communicate(timeout=120)
            run_async(memory.close())
        if name == "contended":
            sample["writer_writes"] = int(out.strip() or 0)
        elif name == "two-councils":
            other = json.loads(out)
            sample["other_share"] = other["share"]
        return sample


def _scenario(name: str, args) -> dict:
    samples: dict[str, list[dict]] = {"two-call": [], "single-span": []}
    for rep in range(args.repeats):
        order = ["two-call", "single-span"] if rep % 2 == 0 else ["single-span", "two-call"]
        for mode in order:
            samples[mode].append(_one_sample(name, mode, args))
    summary: dict = {}
    for mode, rows in samples.items():
        entry = {
            "share_s_median": round(statistics.median(r["share"] for r in rows), 4),
            "share_s_min": round(min(r["share"] for r in rows), 4),
            "share_s_max": round(max(r["share"] for r in rows), 4),
            "wall_s_median": round(statistics.median(r["wall"] for r in rows), 4),
            "shares_per_run": rows[0]["shares"],
        }
        for extra in ("writer_writes", "other_share"):
            if extra in rows[0]:
                entry[f"{extra}_median"] = round(
                    statistics.median(r[extra] for r in rows), 4
                )
        summary[mode] = entry
    before = summary["two-call"]["share_s_median"]
    after = summary["single-span"]["share_s_median"]
    summary["share_time_reduction"] = round(1 - after / before, 3)
    return summary


_SCENARIOS = ("fresh", "busy-day", "contended", "two-councils")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--members", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--preload", type=int, default=400)
    parser.add_argument("--writer-interval", type=float, default=0.05)
    parser.add_argument(
        "--scenario",
        action="append",
        choices=list(_SCENARIOS),
        help="repeatable; default runs all of them",
    )
    parser.add_argument("--child-council", nargs=2, metavar=("ROOT", "MODE"),
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_council:
        _child_council(Path(args.child_council[0]), args.child_council[1],
                       args.members, args.rounds)
        return
    # Never let a developer's own memtomem config leak into the measurement.
    os.environ.pop("MEMTOMEM_CONFIG", None)
    report = {
        "members": args.members,
        "rounds": args.rounds,
        "repeats": args.repeats,
        "preload": args.preload,
        "writer_interval": args.writer_interval,
    }
    for name in args.scenario or _SCENARIOS:
        report[name] = _scenario(name, args)
        print(name, json.dumps(report[name]), file=sys.stderr, flush=True)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
