# Streaming CSV export of quality-aware grid points.
#
# The writer consumes a :class:`QualityTimeSeries` lazily: it never
# materializes more than the current grid batch, so memory stays bounded
# by the number of selected signals (plus, in wide mode, one output row
# per grid point as it is flushed).

from __future__ import annotations

import csv
import io
from collections.abc import Iterator

from ..database.namedsignalvalue import NamedSignalValue
from .engine import QualityTimeSeries
from .types import QualityPoint, SignalKey

LONG_HEADER = [
    "timestamp_s",
    "channel",
    "frame_id",
    "frame_type",
    "message",
    "signal",
    "quality",
    "value",
    "raw_value",
    "age_s",
    "staleness_limit_s",
    "staleness_basis",
    "staleness_explanation",
    "source_line",
    "source_timestamp_s",
]


def _frame_type(key: SignalKey) -> str:
    return "extended" if key.is_extended_frame else "standard"


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, NamedSignalValue):
        return str(value)
    return str(value)


def _point_row(point: QualityPoint) -> list[str]:
    key = point.key
    limit = point.staleness.limit_us
    return [
        f"{point.timestamp_us / 1_000_000:.6f}",
        key.channel,
        f"0x{key.frame_id:X}",
        _frame_type(key),
        key.message_name,
        key.signal_name,
        point.quality.value,
        _scalar(point.value),
        "" if point.raw_value is None else str(point.raw_value),
        "" if point.age_us is None else f"{point.age_us / 1_000_000:.6f}",
        "" if limit is None else f"{limit / 1_000_000:.6f}",
        point.staleness.basis.value,
        point.staleness.explanation,
        "" if point.source is None else str(point.source.line),
        "" if point.source_timestamp_us is None
        else f"{point.source_timestamp_us / 1_000_000:.6f}",
    ]


def write_long_csv(series: QualityTimeSeries,
                   stream: io.TextIOBase) -> int:
    """Write one row per signal per grid point (long/tidy format).

    Returns the number of data rows written.
    """
    writer = csv.writer(stream)
    writer.writerow(LONG_HEADER)
    rows = 0
    for batch in series.iter_batches():
        for point in batch:
            writer.writerow(_point_row(point))
            rows += 1
    return rows


def write_wide_csv(series: QualityTimeSeries,
                   stream: io.TextIOBase) -> int:
    """Write one row per grid point with one column group per signal.

    Each selected signal expands into ``<key>.quality`` /
    ``<key>.value`` / ``<key>.source_line`` columns, so identity and
    quality survive the wide layout. Returns the number of data rows.
    """
    keys = [state.key for state in series._states.values()]
    header: list[str] = ["timestamp_s"]
    for key in keys:
        prefix = str(key)
        header.extend([
            f"{prefix}.quality",
            f"{prefix}.value",
            f"{prefix}.raw_value",
            f"{prefix}.age_s",
            f"{prefix}.limit_s",
            f"{prefix}.basis",
            f"{prefix}.source_line",
        ])

    writer = csv.writer(stream)
    writer.writerow(header)
    rows = 0
    for batch in series.iter_batches():
        by_key = {point.key: point for point in batch}
        row: list[str] = [f"{batch[0].timestamp_us / 1_000_000:.6f}"]
        for key in keys:
            point = by_key[key]
            row.extend([
                point.quality.value,
                _scalar(point.value),
                "" if point.raw_value is None else str(point.raw_value),
                "" if point.age_us is None
                else f"{point.age_us / 1_000_000:.6f}",
                "" if point.staleness.limit_us is None
                else f"{point.staleness.limit_us / 1_000_000:.6f}",
                point.staleness.basis.value,
                "" if point.source is None else str(point.source.line),
            ])
        writer.writerow(row)
        rows += 1
    return rows


def iter_csv_rows(series: QualityTimeSeries) -> Iterator[list[str]]:
    """Convenience iterator yielding long-format rows including the
    header, still streaming."""
    yield LONG_HEADER
    for batch in series.iter_batches():
        for point in batch:
            yield _point_row(point)
