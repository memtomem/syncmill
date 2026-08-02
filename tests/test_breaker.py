"""breaker.py — CircuitBreaker state machine, persistence, degradation.

Exercises the REAL CircuitBreaker (not the FakeBreaker used in supervisor tests)
with an injected clock and an explicit tmp_path state file, so it's hermetic and
never sleeps. Strategy-level integration (skip OPEN agents, record outcomes) lives
in test_supervisor.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from syncmill import breaker as breaker_module
from syncmill.breaker import CircuitBreaker, build_breaker
from syncmill.config import BreakerConfig
from syncmill.models import TaskStatus


class Clock:
    """Manually-advanced wall clock — the injected ``now`` for deterministic
    cooldown tests (no sleeping)."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _breaker(tmp_path: Path, *, threshold=3, cooldown=300, clock=None) -> CircuitBreaker:
    clock = clock or Clock()
    return CircuitBreaker(
        failure_threshold=threshold,
        cooldown_seconds=cooldown,
        state_path=str(tmp_path / "breaker.json"),
        now=clock,
    )


# ── build_breaker ─────────────────────────────────────────────────────────


def test_build_breaker_disabled_returns_none():
    assert build_breaker(BreakerConfig(enabled=False)) is None


def test_build_breaker_default_is_disabled():
    # the disabled default is what keeps out-of-the-box behavior unchanged
    assert build_breaker(BreakerConfig()) is None


def test_build_breaker_enabled_constructs(tmp_path):
    b = build_breaker(
        BreakerConfig(
            enabled=True,
            failure_threshold=2,
            cooldown_seconds=10,
            state_path=str(tmp_path / "b.json"),
        )
    )
    assert isinstance(b, CircuitBreaker)


# ── state machine ───────────────────────────────────────────────────────────


def test_closed_allows_below_threshold(tmp_path):
    b = _breaker(tmp_path, threshold=3)
    assert b.allows("claude") is True
    b.record("claude", TaskStatus.FAILED)
    b.record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is True  # 2 < 3, still CLOSED


def test_opens_after_threshold_consecutive_failures(tmp_path):
    b = _breaker(tmp_path, threshold=3)
    for _ in range(3):
        b.record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is False  # OPEN


def test_open_skips_within_cooldown(tmp_path):
    clock = Clock(1000.0)
    b = _breaker(tmp_path, threshold=1, cooldown=300, clock=clock)
    b.record("claude", TaskStatus.FAILED)  # opens at t=1000
    clock.t = 1000.0 + 299  # still inside cooldown
    assert b.allows("claude") is False


def test_half_open_after_cooldown_allows(tmp_path):
    clock = Clock(1000.0)
    b = _breaker(tmp_path, threshold=1, cooldown=300, clock=clock)
    b.record("claude", TaskStatus.FAILED)  # opens at t=1000
    clock.t = 1000.0 + 300  # cooldown elapsed
    assert b.allows("claude") is True  # HALF_OPEN, one trial


def test_success_closes_and_resets(tmp_path):
    b = _breaker(tmp_path, threshold=1, cooldown=300)
    b.record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is False
    b.record("claude", TaskStatus.COMPLETED)
    assert b.allows("claude") is True  # CLOSED again


def test_half_open_trial_success_closes(tmp_path):
    clock = Clock(1000.0)
    b = _breaker(tmp_path, threshold=2, cooldown=100, clock=clock)
    b.record("claude", TaskStatus.FAILED)
    b.record("claude", TaskStatus.FAILED)  # OPEN at t=1000
    clock.t = 1100.0  # HALF_OPEN
    assert b.allows("claude") is True
    b.record("claude", TaskStatus.COMPLETED)  # trial succeeds -> CLOSED
    clock.t = 1100.0
    assert b.allows("claude") is True
    # and the failure count was wiped: one more failure shouldn't re-open
    b.record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is True


def test_half_open_trial_failure_restarts_cooldown(tmp_path):
    clock = Clock(1000.0)
    b = _breaker(tmp_path, threshold=1, cooldown=300, clock=clock)
    b.record("claude", TaskStatus.FAILED)  # OPEN at t=1000
    clock.t = 1300.0  # cooldown elapsed -> HALF_OPEN
    assert b.allows("claude") is True
    b.record("claude", TaskStatus.FAILED)  # trial fails -> re-OPEN at t=1300
    assert b.allows("claude") is False
    clock.t = 1300.0 + 299
    assert b.allows("claude") is False  # cooldown measured from the NEW failure
    clock.t = 1300.0 + 300
    assert b.allows("claude") is True


