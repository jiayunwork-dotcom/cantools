#!/usr/bin/env python3
"""Demonstrate automatic staleness-limit causality and the false-positive
trade-off, using only synthetic in-memory messages (no bus hardware).

Run with the project venv:
    /tmp/venv/bin/python -I scripts/demo_staleness_policy.py

Scenarios:
  1. Startup with little evidence  -> database cycle anchors the limit.
  2. Periodic traffic with jitter  -> observed median takes over, a
     single long-but-normal gap does not cause a false expiry.
  3. Event-driven message           -> no cycle; a conservative
     provisional limit avoids false positives, and a real stoppage is
     still detected once the (configurable) provisional limit passes.
"""

from __future__ import annotations

import io

import cantools
from cantools.database.can.message import Message
from cantools.database.can.signal import Signal
from cantools.quality import (
    CandumpLogSource,
    QualityTimeSeries,
    SignalQuality,
    SignalSelector,
    StalenessBasis,
    StalenessPolicy,
)

T0 = 1_000_000_000


def make_db(cycle_ms=None):
    db = cantools.database.Database()
    message = Message(
        frame_id=0x100,
        name="M",
        length=1,
        signals=[Signal("S", start=0, length=8)],
        cycle_time=cycle_ms,
        strict=False,
    )
    db._messages = [message]
    db.refresh()
    return db


def run(log_lines, db, policy, stop_ms=2000):
    source = CandumpLogSource(io.StringIO("".join(log_lines)))
    series = QualityTimeSeries(
        db,
        source,
        cantools.quality.select_signals(
            db, [SignalSelector("can0", "M", "S")]
        ),
        start_us=T0,
        stop_us=T0 + stop_ms * 1000,
        step_us=100_000,
        policy=policy,
    )
    return [p for batch in series.iter_batches() for p in batch]


def frame(ms):
    return f"(1000.{ms * 1000:06d}) can0 100#01\n"


def show(title, points):
    print(f"\n=== {title} ===")
    for p in points:
        t = (p.timestamp_us - T0) / 1000
        limit = None if p.staleness.limit_us is None \
            else p.staleness.limit_us / 1000
        print(
            f"t={t:5.1f}ms {p.quality.value:8s} "
            f"limit={limit}ms basis={p.staleness.basis.value}"
        )


def main() -> None:
    # 1) Startup: only one frame so far -> database cycle (100ms) anchors
    #    a 200ms limit rather than guessing from a single arrival.
    db_cyclic = make_db(cycle_ms=100)
    policy = StalenessPolicy(min_evidence=3, jitter_factor=2.0)
    points = run([frame(0)], db_cyclic, policy)
    show("startup, scarce evidence (100ms DB cycle)", points[:6])
    first = points[0]
    assert first.staleness.basis == StalenessBasis.DATABASE_CYCLE
    assert first.staleness.limit_us == 200_000

    # 2) Periodic 100ms traffic with jitter (one 180ms gap). After three
    #    observed intervals the median-based limit takes over; the jittery
    #    gap stays inside it, so there is no false expiry.
    jittered = [frame(t) for t in (0, 100, 200, 380, 480, 580)]
    points = run(jittered, db_cyclic, policy, stop_ms=800)
    show("periodic with jitter (median x 2)", points)
    at_400 = next(p for p in points if p.timestamp_us == T0 + 400_000)
    assert at_400.quality in (SignalQuality.FRESH, SignalQuality.HELD)
    assert any(
        p.staleness.basis == StalenessBasis.OBSERVED_HISTORY for p in points
    )

    # 3a) Event-driven, no cycle: default policy gives a provisional
    #     large limit so a quiet event bus is not flagged prematurely.
    db_event = make_db(cycle_ms=None)
    event_policy = StalenessPolicy(
        expire_without_evidence=True, default_event_limit_s=0.5,
        event_multiplier=3.0,
    )
    points = run([frame(0), frame(100)], db_event, event_policy,
                 stop_ms=900)
    show("event message, provisional default limit", points)
    basis = {p.staleness.basis for p in points}
    assert StalenessBasis.INSUFFICIENT_EVIDENCE in basis
    # a real stoppage beyond the provisional limit is still detected
    assert points[-1].quality == SignalQuality.EXPIRED

    # 3b) Same event bus with expire_without_evidence=False: never
    #     expires until evidence arrives (cost: cannot flag a stoppage
    #     of a never/rarely seen signal).
    relaxed = StalenessPolicy(expire_without_evidence=False)
    points = run([frame(0)], db_event, relaxed, stop_ms=900)
    show("event message, no-expiry policy (no false positives)", points)
    assert all(p.quality != SignalQuality.EXPIRED for p in points)
    assert points[-1].staleness.limit_us is None

    print("\nAll staleness policy assertions passed.")


if __name__ == "__main__":
    main()
