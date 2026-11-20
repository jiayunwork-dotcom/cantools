"""Command line entry: stream a candump -L log into a quality CSV."""

import argparse

from .. import database, quality
from ..quality.csv_export import write_long_csv, write_wide_csv
from .plot import Integer


def _do_export(args):
    dbase = database.load_file(
        args.database,
        encoding=args.encoding,
        frame_id_mask=args.frame_id_mask,
        prune_choices=args.prune,
        strict=not args.no_strict,
    )

    selectors = []
    for spec in args.signals:
        if "." not in spec:
            raise ValueError(
                f"signal must be MESSAGE.SIGNAL, got {spec!r}"
            )
        message_name, _, signal_name = spec.partition(".")
        selectors.append(quality.SignalSelector(
            channel=args.channel,
            message_name=message_name,
            signal_name=signal_name,
        ))
    if not selectors:
        raise ValueError("at least one MESSAGE.SIGNAL must be selected")

    selections = quality.select_signals(dbase, selectors)

    explicit: dict = {}
    for spec in args.staleness:
        if "=" not in spec:
            raise ValueError(
                f"--staleness expects MESSAGE.SIGNAL=SECONDS, got {spec!r}"
            )
        target, _, seconds_text = spec.partition("=")
        message_name, _, signal_name = target.partition(".")
        match = [
            sel for sel in selections
            if sel.message_name == message_name
            and sel.signal_name == signal_name
        ]
        if not match:
            raise ValueError(f"--staleness refers to unselected {target!r}")
        explicit[match[0].key] = float(seconds_text)

    policy = quality.StalenessPolicy(
        jitter_factor=args.jitter_factor,
        min_evidence=args.min_evidence,
        history_size=args.history_size,
        event_multiplier=args.event_multiplier,
        expire_without_evidence=not args.no_expire_without_evidence,
        default_event_limit_s=args.default_event_limit,
    )

    diagnostics: list[quality.Diagnostic] = []
    source = quality.CandumpLogSource(args.input, diagnostics=diagnostics)
    series = quality.QualityTimeSeries(
        dbase,
        source,
        selections,
        start_us=quality.seconds_to_us(args.grid_start, "grid_start"),
        stop_us=quality.seconds_to_us(args.grid_stop, "grid_stop"),
        step_us=quality.seconds_to_us(args.grid_step, "grid_step"),
        explicit_limits_s=explicit,
        policy=policy,
        decode_choices=not args.no_decode_choices,
    )

    if args.wide:
        rows = write_wide_csv(series, args.output)
    else:
        rows = write_long_csv(series, args.output)

    if not args.quiet:
        for diag in diagnostics:
            print(f"line {diag.line}: {diag.kind.value}: {diag.message}")
    print(f"wrote {rows} quality rows to {args.output.name}")


def add_subparser(subparsers):
    parser = subparsers.add_parser(
        "export-quality",
        description=(
            'Stream an absolute-time "candump -L" log into a quality-aware '
            "signal CSV on a fixed microsecond grid."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("database", help="DBC/ARXML database file.")
    parser.add_argument(
        "signals",
        nargs="+",
        help="Signals to export as MESSAGE.SIGNAL (repeated).",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Log channel name the signals are read from.",
    )
    parser.add_argument("--grid-start", type=float, required=True,
                        help="Grid start in absolute epoch seconds.")
    parser.add_argument("--grid-stop", type=float, required=True,
                        help="Grid stop in absolute epoch seconds.")
    parser.add_argument("--grid-step", type=float, required=True,
                        help="Grid step in seconds (positive).")
    parser.add_argument(
        "--staleness",
        action="append",
        default=[],
        metavar="MESSAGE.SIGNAL=SECONDS",
        help="Explicit per-signal staleness limit (repeatable).",
    )
    parser.add_argument("--wide", action="store_true",
                        help="Write one column group per signal instead of "
                             "the default long/tidy layout.")
    parser.add_argument("--jitter-factor", type=float, default=2.0)
    parser.add_argument("--min-evidence", type=int, default=3)
    parser.add_argument("--history-size", type=int, default=16)
    parser.add_argument("--event-multiplier", type=float, default=3.0)
    parser.add_argument("--default-event-limit", type=float, default=3600.0)
    parser.add_argument("--no-expire-without-evidence", action="store_true")
    parser.add_argument("--no-decode-choices", action="store_true")
    parser.add_argument("--no-strict", action="store_true")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("-e", "--encoding")
    parser.add_argument("-m", "--frame-id-mask", type=Integer(0))
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument(
        "-o", "--output",
        type=argparse.FileType("w", encoding="utf-8"),
        default=argparse.FileType("w", encoding="utf-8")("-"),
        help="Output CSV path (default stdout).",
    )
    parser.add_argument(
        "--input",
        type=argparse.FileType("r", encoding="utf-8"),
        default=argparse.FileType("r", encoding="utf-8")("-"),
        help="Input candump -L log (default stdin).",
    )
    parser.set_defaults(func=_do_export)
    return parser
