# Core data types for quality-aware signal time series.

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from ..database.namedsignalvalue import NamedSignalValue

#: A physical signal value as returned by the regular decode chain.
SignalValue = Union[int, float, str, "NamedSignalValue"]


class SignalQuality(str, enum.Enum):
    """Per-signal quality of a single grid point.

    The string values are stable and used verbatim in CSV exports.
    """

    #: The signal has never been observed (no source frame).
    UNSEEN = "unseen"

    #: The grid point coincides with a fresh, successfully decoded
    #: observation.
    FRESH = "fresh"

    #: The value is held from the most recent observation, which is still
    #: within its staleness limit.
    HELD = "held"

    #: The most recent observation is older than the staleness limit;
    #: the held value may no longer represent the bus.
    EXPIRED = "expired"

    #: The most recent observation carried the signal's raw invalid
    #: value (``Signal.raw_invalid``). It blocks any older good value.
    INVALID = "invalid"

    #: The signal belongs to a multiplexer branch that is not active in
    #: the most recent frame of its message.
    BRANCH_INACTIVE = "branch_inactive"

    #: The most recent frame of the message could not be decoded (bad
    #: payload, unknown multiplexer value, ...). It blocks any older
    #: good value.
    DECODE_FAILED = "decode_failed"

    #: The signal's bit field is not fully contained in the most recent
    #: (short) payload. It blocks any older good value.
    TRUNCATED = "truncated"


class StalenessBasis(str, enum.Enum):
    """Where a staleness limit came from."""

    #: Provided explicitly by the caller (always takes precedence).
    EXPLICIT = "explicit"

    #: Derived from the database message cycle time.
    DATABASE_CYCLE = "database_cycle"

    #: Learned online from observed frame arrival intervals.
    OBSERVED_HISTORY = "observed_history"

    #: Not enough evidence yet; a conservative provisional limit is in
    #: use.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DiagnosticKind(str, enum.Enum):
    """Kind of a log-level diagnostic entry."""

    REMOTE_FRAME = "remote_frame"
    UNKNOWN_MESSAGE = "unknown_message"
    MALFORMED_LINE = "malformed_line"
    BAD_PAYLOAD = "bad_payload"
    TIME_REGRESSION = "time_regression"


@dataclass(frozen=True)
class SignalKey:
    """Identity of a signal on a specific channel and frame type.

    The same frame id on two channels (or standard vs. extended) is a
    distinct key, so values from different buses can never be mixed.
    """

    channel: str
    frame_id: int
    is_extended_frame: bool
    message_name: str
    signal_name: str

    def __str__(self) -> str:
        kind = "extended" if self.is_extended_frame else "standard"
        return (f"{self.channel}:{self.message_name}.{self.signal_name}"
                f"[0x{self.frame_id:X},{kind}]")


@dataclass(frozen=True)
class Source:
    """Where a value (or the event that invalidated it) came from."""

    #: 1-based line number in the original log.
    line: int
    #: Timestamp in whole microseconds since the epoch (absolute logs).
    timestamp_us: int
    #: Raw channel name as found in the log.
    channel: str
    frame_id: int
    is_extended_frame: bool


@dataclass(frozen=True)
class StalenessInfo:
    """The staleness limit applied at a grid point and why."""

    #: Limit in microseconds (``None`` while no limit can be given at
    #: all, e.g. an event message with no observations yet).
    limit_us: Optional[int]
    basis: StalenessBasis
    #: Human readable explanation of how the limit was derived.
    explanation: str


@dataclass(frozen=True)
class QualityPoint:
    """One signal value on the regular output grid.

    ``value`` is the physical value produced by the regular database
    decode chain (scaled/enumerated) whenever ``quality`` is
    :data:`SignalQuality.FRESH` or :data:`SignalQuality.HELD`. It is
    ``None`` for every unavailable quality. Invalid raw values are
    reported separately in ``raw_value`` and never surface as a trusted
    scaled or enumerated reading.
    """

    key: SignalKey
    #: Grid timestamp in whole microseconds.
    timestamp_us: int
    quality: SignalQuality
    value: Optional[SignalValue]
    #: Raw (unscaled) value of the underlying observation, if a complete
    #: bit field was available. Set for INVALID as well.
    raw_value: Optional[Union[int, float]]
    #: Source of the observation backing this point. For FRESH/HELD it
    #: is the last good observation; for the blocking qualities it is
    #: the observation that made the signal unavailable. ``None`` for
    #: UNSEEN, which has no source by definition.
    source: Optional[Source]
    #: Timestamp of the backing observation in microseconds.
    source_timestamp_us: Optional[int]
    #: Age of the backing observation at the grid point, in
    #: microseconds. ``None`` for UNSEEN.
    age_us: Optional[int]
    staleness: StalenessInfo


@dataclass(frozen=True)
class Diagnostic:
    """A log line that could not be turned into a valid report."""

    line: int
    kind: DiagnosticKind
    timestamp_us: Optional[int]
    message: str
    raw_line: str


@dataclass(frozen=True)
class SignalObservation:
    """The per-signal result of decoding one recognized frame.

    This is the quality-aware counterpart of the plain decode dict. It
    is produced for every selected signal of every recognized data
    frame, including frames that fail to decode.
    """

    key: SignalKey
    timestamp_us: int
    source: Source
    quality: SignalQuality
    value: Optional[SignalValue]
    raw_value: Optional[Union[int, float]]
    #: Error message for DECODE_FAILED, otherwise ``None``.
    error: Optional[str] = field(default=None)


@dataclass(frozen=True)
class DecodedFrame:
    """Quality-aware decode result for a single recognized data frame."""

    channel: str
    frame_id: int
    is_extended_frame: bool
    message_name: str
    timestamp_us: int
    source: Source
    #: Signal name -> :class:`SignalObservation` for the selected
    #: signals of this message (all of them when used without a
    #: selection).
    observations: dict[str, SignalObservation]
    #: Error message if the frame as a whole could not be decoded.
    error: Optional[str]
