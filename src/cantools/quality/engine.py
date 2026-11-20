# Streaming, single-pass quality-aware signal grid engine.

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Optional, Union

from ..database.can.database import Database
from ..database.can.message import Message
from ..database.namedsignalvalue import NamedSignalValue
from ..logreader import DataFrame
from .decoder import decode_frame_with_quality
from .logsource import CandumpLogSource, LogLine
from .staleness import US_PER_S, StalenessEstimator, StalenessPolicy
from .types import (
    Diagnostic,
    DiagnosticKind,
    QualityPoint,
    SignalKey,
    SignalQuality,
    Source,
    StalenessInfo,
)

#: Qualities that carry a usable physical value.
_USABLE = frozenset({SignalQuality.FRESH, SignalQuality.HELD})
#: Qualities that block any previously held good value.
_BLOCKING = frozenset({
    SignalQuality.INVALID,
    SignalQuality.DECODE_FAILED,
    SignalQuality.TRUNCATED,
    SignalQuality.BRANCH_INACTIVE,
})


def validate_grid(start_us: int, stop_us: int, step_us: int) -> None:
    if not isinstance(start_us, int) or not isinstance(stop_us, int) \
            or not isinstance(step_us, int):
        raise TypeError("grid start/stop/step must be integer microseconds")
    if stop_us < start_us:
        raise ValueError(
            f"grid stop ({stop_us}us) must not be earlier than start "
            f"({start_us}us)"
        )
    if step_us <= 0:
        raise ValueError(f"grid step must be a positive number of "
                         f"microseconds, got {step_us}")


def seconds_to_us(value: float, name: str = "value") -> int:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number of seconds")
    return int(round(value * US_PER_S))


@dataclass(frozen=True)
class Selection:
    """One requested signal, fully disambiguated by channel and frame
    type."""

    channel: str
    frame_id: int
    is_extended_frame: bool
    message_name: str
    signal_name: str

    @property
    def identity(self) -> tuple[str, int, bool]:
        return (self.channel, self.frame_id, self.is_extended_frame)

    @property
    def key(self) -> SignalKey:
        return SignalKey(
            channel=self.channel,
            frame_id=self.frame_id,
            is_extended_frame=self.is_extended_frame,
            message_name=self.message_name,
            signal_name=self.signal_name,
        )


class _SignalState:
    """Mutable, bounded per-signal stream state."""

    __slots__ = (
        "db_cycle_s",
        "key",
        "last_quality",
        "last_raw",
        "last_source",
        "last_source_us",
        "last_value",
        "selection",
    )

    def __init__(self, selection: Selection,
                 db_cycle_s: Optional[float]) -> None:
        self.key = selection.key
        self.selection = selection
        self.db_cycle_s = db_cycle_s
        # No observation yet.
        self.last_quality: SignalQuality = SignalQuality.UNSEEN
        self.last_value: Optional[Union[int, float, str, NamedSignalValue]] = None
        self.last_raw: Optional[Union[int, float]] = None
        self.last_source: Optional[Source] = None
        self.last_source_us: Optional[int] = None

    def apply_observation(self, obs) -> None:
        """Fold one per-signal observation into the state.

        Blocking observations replace (never preserve) the previous good
        value. A branch switch immediately deactivates the old branch; a
        later switch back requires a fresh good sample to recover.
        """
        self.last_quality = obs.quality
        self.last_source = obs.source
        self.last_source_us = obs.source.timestamp_us
        if obs.quality in _USABLE:
            self.last_value = obs.value
            self.last_raw = obs.raw_value
        else:
            # INVALID keeps its raw value (never scaled/enumerated);
            # every other blocking state carries no usable value.
            self.last_value = None
            self.last_raw = (
                obs.raw_value
                if obs.quality == SignalQuality.INVALID
                else None
            )


