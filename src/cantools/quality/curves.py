# Adapt quality-aware time series into plottable line segments.
#
# A signal is never drawn across an unavailable interval: each maximal
# run of FRESH/HELD grid points becomes one segment, and every other
# quality breaks the line. Identities keep their channel and frame
# type, so the same frame id on two channels is always two curves.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..database.namedsignalvalue import NamedSignalValue
from .types import QualityPoint, SignalKey, SignalQuality

_USABLE = frozenset({SignalQuality.FRESH, SignalQuality.HELD})


def _plottable(value):
    """Normalize a decoded value into something matplotlib accepts.

    Enumerated choices arrive as :class:`NamedSignalValue`; they are
    drawn as their stable string name, keeping the same text the detail
    view and CSV report.
    """
    if isinstance(value, NamedSignalValue):
        return str(value)
    return value


@dataclass
class CurveSegment:
    """One contiguous, drawable run of usable grid points."""

    key: SignalKey
    x_us: list[int] = field(default_factory=list)
    y: list[object] = field(default_factory=list)
    # True for the point that coincides with a new observation.
    fresh_markers: list[bool] = field(default_factory=list)


@dataclass
class QualityCurve:
    """Everything the plot layer needs for one selected signal."""

    key: SignalKey
    segments: list[CurveSegment]
    #: Grid points that are unavailable, as (timestamp_us, quality) for
    #: markers/annotation. UNSEEN points are kept separately because they
    #: have no source.
    unavailable: list[tuple[int, SignalQuality, Optional[int]]]
    unseen_x_us: list[int]
    #: Last usable timestamp before expiry, and the grid timestamp at
    #: which expiry was first detected. This lets the curve end exactly
    #: at the staleness limit rather than at the next frame.
    expiry_ends: list[tuple[int, int]]


def build_curve(points: list[QualityPoint]) -> QualityCurve:
    """Group one signal's ordered grid points into drawable segments.

    ``points`` must belong to a single :class:`SignalKey` and be ordered
    by grid time.
    """
    if not points:
        raise ValueError("cannot build a curve from no points")
    key = points[0].key
    for point in points[1:]:
        if point.key != key:
            raise ValueError("all points of a curve must share one key")

    segments: list[CurveSegment] = []
    current: Optional[CurveSegment] = None
    unavailable: list[tuple[int, SignalQuality, Optional[int]]] = []
    unseen_x_us: list[int] = []
    expiry_ends: list[tuple[int, int]] = []
    last_usable_us: Optional[int] = None

    for point in points:
        if point.quality in _USABLE:
            if current is None:
                current = CurveSegment(key=key)
                segments.append(current)
            current.x_us.append(point.timestamp_us)
            current.y.append(_plottable(point.value))
            current.fresh_markers.append(
                point.quality == SignalQuality.FRESH
            )
            last_usable_us = point.timestamp_us
        else:
            if current is not None:
                current = None
            if point.quality == SignalQuality.UNSEEN:
                unseen_x_us.append(point.timestamp_us)
            else:
                source_line = (
                    point.source.line if point.source is not None else None
                )
                unavailable.append(
                    (point.timestamp_us, point.quality, source_line)
                )
            if point.quality == SignalQuality.EXPIRED and last_usable_us is not None:
                expiry_ends.append((last_usable_us, point.timestamp_us))

    return QualityCurve(
        key=key,
        segments=segments,
        unavailable=unavailable,
        unseen_x_us=unseen_x_us,
        expiry_ends=expiry_ends,
    )


def build_curves(
    points_by_key: dict[SignalKey, list[QualityPoint]],
) -> dict[SignalKey, QualityCurve]:
    """Build curves for every signal identity, preserving each one as a
    separate curve."""
    return {
        key: build_curve(points)
        for key, points in points_by_key.items()
    }
