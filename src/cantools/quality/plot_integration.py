# Glue between the quality time series engine and matplotlib.
#
# The legacy plot path is untouched. This module is imported lazily only
# when ``cantools plot --quality`` runs, so importing cantools without
# matplotlib still works.

from __future__ import annotations

import csv as _csv
import datetime
import io
from collections import OrderedDict
from typing import Optional

from .. import quality
from ..quality.csv_export import LONG_HEADER, _point_row
from ..quality.curves import QualityCurve, build_curves
from ..quality.staleness import US_PER_S
from ..subparsers.plot import QUALITY_COLORS, plt


def _parse_staleness(specs: list[str],
                     selections: list[quality.Selection]
                     ) -> dict[quality.SignalKey, float]:
    """Parse ``Message.Signal=seconds`` into a SignalKey -> seconds map."""
    by_message_signal = {
        (selection.message_name, selection.signal_name): selection.key
        for selection in selections
    }

    limits: dict[quality.SignalKey, float] = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(
                f"--staleness expects MESSAGE.SIGNAL=SECONDS, got {spec!r}"
            )
        target, _, seconds_text = spec.partition("=")
        if "." not in target:
            raise ValueError(
                f"--staleness target must be MESSAGE.SIGNAL, got {target!r}"
            )
        message_name, _, signal_name = target.partition(".")
        key = by_message_signal.get((message_name, signal_name))
        if key is None:
            raise ValueError(
                f"--staleness refers to unselected signal {target!r}"
            )
        try:
            limits[key] = float(seconds_text)
        except ValueError as e:
            raise ValueError(
                f"--staleness seconds for {target!r} must be a number"
            ) from e
    return limits  # the estimator validates finiteness/positivity


def _select_all_signals(dbase, channels: Optional[set[str]]):
    """Build selectors for every database signal on the requested
    channels. An empty channel set means every channel seen in the log;
    the engine matches frames by their actual channel name."""
    channel_list = sorted(channels) if channels else [""]
    selectors = []
    for message in dbase.messages:
        for signal in message.signals:
            for channel in channel_list:
                selectors.append(quality.SignalSelector(
                    channel=channel,
                    message_name=message.name,
                    signal_name=signal.name,
                    is_extended_frame=message.is_extended_frame,
                ))
    return selectors


def run_quality_plot(dbase, stdin: io.TextIOBase, args) -> None:
    if plt is None:
        from .plot import MatplotlibNotInstalledError
        raise MatplotlibNotInstalledError()

    if args.grid_start is None or args.grid_stop is None \
            or args.grid_step is None:
        raise ValueError(
            "--quality requires --grid-start, --grid-stop and --grid-step"
        )
    if args.grid_stop < args.grid_start:
        raise ValueError("--grid-stop must not be earlier than --grid-start")
    if args.grid_step <= 0:
        raise ValueError("--grid-step must be a positive number of seconds")

    channels = set(args.channel) if args.channel else None
    selectors = _select_all_signals(dbase, channels)
    selections = quality.select_signals(dbase, selectors)
    explicit = _parse_staleness(args.staleness, selections)

    diagnostics: list[quality.Diagnostic] = []
    source = quality.CandumpLogSource(stdin, diagnostics=diagnostics)
    series = quality.QualityTimeSeries(
        dbase,
        source,
        selections,
        start_us=quality.seconds_to_us(args.grid_start, "grid_start"),
        stop_us=quality.seconds_to_us(args.grid_stop, "grid_stop"),
        step_us=quality.seconds_to_us(args.grid_step, "grid_step"),
        explicit_limits_s=explicit,
        decode_choices=not args.no_decode_choices,
    )

    # Plot is allowed to retain the final selected points; the engine
    # itself remains single-pass and bounded while producing them.
    points_by_key: OrderedDict[quality.SignalKey,
                               list[quality.QualityPoint]] = OrderedDict()
    csv_stream = open(args.quality_csv, "w", encoding="utf-8", newline="") \
        if args.quality_csv else None
    try:
        writer = _csv.writer(csv_stream) if csv_stream is not None else None
        if writer is not None:
            writer.writerow(LONG_HEADER)
        for batch in series.iter_batches():
            for point in batch:
                points_by_key.setdefault(point.key, []).append(point)
                if writer is not None:
                    writer.writerow(_point_row(point))
    finally:
        if csv_stream is not None:
            csv_stream.close()

    curves = build_curves(points_by_key)
    _draw(curves, diagnostics, args)