def test_intermittent_success_resets_consecutive_count(tmp_path):
    # consecutive, not cumulative: a success in the middle resets the count
    b = _breaker(tmp_path, threshold=3)
    b.record("claude", TaskStatus.FAILED)
    b.record("claude", TaskStatus.FAILED)
    b.record("claude", TaskStatus.COMPLETED)  # reset
    b.record("claude", TaskStatus.FAILED)
    b.record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is True  # only 2 in a row since the reset


def test_breaker_is_per_agent(tmp_path):
    b = _breaker(tmp_path, threshold=1)
    b.record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is False
    assert b.allows("codex") is True  # independent state


def test_non_completed_statuses_count_as_failure(tmp_path):
    # anything that isn't COMPLETED is a failure for the breaker
    b = _breaker(tmp_path, threshold=2)
    b.record("claude", TaskStatus.FAILED)
    b.record("claude", TaskStatus.RUNNING)
    assert b.allows("claude") is False


# ── persistence ─────────────────────────────────────────────────────────────


def test_open_state_survives_reload(tmp_path):
    # the whole point: failures accumulate ACROSS separate breaker instances
    # (= separate `syncmill run` processes)
    path = str(tmp_path / "breaker.json")
    clock = Clock(1000.0)
    b1 = CircuitBreaker(3, 300, path, now=clock)
    b1.record("claude", TaskStatus.FAILED)
    b1.record("claude", TaskStatus.FAILED)
    b1.record("claude", TaskStatus.FAILED)  # OPEN

    b2 = CircuitBreaker(3, 300, path, now=clock)
    assert b2.allows("claude") is False  # loaded the OPEN state


def test_success_persists_reset(tmp_path):
    path = str(tmp_path / "breaker.json")
    b1 = CircuitBreaker(1, 300, path)
    b1.record("claude", TaskStatus.FAILED)  # OPEN
    b1.record("claude", TaskStatus.COMPLETED)  # CLOSED, persisted
    b2 = CircuitBreaker(1, 300, path)
    assert b2.allows("claude") is True


def test_degraded_is_not_a_one_way_latch(tmp_path):
    # `degraded` reflects the LATEST persistence outcome: a transient failure
    # at construction must not leave a long-lived process (board run --loop)
    # permissive forever once later I/O succeeds.
    path = tmp_path / "breaker.json"
    path.write_text("{ this is not valid json")
    b = CircuitBreaker(1, 300, str(path))
    assert b.degraded is True
    b.record("claude", TaskStatus.FAILED)  # save succeeds → recovered
    assert b.degraded is False
    assert b.last_error is None
    assert b.allows("claude") is False  # and the breaker actually works again


def test_degraded_clears_when_a_reload_reads_a_repaired_file(tmp_path):
    path = tmp_path / "breaker.json"
    path.write_text("{ this is not valid json")
    b = CircuitBreaker(1, 300, str(path))
    assert b.degraded is True
    # another process repairs/rewrites the file → the mtime-guarded reload in
    # allows() picks it up and clears the flag
    CircuitBreaker(1, 300, str(path)).record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is False
    assert b.degraded is False


def test_allows_picks_up_another_processes_write(tmp_path):
    # A long-lived process holds one breaker while other one-shot runs write
    # the shared file; allows() must see their recorded failures, not decide
    # from the state loaded at construction forever.
    path = str(tmp_path / "breaker.json")
    long_lived = CircuitBreaker(1, 300, path)
    assert long_lived.allows("claude") is True
    other = CircuitBreaker(1, 300, path)
    other.record("claude", TaskStatus.FAILED)  # opens the circuit on disk
    assert long_lived.allows("claude") is False


