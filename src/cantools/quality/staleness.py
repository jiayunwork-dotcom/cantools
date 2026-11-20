# Online, bounded staleness-limit estimation.
#
# The estimator never looks ahead: at any point it only knows the
# explicit configuration, the database cycle time and the arrival times
# seen *so far*. All history is bounded.

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Optional

from .types import StalenessBasis, StalenessInfo

#: Microseconds per second.
US_PER_S = 1_000_000


@dataclass(frozen=True)
class StalenessPolicy:
    """Configuration of automatic staleness-limit estimation.

    :param jitter_factor: A learned/periodic nominal interval ``T`` is
        turned into a limit of ``T * jitter_factor``. Values above 1
        tolerate jitter (a 2.0 default tolerates an interval twice the
        nominal one). Larger values delay detecting a real stoppage but
        avoid false expiries during normal jitter.

    :param min_evidence: Number of observed arrival intervals required
        before the observed rhythm is trusted. Below this the database
        cycle is used when available, otherwise the
        ``insufficient_evidence`` behavior applies.

    :param history_size: Maximum number of recent arrival intervals kept
        per message (bounded memory).

    :param event_multiplier: Event-driven messages have no database
        cycle. While evidence is insufficient their provisional limit is
        the largest observed gap multiplied by this factor. With no
        observations at all the behavior depends on
        ``expire_without_evidence``.

    :param expire_without_evidence: When ``True`` (default), a signal
        with neither explicit limit, database cycle nor any observation
        gets a provisional limit of ``default_event_limit_s`` seconds so
        a stoppage is still detected eventually. When ``False`` such
        signals never expire until evidence arrives (the limit is
        ``None``). The latter avoids false positives for genuinely
        event-driven traffic but cannot flag a stoppage of a never-seen
        signal.

    :param default_event_limit_s: Provisional limit in seconds used by
        ``expire_without_evidence`` for messages without any other
        evidence.
    """

    jitter_factor: float = 2.0
    min_evidence: int = 3
    history_size: int = 16
    event_multiplier: float = 3.0
    expire_without_evidence: bool = True
    default_event_limit_s: float = 3600.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.jitter_factor) or self.jitter_factor <= 0:
            raise ValueError("jitter_factor must be a finite positive number")
        if self.min_evidence < 1:
            raise ValueError("min_evidence must be >= 1")
        if self.history_size < 1:
            raise ValueError("history_size must be >= 1")
        if (not math.isfinite(self.event_multiplier)
                or self.event_multiplier <= 0):
            raise ValueError("event_multiplier must be a finite positive number")
        if (not math.isfinite(self.default_event_limit_s)
                or self.default_event_limit_s <= 0):
            raise ValueError(
                "default_event_limit_s must be a finite positive number"
            )


class _Rhythm:
    """Bounded arrival history for one message identity."""

    __slots__ = ("count", "intervals", "last_timestamp_us", "max_gap_us")

    def __init__(self, history_size: int) -> None:
        self.intervals: deque[int] = deque(maxlen=history_size)
        self.last_timestamp_us: Optional[int] = None
        self.max_gap_us: int = 0
        self.count: int = 0

    def observe(self, timestamp_us: int) -> None:
        self.count += 1
        if self.last_timestamp_us is not None:
            gap = timestamp_us - self.last_timestamp_us
            if gap >= 0:
                self.intervals.append(gap)
                self.max_gap_us = max(self.max_gap_us, gap)
        self.last_timestamp_us = timestamp_us

    def nominal_interval_us(self) -> Optional[int]:
        """Robust nominal interval: the median of recent bounded
        intervals. The median is insensitive to a single long event-like
        gap."""
        if not self.intervals:
            return None
        ordered = sorted(self.intervals)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) // 2


