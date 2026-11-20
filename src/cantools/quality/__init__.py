"""Quality-aware signal time series over CAN acquisition logs.

This package adds a *quality* dimension to the existing cantools decode
chain. Instead of treating the most recent decoded value as eternally
valid, every grid point of every selected signal is classified as one
of:

* :data:`~cantools.quality.types.SignalQuality.UNSEEN` -- never observed,
* :data:`~cantools.quality.types.SignalQuality.FRESH` -- a new
  observation at this grid point,
* :data:`~cantools.quality.types.SignalQuality.HELD` -- the most recent
  observation is still within its staleness limit,
* :data:`~cantools.quality.types.SignalQuality.EXPIRED` -- older than
  the limit,
* :data:`~cantools.quality.types.SignalQuality.INVALID` -- the frame
  carried the signal's defined raw invalid value,
* :data:`~cantools.quality.types.SignalQuality.BRANCH_INACTIVE` -- the
  multiplexer branch of the signal is not active,
* :data:`~cantools.quality.types.SignalQuality.DECODE_FAILED` -- the
  whole message could not be decoded,
* :data:`~cantools.quality.types.SignalQuality.TRUNCATED` -- the signal's
  bit field is not fully present in a short payload.

The reader is strictly forward-only and single-pass; resident memory is
bounded by the number of selected signals, the bounded rhythm history
and the current output batch.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Optional

from .. import database as _database
from ..database.can.database import Database as CanDatabase
from .csv_export import write_long_csv, write_wide_csv
from .decoder import decode_frame_with_quality
from .decoding import decode_message_quality
from .engine import QualityTimeSeries, Selection, seconds_to_us, validate_grid
from .logsource import (
    CandumpLogSource,
    TimeRegressionError,
    parse_log_line,
    parse_timestamp_us,
)
from .result import QualityDecodeResult, SignalStatus
from .selection import SignalSelector, select_signals
from .staleness import StalenessEstimator, StalenessPolicy
from .types import (
    Diagnostic,
    DiagnosticKind,
    QualityPoint,
    SignalKey,
    SignalQuality,
    Source,
    StalenessBasis,
    StalenessInfo,
)

__all__ = [
    "CandumpLogSource",
    "Diagnostic",
    "DiagnosticKind",
    "QualityDecodeResult",
    "QualityPoint",
    "QualityTimeSeries",
    "Selection",
    "SignalKey",
    "SignalQuality",
    "SignalSelector",
    "SignalStatus",
    "Source",
    "StalenessBasis",
    "StalenessEstimator",
    "StalenessInfo",
    "StalenessPolicy",
    "TimeRegressionError",
    "build_series",
    "build_series_from_file",
    "decode_frame_with_quality",
    "decode_message_quality",
    "parse_log_line",
    "parse_timestamp_us",
    "seconds_to_us",
    "select_signals",
    "validate_grid",
    "write_long_csv",
    "write_wide_csv",
]


def build_series(
    database: CanDatabase,
    stream: io.TextIOBase,
    selectors: Sequence[SignalSelector],
    *,
    start_s: float,
    stop_s: float,
    step_s: float,
    explicit_limits_s: Optional[dict[SignalKey, float]] = None,
    policy: Optional[StalenessPolicy] = None,
    decode_choices: bool = True,
    scaling: bool = True,
) -> QualityTimeSeries:
    """Build a quality-aware time series from an open candump log stream.

    Time arguments are seconds (absolute epoch seconds for absolute
    logs); they are snapped to whole microseconds. The stream is read
    at most once and closed by the returned series when it is consumed
    or :meth:`QualityTimeSeries.close` is called -- but note the stream
    is provided by the caller, so only a file opened by
    :func:`build_series_from_file` is owned.
    """
    selections = select_signals(database, list(selectors))
    diagnostics: list[Diagnostic] = []
    source = CandumpLogSource(stream, diagnostics=diagnostics)
    return QualityTimeSeries(
        database,
        source,
        selections,
        start_us=seconds_to_us(start_s, "start_s"),
        stop_us=seconds_to_us(stop_s, "stop_s"),
        step_us=seconds_to_us(step_s, "step_s"),
        explicit_limits_s=explicit_limits_s,
        policy=policy,
        decode_choices=decode_choices,
        scaling=scaling,
    )


def build_series_from_file(
    database_path: str,
    log_path: str,
    selectors: Sequence[SignalSelector],
    *,
    start_s: float,
    stop_s: float,
    step_s: float,
    database_encoding: Optional[str] = None,
    explicit_limits_s: Optional[dict[SignalKey, float]] = None,
    policy: Optional[StalenessPolicy] = None,
    decode_choices: bool = True,
    scaling: bool = True,
) -> QualityTimeSeries:
    """Open a database and a candump log from paths and build the series.

    The log file handle is owned by the returned series and released
    when iteration finishes early or via ``close()`` / context manager.
    """
    dbase = _database.load_file(database_path, encoding=database_encoding)
    from ..database.can.database import Database as CanDatabase
    if not isinstance(dbase, CanDatabase):
        raise ValueError(
            "quality time series require a CAN database (DBC/ARXML/...), "
            "not a diagnostics database"
        )
    selections = select_signals(dbase, list(selectors))
    diagnostics: list[Diagnostic] = []
    source = CandumpLogSource.open(log_path, diagnostics=diagnostics)
    return QualityTimeSeries(
        dbase,
        source,
        selections,
        start_us=seconds_to_us(start_s, "start_s"),
        stop_us=seconds_to_us(stop_s, "stop_s"),
        step_us=seconds_to_us(step_s, "step_s"),
        explicit_limits_s=explicit_limits_s,
        policy=policy,
        decode_choices=decode_choices,
        scaling=scaling,
    )