class QualityTimeSeries:
    """Forward-only, single-pass quality-aware signal time series.

    Constant resident state:

    * one :class:`_SignalState` per selected signal,
    * one bounded rhythm per distinct selected message identity,
    * the output batch currently being produced,
    * diagnostics accumulated by the log source (bounded only by the
      number of bad lines, which callers can drain).

    No raw frames and no past grid points are retained between batches.
    """

    def __init__(
        self,
        database: Database,
        source: CandumpLogSource,
        selections: Sequence[Selection],
        *,
        start_us: int,
        stop_us: int,
        step_us: int,
        explicit_limits_s: Optional[dict[SignalKey, float]] = None,
        policy: Optional[StalenessPolicy] = None,
        decode_choices: bool = True,
        scaling: bool = True,
    ) -> None:
        validate_grid(start_us, stop_us, step_us)
        if not selections:
            raise ValueError("at least one signal must be selected")

        self._db = database
        self._source = source
        self._start_us = start_us
        self._stop_us = stop_us
        self._step_us = step_us
        self._decode_choices = decode_choices
        self._scaling = scaling

        # Explicit limits keyed by the frozen SignalKey objects.
        explicit: dict[object, float] = {}
        for key, limit in (explicit_limits_s or {}).items():
            explicit[key] = limit
        self._estimator = StalenessEstimator(explicit, policy)

        # Resolve and validate selections against the database, building
        # the message -> selected signal names index. A selection on the
        # empty channel is a wildcard: per-channel states are created on
        # demand when the first frame of that channel is seen, so the
        # log is never pre-scanned.
        self._states: dict[SignalKey, _SignalState] = {}
        self._message_index: dict[
            tuple[str, int, bool], tuple[Message, set[str]]
        ] = {}
        self._wildcard_messages: dict[
            tuple[int, bool], tuple[Message, set[str]]
        ] = {}
        seen_keys: set[SignalKey] = set()
        self._wildcard_signals: set[tuple[int, bool, str]] = set()
        for selection in selections:
            if selection.key in seen_keys:
                raise ValueError(f"duplicate signal selection: {selection.key}")
            seen_keys.add(selection.key)

            message = self._lookup_message(selection)
            # Validate the signal exists.
            message.get_signal_by_name(selection.signal_name)

            if selection.channel == "":
                wildcard_id = (selection.frame_id,
                               selection.is_extended_frame)
                entry = self._wildcard_messages.get(wildcard_id)
                if entry is None:
                    self._wildcard_messages[wildcard_id] = (
                        message, {selection.signal_name}
                    )
                else:
                    entry[1].add(selection.signal_name)
                self._wildcard_signals.add(
                    (selection.frame_id,
                     selection.is_extended_frame,
                     selection.signal_name)
                )
                continue

            state = _SignalState(
                selection,
                message.cycle_time / 1000.0
                if message.cycle_time else None,
            )
            self._states[selection.key] = state

            index = self._message_index.get(selection.identity)
            if index is None:
                self._message_index[selection.identity] = (
                    message, {selection.signal_name}
                )
            else:
                index[1].add(selection.signal_name)

        # Wildcard states are populated lazily; expose their (empty at
        # first) state map through the same attribute.
        self.diagnostics: list[Diagnostic] = source.diagnostics
        self._frame_iter: Optional[Iterator[LogLine]] = None
        self._lookahead: Optional[LogLine] = None
        self._closed = False
        self._materialized_wildcard_keys: set[SignalKey] = set()

    # ------- setup helpers -------

    def _lookup_message(self, selection: Selection):
        try:
            message = self._db.get_message_by_frame_id(
                selection.frame_id,
                force_extended_id=selection.is_extended_frame,
            )
        except KeyError:
            raise ValueError(
                f"no message in database for frame id "
                f"0x{selection.frame_id:X} "
                f"({'extended' if selection.is_extended_frame else 'standard'})"
            ) from None
        if message.name != selection.message_name:
            raise ValueError(
                f"frame id 0x{selection.frame_id:X} resolves to message "
                f"{message.name!r}, not {selection.message_name!r}"
            )
        return message

    # ------- iteration -------

    def grid_timestamps_us(self) -> Iterator[int]:
        t = self._start_us
        while t <= self._stop_us:
            yield t
            t += self._step_us

    def iter_batches(self) -> Iterator[list[QualityPoint]]:
        """Yield one list of :class:`QualityPoint` per grid point.

        The frame log is read at most once. Each batch is independent;
        nothing about past batches is retained internally. The grid loop
        is written inline (not as a nested generator) so the single-pass
        frame iterator and its lookahead keep working across batches.
        """
        if self._closed:
            raise RuntimeError("time series has already been consumed/closed")
        self._frame_iter = iter(self._source)
        try:
            grid_us = self._start_us
            while grid_us <= self._stop_us:
                self._advance_frames(grid_us)
                yield self._build_batch(grid_us)
                grid_us += self._step_us
            # Drain frames arriving after the last grid point. They do
            # not produce grid points, but unknown-message diagnostics
            # and rhythm evidence must still be complete, and the
            # single-pass log must be fully consumed.
            self._drain_remaining_frames()
        finally:
            self.close()

    def _drain_remaining_frames(self) -> None:
        if self._lookahead is not None:
            frame_log = self._lookahead
            self._lookahead = None
            self._process_frame(frame_log)
        assert self._frame_iter is not None
        for frame_log in self._frame_iter:
            self._process_frame(frame_log)

    def __iter__(self) -> Iterator[QualityPoint]:
        for batch in self.iter_batches():
            yield from batch

    def close(self) -> None:
        """Release the underlying log stream (if owned by the source)."""
        if not self._closed:
            self._source.close()
            self._closed = True

    def __enter__(self) -> QualityTimeSeries:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------- internals -------

    def _advance_frames(self, grid_us: int) -> None:
        """Consume every recognized frame with timestamp <= grid point.

        A single one-frame lookahead slot bridges grid points: the log is
        read strictly forward and never rewound.
        """
        assert self._frame_iter is not None
        while True:
            frame_log: Optional[LogLine]
            if self._lookahead is not None:
                frame_log = self._lookahead
                self._lookahead = None
            else:
                frame_log = next(self._frame_iter, None)
            if frame_log is None:
                return
            if frame_log.timestamp_us > grid_us:
                self._lookahead = frame_log
                return
            self._process_frame(frame_log)

    def _materialize_wildcard(
        self,
        frame: "DataFrame",
        wildcard: tuple[Message, set[str]],
    ) -> None:
        """Create per-channel states for a wildcard (empty channel)
        selection the first time the channel is observed."""
        message, signal_names = wildcard
        identity = (
            frame.channel, frame.frame_id, frame.is_extended_frame
        )
        if identity in self._message_index:
            return

        self._message_index[identity] = (message, set(signal_names))
        for signal_name in signal_names:
            selection = Selection(
                channel=frame.channel,
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                message_name=message.name,
                signal_name=signal_name,
            )
            if selection.key in self._materialized_wildcard_keys:
                continue
            self._materialized_wildcard_keys.add(selection.key)
            state = _SignalState(
                selection,
                message.cycle_time / 1000.0
                if message.cycle_time else None,
            )
            self._states[selection.key] = state

    def _process_frame(self, frame_log: LogLine) -> None:
        frame = frame_log.frame
        identity = (
            frame.channel, frame.frame_id, frame.is_extended_frame
        )
        index = self._message_index.get(identity)
        if index is None:
            wildcard = self._wildcard_messages.get(
                (frame.frame_id, frame.is_extended_frame)
            )
            if wildcard is not None:
                self._materialize_wildcard(frame, wildcard)
                index = self._message_index.get(identity)
        if index is None:
            # Frame on a non-selected identity (may still be known to
            # the database). Not part of this series; the log source has
            # no opinion either way. Unknown *database* messages are
            # recorded as diagnostics here.
            try:
                self._db.get_message_by_frame_id(
                    frame.frame_id,
                    force_extended_id=frame.is_extended_frame,
                )
            except KeyError:
                self.diagnostics.append(Diagnostic(
                    line=frame_log.line,
                    kind=DiagnosticKind.UNKNOWN_MESSAGE,
                    timestamp_us=frame_log.timestamp_us,
                    message=(
                        f"unknown message frame id 0x{frame.frame_id:X} "
                        f"on channel {frame.channel!r}"
                    ),
                    raw_line=frame_log.raw_line,
                ))
            return

        message, selected_names = index
        source = Source(
            line=frame_log.line,
            timestamp_us=frame_log.timestamp_us,
            channel=frame.channel,
            frame_id=frame.frame_id,
            is_extended_frame=frame.is_extended_frame,
        )

        def key_factory(signal_name: str) -> SignalKey:
            return SignalKey(
                channel=frame.channel,
                frame_id=frame.frame_id,
                is_extended_frame=frame.is_extended_frame,
                message_name=message.name,
                signal_name=signal_name,
            )

        decoded = decode_frame_with_quality(
            message=message,
            data=frame.data,
            source=source,
            key_factory=key_factory,
            decode_choices=self._decode_choices,
            scaling=self._scaling,
            selected_signals=selected_names,
        )

        # Rhythm learning uses every recognized frame of a selected
        # message identity, including frames that fail to decode: their
        # arrival still says the sender is alive.
        self._estimator.note_frame(identity, frame_log.timestamp_us)

        for obs in decoded.observations.values():
            state = self._states.get(obs.key)
            if state is not None:
                state.apply_observation(obs)

    def _build_batch(self, grid_us: int) -> list[QualityPoint]:
        batch: list[QualityPoint] = []
        for state in self._states.values():
            batch.append(self._point_for(state, grid_us))
        return batch

    def _point_for(self, state: _SignalState, grid_us: int) -> QualityPoint:
        staleness = self._estimator.resolve(
            state.key,
            state.selection.identity,
            state.db_cycle_s,
        )

        if state.last_quality == SignalQuality.UNSEEN:
            return self._make_point(
                state, grid_us, SignalQuality.UNSEEN,
                value=None, raw=None, source=None, source_us=None,
                age=None, staleness=staleness,
            )

        assert state.last_source is not None
        age = grid_us - state.last_source_us  # type: ignore[operator]

        # Blocking qualities remain blocking at every later grid point
        # until a new observation resolves them. They are never silently
        # converted back to held values.
        if state.last_quality in _BLOCKING:
            return self._make_point(
                state, grid_us, state.last_quality,
                value=None,
                raw=state.last_raw,
                source=state.last_source,
                source_us=state.last_source_us,
                age=age,
                staleness=staleness,
            )

        # Last observation was good (FRESH at its arrival). Expiry is
        # evaluated against the current limit.
        expired = (
            staleness.limit_us is not None and age > staleness.limit_us
        )
        if expired:
            return self._make_point(
                state, grid_us, SignalQuality.EXPIRED,
                value=None, raw=None,
                source=state.last_source,
                source_us=state.last_source_us,
                age=age, staleness=staleness,
            )

        quality = (
            SignalQuality.FRESH if age == 0 else SignalQuality.HELD
        )
        return self._make_point(
            state, grid_us, quality,
            value=state.last_value,
            raw=state.last_raw,
            source=state.last_source,
            source_us=state.last_source_us,
            age=age, staleness=staleness,
        )

    @staticmethod
    def _make_point(state, grid_us, quality, *, value, raw, source,
                    source_us, age, staleness: StalenessInfo) -> QualityPoint:
        return QualityPoint(
            key=state.key,
            timestamp_us=grid_us,
            quality=quality,
            value=value,
            raw_value=raw,
            source=source,
            source_timestamp_us=source_us,
            age_us=age,
            staleness=staleness,
        )