class StalenessEstimator:
    """Resolve the staleness limit of one signal identity at a point in
    time, using only information available up to that point.

    Explicit per-signal limits always win and are validated up front.
    Otherwise the database cycle time anchors the limit until enough
    observed intervals exist; observed rhythm then takes over. Signals
    of event-driven messages (no cycle) use a conservative provisional
    limit while evidence is scarce.
    """

    def __init__(
        self,
        explicit_limits_s: Optional[dict[object, float]] = None,
        policy: Optional[StalenessPolicy] = None,
    ) -> None:
        self.policy = policy or StalenessPolicy()
        self._explicit_us: dict[object, int] = {}
        for key, limit_s in (explicit_limits_s or {}).items():
            if not isinstance(limit_s, (int, float)):
                raise ValueError(
                    f"explicit staleness limit for {key!r} must be a number"
                )
            if not math.isfinite(limit_s) or limit_s <= 0:
                raise ValueError(
                    f"explicit staleness limit for {key!r} must be a finite "
                    "positive number of seconds"
                )
            self._explicit_us[key] = int(round(limit_s * US_PER_S))

        # Identity (channel, frame_id, extended) -> rhythm. Signals of
        # the same message share one rhythm.
        self._rhythms: dict[tuple[str, int, bool], _Rhythm] = {}

    # ------- online updates -------

    def note_frame(self, identity: tuple[str, int, bool],
                   timestamp_us: int) -> None:
        rhythm = self._rhythms.get(identity)
        if rhythm is None:
            rhythm = _Rhythm(self.policy.history_size)
            self._rhythms[identity] = rhythm
        rhythm.observe(timestamp_us)

    # ------- queries -------

    def explicit_limit_us(self, key: object) -> Optional[int]:
        return self._explicit_us.get(key)

    def resolve(
        self,
        signal_key: object,
        identity: tuple[str, int, bool],
        db_cycle_time_s: Optional[float],
    ) -> StalenessInfo:
        """Return the limit that applies *now*, with its basis."""

        explicit = self._explicit_us.get(signal_key)
        if explicit is not None:
            return StalenessInfo(
                limit_us=explicit,
                basis=StalenessBasis.EXPLICIT,
                explanation=(
                    f"explicit limit {explicit / US_PER_S:g}s overrides all "
                    "automatic limits"
                ),
            )

        rhythm = self._rhythms.get(identity)
        intervals = rhythm.intervals if rhythm is not None else None
        enough = (
            rhythm is not None
            and intervals is not None
            and len(intervals) >= self.policy.min_evidence
        )

        if enough:
            assert rhythm is not None
            assert intervals is not None
            nominal = rhythm.nominal_interval_us()
            if nominal is not None and nominal > 0:
                limit = max(1, int(round(
                    nominal * self.policy.jitter_factor
                )))
                return StalenessInfo(
                    limit_us=limit,
                    basis=StalenessBasis.OBSERVED_HISTORY,
                    explanation=(
                        f"median observed interval "
                        f"{nominal / US_PER_S:g}s over {len(intervals)} "
                        f"frames x jitter factor {self.policy.jitter_factor:g}"
                    ),
                )

        db_cycle_us = None
        if db_cycle_time_s is not None and db_cycle_time_s > 0:
            db_cycle_us = int(round(db_cycle_time_s * US_PER_S))

        if db_cycle_us is not None:
            # Until observed evidence is sufficient the database cycle
            # anchors the limit; jitter factor still applies so ordinary
            # scheduling jitter does not trigger false expiries.
            limit = max(1, int(round(
                db_cycle_us * self.policy.jitter_factor
            )))
            seen = len(intervals) if intervals is not None else 0
            return StalenessInfo(
                limit_us=limit,
                basis=StalenessBasis.DATABASE_CYCLE,
                explanation=(
                    f"database cycle {db_cycle_time_s:g}s x jitter factor "
                    f"{self.policy.jitter_factor:g}; observed-evidence "
                    f"{seen}/{self.policy.min_evidence}"
                ),
            )

        # Event-driven message (no database cycle): provisional handling
        # while evidence is insufficient.
        if rhythm is not None and rhythm.max_gap_us > 0:
            limit = max(1, int(round(
                rhythm.max_gap_us * self.policy.event_multiplier
            )))
            return StalenessInfo(
                limit_us=limit,
                basis=StalenessBasis.INSUFFICIENT_EVIDENCE,
                explanation=(
                    f"no database cycle and only {len(rhythm.intervals)} "
                    "interval(s); provisional limit is largest observed gap "
                    f"{rhythm.max_gap_us / US_PER_S:g}s x event multiplier "
                    f"{self.policy.event_multiplier:g}"
                ),
            )

        if self.policy.expire_without_evidence:
            limit = int(round(
                self.policy.default_event_limit_s * US_PER_S
            ))
            return StalenessInfo(
                limit_us=limit,
                basis=StalenessBasis.INSUFFICIENT_EVIDENCE,
                explanation=(
                    "no database cycle and no observations yet; provisional "
                    f"default limit {self.policy.default_event_limit_s:g}s"
                ),
            )

        return StalenessInfo(
            limit_us=None,
            basis=StalenessBasis.INSUFFICIENT_EVIDENCE,
            explanation=(
                "no database cycle and no observations yet; policy keeps the "
                "signal unexpired until evidence arrives"
            ),
        )