def test_save_caches_the_marker_of_its_own_write_not_a_racing_writers(
    tmp_path, monkeypatch
):
    # A concurrent writer replacing the file right after our os.replace must
    # not get ITS file marker cached against OUR in-memory state — that pairing
    # makes every later allows() skip the mtime-guarded reload, so a long-lived
    # process would ignore the newly opened circuit until some other write.
    path = tmp_path / "breaker.json"
    a = CircuitBreaker(2, 300, str(path))
    b = CircuitBreaker(2, 300, str(path))
    real_replace = os.replace
    raced: list = []

    def racing_replace(src, dst):
        real_replace(src, dst)
        if not raced and Path(dst) == path:
            raced.append(1)
            # B lands in the window between A's replace and any later stat of
            # state_path; bump the mtime so a coarse filesystem clock can't
            # accidentally hide the race from the assertion.
            b.record("claude", TaskStatus.FAILED)
            b.record("claude", TaskStatus.FAILED)  # OPEN on disk
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    monkeypatch.setattr(breaker_module.os, "replace", racing_replace)
    a.record("claude", TaskStatus.COMPLETED)
    monkeypatch.undo()
    assert raced == [1]
    assert a.allows("claude") is False  # A must see B's OPEN circuit


def test_concurrent_writers_are_last_writer_wins_not_corrupt(tmp_path):
    # The documented concurrency contract: two breakers on the same file (two
    # `syncmill run` processes) race last-writer-wins — the atomic os.replace
    # keeps the file parseable; a lost increment is acceptable for advisory
    # back-off. Prove: no corruption, last writer's view persists.
    path = str(tmp_path / "breaker.json")
    b1 = CircuitBreaker(3, 300, path)
    b2 = CircuitBreaker(3, 300, path)  # both loaded the same (empty) state
    b1.record("claude", TaskStatus.FAILED)  # b1 writes failures=1
    b2.record("claude", TaskStatus.FAILED)  # b2 (stale view) overwrites: failures=1
    # b1's increment is lost (last-writer-wins), but the file is well-formed JSON
    b3 = CircuitBreaker(3, 300, path)
    assert b3.degraded is False
    data = json.loads(Path(path).read_text())
    assert data["claude"]["failures"] == 1  # b2's view won; not corrupt, not 2


def test_state_file_format(tmp_path):
    path = tmp_path / "breaker.json"
    clock = Clock(1234.5)
    b = CircuitBreaker(1, 300, str(path), now=clock)
    b.record("claude", TaskStatus.FAILED)
    data = json.loads(path.read_text())
    assert data["claude"]["failures"] == 1
    assert data["claude"]["opened_at"] == 1234.5


def test_missing_file_is_fresh_not_degraded(tmp_path):
    b = _breaker(tmp_path / "does-not-exist-yet")
    assert b.degraded is False
    assert b.allows("claude") is True


def test_corrupt_file_degrades_and_stays_permissive(tmp_path):
    path = tmp_path / "breaker.json"
    path.write_text("{ this is not valid json")
    b = CircuitBreaker(1, 300, str(path))
    assert b.degraded is True
    assert b.last_error and "load" in b.last_error
    # a broken state file must NOT block agents from running
    assert b.allows("claude") is True


@pytest.mark.parametrize("blob", ["[]", "42", '"a string"', "null"])
def test_wrong_shape_state_degrades_and_stays_permissive(tmp_path, blob):
    # valid JSON but not a {agent: state} object → broken, not empty: degrade
    # (surface it) but stay permissive so agents still run
    path = tmp_path / "breaker.json"
    path.write_text(blob)
    b = CircuitBreaker(1, 300, str(path))
    assert b.degraded is True
    assert b.last_error and "not a JSON object" in b.last_error
    assert b.allows("claude") is True


def test_malformed_entry_skipped_others_loaded(tmp_path):
    path = tmp_path / "breaker.json"
    path.write_text(
        json.dumps(
            {
                "claude": {"failures": "not-an-int", "opened_at": None},  # malformed
                "codex": {"failures": 5, "opened_at": 1000.0},  # valid
            }
        )
    )
    clock = Clock(1000.0)
    b = CircuitBreaker(3, 300, str(path), now=clock)
    assert b.degraded is False  # lenient per-entry, not a whole-file failure
    assert b.allows("claude") is True  # malformed entry dropped -> treated CLOSED
    assert b.allows("codex") is False  # valid OPEN entry honored