def _to_x(timestamp_us: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(
        timestamp_us / US_PER_S, tz=datetime.timezone.utc
    )


def _draw(curves: dict, diagnostics, args) -> None:
    figure = plt.figure()
    names = sorted({key.signal_name for key in curves})
    if not any(curve.segments for curve in curves.values()):
        print("WARNING: quality mode found no usable signal values to plot.")

    for index, signal_name in enumerate(names, start=1):
        axes = figure.add_subplot(len(names), 1, index)
        related = {
            key: curve for key, curve in curves.items()
            if key.signal_name == signal_name
        }
        categorical = _is_categorical(related)
        category_index: Optional[dict[str, int]] = (
            _category_index(related) if categorical else None
        )
        for key, curve in related.items():
            _draw_curve(axes, key, curve, categorical, category_index)
        if categorical and category_index is not None:
            axes.set_yticks(list(category_index.values()),
                            labels=list(category_index.keys()))
        axes.set_title(signal_name)
        axes.set_xlabel("absolute time (UTC)")
        axes.legend(fontsize="small", loc="best")

    if diagnostics and not args.quiet:
        for diag in diagnostics:
            print(f"line {diag.line}: {diag.kind.value}: {diag.message}")

    figure.tight_layout()
    if args.output_file:
        plt.savefig(args.output_file)
        print(f"Result written to {args.output_file}")
    else:
        plt.show()


def _is_categorical(related: dict) -> bool:
    for curve in related.values():
        for segment in curve.segments:
            if any(isinstance(y, str) for y in segment.y):
                return True
    return False


def _category_index(related: dict) -> dict[str, int]:
    categories: dict[str, int] = {}
    for curve in related.values():
        for segment in curve.segments:
            for y in segment.y:
                label = str(y)
                if label not in categories:
                    categories[label] = len(categories)
    return categories


def _draw_curve(axes, key: quality.SignalKey, curve: QualityCurve,
                categorical: bool, category_index: Optional[dict]) -> None:
    label = f"{key.channel}:{key.message_name}.{key.signal_name}"

    def y_value(y):
        if categorical and category_index is not None:
            return category_index[str(y)]
        return y

    for segment_index, segment in enumerate(curve.segments):
        xs = [_to_x(us) for us in segment.x_us]
        ys = [y_value(y) for y in segment.y]
        axes.plot(
            xs,
            ys,
            color=QUALITY_COLORS["fresh"],
            linewidth=1.5,
            label=label if segment_index == 0 else None,
        )
        fresh_x = [
            xs[i] for i, is_fresh in enumerate(segment.fresh_markers)
            if is_fresh
        ]
        fresh_y = [
            ys[i] for i, is_fresh in enumerate(segment.fresh_markers)
            if is_fresh
        ]
        if fresh_x:
            axes.scatter(fresh_x, fresh_y, s=18,
                         color=QUALITY_COLORS["fresh"], zorder=3)

    labelled: set[str] = set()
    for timestamp_us, quality_kind, _line in curve.unavailable:
        name = quality_kind.value
        axes.axvline(
            _to_x(timestamp_us),
            color=QUALITY_COLORS.get(name, "#555555"),
            linewidth=0.8, alpha=0.6, linestyle="--",
            label=name if name not in labelled else None,
        )
        labelled.add(name)
