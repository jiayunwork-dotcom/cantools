import argparse
import json
import logging
import sys

from argparse_addons import Integer

from .. import database, logreader
from ..database import MigrationError
from ..logreader import format_candump_log_line
from .__utils__ import format_message_by_frame_id

logging.basicConfig(level=logging.WARNING)


def _migrate_stream(dbase, args):
    # Audit the mapping and both databases completely before a single
    # input line is consumed or a single output line is written.
    target_db = database.load_file(args.migrate_to,
                                   encoding=args.encoding,
                                   frame_id_mask=args.frame_id_mask,
                                   strict=not args.no_strict)
    with open(args.migration_map, encoding='utf-8') as fin:
        mapping = json.load(fin)

    plan = dbase.compile_migration(
        target_db,
        mapping,
        max_abs_error=(args.max_abs_error
                       if args.max_abs_error is not None else 0.0))

    parser = logreader.Parser(sys.stdin)
    line_number = 0
    for raw_line, frame in parser.iterlines(keep_unknowns=True):
        line_number += 1
        if frame is None:
            raise MigrationError(
                f"not a candump -L log entry: {raw_line!r}",
                line_number=line_number)

        try:
            migrated = plan.transcode(frame)
        except MigrationError as exc:
            exc.line_number = line_number
            raise

        sys.stdout.write(format_candump_log_line(migrated) + '\n')
        # Flush every line so an early stop leaves an exact, readable
        # prefix and never a half-written final line.
        sys.stdout.flush()


def _do_decode(args):
    if args.migrate_to is not None or args.migration_map is not None \
            or args.max_abs_error is not None:
        missing = []
        if args.migrate_to is None:
            missing.append('--migrate-to')
        if args.migration_map is None:
            missing.append('--migration-map')
        if missing:
            raise ValueError(
                "migration requires all of --migrate-to and "
                "--migration-map (--max-abs-error is optional); missing: "
                + ', '.join(missing))

    dbase = database.load_file(args.database,
                               encoding=args.encoding,
                               frame_id_mask=args.frame_id_mask,
                               prune_choices=args.prune,
                               strict=not args.no_strict)

    if args.migrate_to is not None:
        try:
            _migrate_stream(dbase, args)
        except (MigrationError, json.JSONDecodeError, OSError,
                database.errors.Error) as exc:
            # Report with the originating log line number when
            # available, then stop. Already flushed output lines form a
            # valid prefix.
            if getattr(exc, 'line_number', None) is not None:
                detail = (f"line {exc.line_number}: {exc}")
            else:
                detail = str(exc)
            print(f"error: {detail}", file=sys.stderr)
            raise SystemExit(1) from None
        return

    decode_choices = not args.no_decode_choices
    decode_containers = not args.no_decode_containers
    allow_truncated = args.no_strict
    allow_excess = args.no_strict
    parser = logreader.Parser(sys.stdin)
    for line, frame in parser.iterlines(keep_unknowns=True):
        if frame is not None:
            line += ' ::'
            line += format_message_by_frame_id(dbase,
                                               frame.frame_id,
                                               frame.data,
                                               decode_choices,
                                               args.single_line,
                                               decode_containers,
                                               allow_truncated=allow_truncated,
                                               allow_excess=allow_excess)

        print(line)


def add_subparser(subparsers):
    decode_parser = subparsers.add_parser(
        'decode',
        description=('Decode "candump" CAN frames read from standard input '
                     'and print them in a human readable format.'),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    decode_parser.add_argument(
        '-c', '--no-decode-choices',
        action='store_true',
        help='Do not convert scaled values to choice strings.')
    decode_parser.add_argument(
        '-t', '--no-decode-containers',
        action='store_true',
        help='Do not decode container messages.')
    decode_parser.add_argument(
        '-s', '--single-line',
        action='store_true',
        help='Print the decoded message on a single line.')
    decode_parser.add_argument(
        '-e', '--encoding',
        help='File encoding.')
    decode_parser.add_argument(
        '--prune',
        action='store_true',
        help='Try to shorten the names of named signal choices.')
    decode_parser.add_argument(
        '--no-strict',
        action='store_true',
        help='Skip database consistency checks.')
    decode_parser.add_argument(
        '-m', '--frame-id-mask',
        type=Integer(0),
        help=('Only compare selected frame id bits to find the message in the '
              'database. By default the candump and database frame ids must '
              'be equal for a match.'))
    decode_parser.add_argument(
        '--migrate-to',
        metavar='TARGET_DB',
        help=('Migrate frames to the layout of this target database '
              'instead of annotating them. Requires --migration-map; '
              'stdin must be candump -L and stdout is written as '
              'candump -L.'))
    decode_parser.add_argument(
        '--migration-map',
        metavar='FILE',
        help=('JSON file describing the source-to-target message and '
              'signal mapping. Only used together with --migrate-to.'))
    decode_parser.add_argument(
        '--max-abs-error',
        type=float,
        default=None,
        help=('Per-signal absolute error budget in physical units for '
              'numeric migration. Only used together with --migrate-to '
              'and --migration-map.'))
    decode_parser.add_argument(
        'database',
        help='Database file.')
    decode_parser.set_defaults(func=_do_decode)