def test_threshold_reached_but_no_timestamp_does_not_trap(tmp_path):
    # defensive: a state file with failures>=threshold but opened_at null must not
    # wedge the agent OPEN forever
    path = tmp_path / "breaker.json"
    path.write_text(json.dumps({"claude": {"failures": 9, "opened_at": None}}))
    b = CircuitBreaker(3, 300, str(path))
    assert b.allows("claude") is True


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_opened_at_does_not_wedge_open(tmp_path, token):
    # json.loads accepts NaN/Infinity; a hand-edited/corrupt file with one must
    # not trap the agent OPEN forever (it would slip past the `opened_at is None`
    # guard). It's normalized to None -> defensive "no timestamp" path -> allowed.
    path = tmp_path / "breaker.json"
    path.write_text('{"claude": {"failures": 9, "opened_at": %s}}' % token)
    b = CircuitBreaker(3, 300, str(path))
    assert b.degraded is False  # parseable, just non-finite
    assert b.allows("claude") is True


def test_future_finite_opened_at_does_not_wedge_open(tmp_path):
    # a finite-but-future opened_at (huge corrupt value, or clock-skew) is NOT
    # caught by isfinite, yet clamps elapsed to 0 → OPEN forever. The future-check
    # normalizes it to None so it can't trap the agent.
    path = tmp_path / "breaker.json"
    path.write_text(json.dumps({"claude": {"failures": 5, "opened_at": 1e308}}))
    b = CircuitBreaker(3, 300, str(path), now=Clock(1000.0))
    assert b.degraded is False
    assert b.allows("claude") is True


def test_negative_failures_clamped_so_circuit_still_opens(tmp_path):
    # a corrupt negative count would stay < threshold no matter how many real
    # failures follow, suppressing back-off; it's clamped to 0 on load
    path = tmp_path / "breaker.json"
    path.write_text(json.dumps({"claude": {"failures": -10, "opened_at": None}}))
    b = CircuitBreaker(3, 300, str(path))
    assert b.allows("claude") is True  # clamped to 0 → CLOSED
    for _ in range(3):
        b.record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is False  # 3 real failures from 0 → OPENS (not -7)


def test_creates_parent_directories(tmp_path):
    nested = tmp_path / "a" / "b" / "breaker.json"
    b = CircuitBreaker(1, 300, str(nested))
    b.record("claude", TaskStatus.FAILED)
    assert nested.exists()


def test_expanduser_state_path_writes_under_home(tmp_path, monkeypatch):
    # ~ in state_path is expanded by the breaker (config holds a raw string).
    # isolated_home already points HOME at tmp_path/home; be explicit here too.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    b = CircuitBreaker(1, 300, "~/.syncmill/breaker.json")
    b.record("claude", TaskStatus.FAILED)
    assert (home / ".syncmill" / "breaker.json").exists()


# ── clock robustness ────────────────────────────────────────────────────────


def test_backward_clock_skew_keeps_open_not_wedged(tmp_path):
    clock = Clock(1000.0)
    b = _breaker(tmp_path, threshold=1, cooldown=300, clock=clock)
    b.record("claude", TaskStatus.FAILED)  # OPEN at t=1000
    clock.t = 500.0  # wall clock jumps BACKWARD
    # elapsed is clamped to >=0, so a backward jump can't make it spuriously
    # half-open (or error); the agent stays OPEN.
    assert b.allows("claude") is False


def test_zero_cooldown_half_opens_immediately(tmp_path):
    clock = Clock(1000.0)
    b = _breaker(tmp_path, threshold=1, cooldown=0, clock=clock)
    b.record("claude", TaskStatus.FAILED)
    assert b.allows("claude") is True  # cooldown=0 -> immediately HALF_OPEN


def test_threshold_and_cooldown_floors(tmp_path):
    # constructor floors a nonsensical threshold to 1 and cooldown to 0
    b = CircuitBreaker(0, -100, str(tmp_path / "b.json"))
    b.record("claude", TaskStatus.FAILED)  # threshold floored to 1 -> opens
    assert b.allows("claude") is True  # cooldown floored to 0 -> half-open at once


def test_save_failure_degrades_without_raising(tmp_path, monkeypatch):
    b = _breaker(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("syncmill.breaker.tempfile.mkstemp", boom)
    b.record("claude", TaskStatus.FAILED)  # must not raise
    assert b.degraded is True
    assert b.last_error and "save" in b.last_error
