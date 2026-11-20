"""Regression tests for frame identity resolution.

A CAN frame is identified by its arbitration id *and* its frame type.
These tests pin down that the database index, ``decode_message`` /
``encode_message``, the ``decode`` command, the legacy plot path and the
quality path all agree on one identity, and that a partial
``frame_id_mask`` only selects arbitration id bits.

All truth values are written directly from the input message
definitions, not read back through the code under test.
"""

from __future__ import annotations

import io
import logging
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import cantools
import cantools.database
from cantools import logreader
from cantools.database.can.database import Database
from cantools.database.can.frame_identity import (
    EXTENDED_FRAME_ID_MAX,
    STANDARD_FRAME_ID_MAX,
    mask_frame_id,
    normalize_frame_id_mask,
    resolve_frame_identity,
)
from cantools.database.can.message import Message
from cantools.database.can.signal import Signal
from cantools.database.errors import AmbiguousFrameIdError
from cantools.quality import (
    CandumpLogSource,
    DiagnosticKind,
    QualityTimeSeries,
    SignalSelector,
    select_signals,
)

FILES = os.path.join(os.path.dirname(__file__), 'files', 'frame_identity')
SAME_ID_DBC = os.path.join(FILES, 'same_id.dbc')
SAME_ID_LOG = os.path.join(FILES, 'same_id.log')
ONLY_EXTENDED_DBC = os.path.join(FILES, 'only_extended.dbc')
ONLY_STANDARD_DBC = os.path.join(FILES, 'only_standard.dbc')
STANDARD_FRAME_LOG = os.path.join(FILES, 'standard_frame.log')
EXTENDED_FRAME_LOG = os.path.join(FILES, 'extended_frame.log')
EXTENDED_SMALL_ID_LOG = os.path.join(FILES, 'extended_small_id.log')
MASKED_DBC = os.path.join(FILES, 'masked.dbc')
MASKED_LOG = os.path.join(FILES, 'masked.log')
MASKED_CONFLICT_DBC = os.path.join(FILES, 'masked_conflict.dbc')
MASKED_CONFLICT_REVERSED_DBC = os.path.join(
    FILES, 'masked_conflict_reversed.dbc')
SAME_LAYOUT_DBC = os.path.join(FILES, 'same_layout.dbc')
SAME_NAME_CONFLICT_DBC = os.path.join(FILES, 'same_name_conflict.dbc')


def read(path):
    with open(path, encoding='utf-8') as fin:
        return fin.read()


def frames(path):
    return list(logreader.Parser(io.StringIO(read(path))))


def run_cli(argv, stdin_text):
    stdout = io.StringIO()
    with mock.patch('sys.stdin', io.StringIO(stdin_text)), \
            mock.patch('sys.stdout', stdout), \
            mock.patch('sys.argv', argv):
        cantools._main()
    return stdout.getvalue()


def make_message(frame_id, name, length=8, extended=False):
    signal = Signal('S', start=0, length=8)
    message = Message(frame_id=frame_id,
                      name=name,
                      length=length,
                      signals=[signal],
                      is_extended_frame=extended)
    return message


class IdentityHelperTests(unittest.TestCase):
    """Boundaries of legal ids, masks and frame type inference."""

    def test_legal_boundaries(self):
        self.assertEqual(STANDARD_FRAME_ID_MAX, 0x7FF)
        self.assertEqual(EXTENDED_FRAME_ID_MAX, 0x1FFFFFFF)

    def test_default_mask_keeps_every_arbitration_id_bit(self):
        self.assertEqual(normalize_frame_id_mask(None), 0x1FFFFFFF)
        # A legacy all-ones mask must not leak the DBC frame type flag.
        self.assertEqual(normalize_frame_id_mask(0xFFFFFFFF), 0x1FFFFFFF)

    def test_mask_only_selects_arbitration_id_bits(self):
        mask = 0x1FFFFF00
        self.assertEqual(mask_frame_id(0x18FF0101, mask), 0x18FF0100)
        self.assertEqual(mask_frame_id(0x18FF0102, mask), 0x18FF0100)
        self.assertEqual(mask_frame_id(0x18FF0101, 0x0), 0x0)
        self.assertEqual(mask_frame_id(0x18FF0101, 0x1FFFFFFF), 0x18FF0101)

    def test_numeric_inference_is_kept_by_default(self):
        self.assertEqual(resolve_frame_identity(0x123), (0x123, False))
        self.assertEqual(resolve_frame_identity(0x7FF), (0x7FF, False))
        self.assertEqual(resolve_frame_identity(0x800), (0x800, True))
        self.assertEqual(
            resolve_frame_identity(0x123, force_extended_id=True),
            (0x123, True))

    def test_explicit_frame_type_wins_for_small_extended_ids(self):
        self.assertEqual(
            resolve_frame_identity(0x123, is_extended_frame=True),
            (0x123, True))
        self.assertEqual(
            resolve_frame_identity(0x7FF, is_extended_frame=False),
            (0x7FF, False))

    def test_dbc_frame_type_flag_is_not_an_arbitration_id(self):
        # 0x80000000 is the DBC file flag, not part of the id.
        self.assertEqual(
            resolve_frame_identity(0x80000000 | 0x123),
            (0x123, True))

    def test_conflicting_explicit_and_forced_type_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_frame_identity(0x123,
                                   force_extended_id=True,
                                   is_extended_frame=False)


class SameNumericIdTests(unittest.TestCase):
    """Standard and extended messages with the same numeric id."""

    @classmethod
    def setUpClass(cls):
        cls.db = cantools.database.load_file(SAME_ID_DBC)

    def test_dbc_loader_keeps_type_out_of_the_id(self):
        messages = {message.name: message for message in self.db.messages}
        # BO_ 291 and BO_ 2147483939 both carry arbitration id 0x123.
        self.assertEqual(messages['Standard'].frame_id, 0x123)
        self.assertFalse(messages['Standard'].is_extended_frame)
        self.assertEqual(messages['Extended'].frame_id, 0x123)
        self.assertTrue(messages['Extended'].is_extended_frame)

    def test_log_reader_reports_the_wire_frame_type(self):
        parsed = frames(SAME_ID_LOG)
        self.assertEqual([(frame.frame_id, frame.is_extended_frame)
                          for frame in parsed],
                         [(0x123, False), (0x123, True)])

    def test_lookup_selects_by_frame_type(self):
        self.assertEqual(self.db.get_message_by_frame_id(0x123).name,
                         'Standard')
        self.assertEqual(
            self.db.get_message_by_frame_id(
                0x123, force_extended_id=True).name,
            'Extended')
        self.assertEqual(
            self.db.get_message_by_frame_id(
                0x123, is_extended_frame=True).name,
            'Extended')
        # The DBC file flag is accepted on input but stripped.
        self.assertEqual(
            self.db.get_message_by_frame_id(0x80000000 | 0x123).name,
            'Extended')

    def test_encode_and_decode_select_by_frame_type(self):
        standard_data = bytes([5]) + bytes(7)
        extended_data = bytes([6]) + bytes(7)

        self.assertEqual(self.db.decode_message(0x123, standard_data),
                         {'S': 5})
        self.assertEqual(
            self.db.decode_message(0x123, extended_data,
                                   force_extended_id=True),
            {'E': 60})
        self.assertEqual(
            self.db.encode_message(0x123, {'S': 5}), standard_data)
        self.assertEqual(
            self.db.encode_message(0x123, {'E': 60},
                                   force_extended_id=True),
            extended_data)

    def test_decode_cli_uses_the_log_frame_type(self):
        output = run_cli(
            ['cantools', 'decode', '-s', SAME_ID_DBC], read(SAME_ID_LOG))
        self.assertEqual(output,
                         '(1700000000.000001) can0 123#0500000000000000 :: '
                         'Standard(S: 5)\n'
                         '(1700000000.100001) can0 00000123#0600000000000000 '
                         ':: Extended(E: 60)\n')

    def test_decode_cli_extended_small_id_is_extended(self):
        output = run_cli(
            ['cantools', 'decode', '-s', SAME_ID_DBC],
            read(EXTENDED_SMALL_ID_LOG))
        self.assertEqual(output,
                         '(1700000000.000001) can0 00000123#0600000000000000 '
                         ':: Extended(E: 60)\n'
                         '(1700000000.100001) can0 123#0500000000000000 :: '
                         'Standard(S: 5)\n')

    def test_plot_records_each_type_on_its_own_signal(self):
        plotter, _ = record_plot(self.db, SAME_ID_LOG)
        self.assertEqual(sorted(plotter.signals.values), ['Extended.E',
                                                          'Standard.S'])
        self.assertEqual(plotter.signals.values['Standard.S'].y, [5])
        self.assertEqual(plotter.signals.values['Extended.E'].y, [60])


class OneFrameTypeOnlyTests(unittest.TestCase):
    """A missing frame type must be unknown, never borrowed."""

    def test_existing_extended_definition_does_not_serve_standard(self):
        db = cantools.database.load_file(ONLY_EXTENDED_DBC)
        self.assertEqual(
            db.get_message_by_frame_id(0x123,
                                       is_extended_frame=True).name,
            'Extended')
        with self.assertRaises(KeyError):
            db.get_message_by_frame_id(0x123, is_extended_frame=False)

    def test_decode_cli_reports_unknown_instead_of_borrowing(self):
        output = run_cli(
            ['cantools', 'decode', '-s', ONLY_EXTENDED_DBC],
            read(STANDARD_FRAME_LOG))
        self.assertEqual(
            output,
            '(1700000000.000001) can0 123#0600000000000000 :: '
            'Unknown frame id 291 (0x123)\n')

    def test_plot_reports_unknown_instead_of_borrowing(self):
        db = cantools.database.load_file(ONLY_EXTENDED_DBC)
        plotter, output = record_plot(db, STANDARD_FRAME_LOG)
        self.assertEqual(plotter.signals.values, {})
        self.assertIn('Unknown frame id 291 (0x123)', output)

    def test_existing_standard_definition_does_not_serve_extended(self):
        output = run_cli(
            ['cantools', 'decode', '-s', ONLY_STANDARD_DBC],
            read(EXTENDED_FRAME_LOG))
        self.assertEqual(
            output,
            '(1700000000.000001) can0 00000123#0500000000000000 :: '
            'Unknown frame id 291 (0x123)\n')


class MaskTests(unittest.TestCase):
    """Masked lookup, encode/decode and conflicts."""

    def test_mask_hit_matches_lookup_encode_and_decode(self):
        db = cantools.database.load_file(MASKED_DBC,
                                         frame_id_mask=0x1FFFFF00)
        message = db.get_message_by_frame_id(0x18FF0102,
                                             is_extended_frame=True)
        self.assertEqual(message.name, 'J1939PG')
        self.assertEqual(
            db.get_message_by_frame_id(0x18FF0102,
                                       force_extended_id=True).name,
            'J1939PG')
        encoded = db.encode_message(0x18FF0102, {'V': 1},
                                    force_extended_id=True)
        self.assertEqual(encoded, bytes([1]) + bytes(7))
        self.assertEqual(
            db.decode_message(0x18FF0102, encoded,
                              force_extended_id=True),
            {'V': 1})

    def test_mask_miss_is_unknown(self):
        db = cantools.database.load_file(MASKED_DBC,
                                         frame_id_mask=0x1FFFFF00)
        with self.assertRaises(KeyError):
            db.get_message_by_frame_id(0x18FF0202, is_extended_frame=True)

    def test_mask_zero_still_separates_frame_types(self):
        db = cantools.database.load_file(SAME_ID_DBC, frame_id_mask=0)
        self.assertEqual(
            db.get_message_by_frame_id(0x123,
                                       is_extended_frame=False).name,
            'Standard')
        self.assertEqual(
            db.get_message_by_frame_id(0x123,
                                       is_extended_frame=True).name,
            'Extended')
        # Any id of that frame type now resolves to the same definition.
        self.assertEqual(
            db.get_message_by_frame_id(0x7FF,
                                       is_extended_frame=False).name,
            'Standard')

    def test_different_layouts_are_an_ambiguity_not_last_wins(self):
        for path in (MASKED_CONFLICT_DBC, MASKED_CONFLICT_REVERSED_DBC):
            db = cantools.database.load_file(path,
                                             frame_id_mask=0x1FFFFF00)
            with self.assertRaises(AmbiguousFrameIdError) as ctx:
                db.get_message_by_frame_id(0x18FF0102,
                                           is_extended_frame=True)
            names = sorted(message.name for message in ctx.exception.messages)
            self.assertEqual(names, ['J1939PG', 'OtherPG'])
            self.assertEqual(ctx.exception.frame_id_mask, 0x1FFFFF00)
            self.assertTrue(ctx.exception.is_extended_frame)
            text = str(ctx.exception)
            self.assertIn('J1939PG', text)
            self.assertIn('OtherPG', text)
            self.assertIn('extended', text)
            self.assertIn('0x1fffff00', text)

    def test_decode_cli_reports_ambiguity(self):
        output = run_cli(
            ['cantools', 'decode', '-s', '-m', '0x1FFFFF00',
             MASKED_CONFLICT_DBC],
            read(MASKED_LOG))
        self.assertIn('ambiguous extended frame id', output)
        self.assertIn('J1939PG', output)
        self.assertIn('OtherPG', output)
        self.assertIn('0x1fffff00', output)

    def test_same_layout_may_share_the_identity(self):
        db = cantools.database.load_file(SAME_LAYOUT_DBC,
                                         frame_id_mask=0x1FFFFF00)
        message = db.get_message_by_frame_id(0x18FF0102,
                                             is_extended_frame=True)
        # Both definitions are provably identical, so sharing is safe.
        self.assertIn(message.name, ('Alpha', 'AlphaAlias'))
        self.assertEqual(
            db.decode_message(0x18FF0102, bytes([7]) + bytes(7),
                              force_extended_id=True),
            {'V': 7})

    def test_same_name_is_not_proof_of_same_layout(self):
        db = cantools.database.load_file(SAME_NAME_CONFLICT_DBC,
                                         frame_id_mask=0x1FFFFF00)
        # Both definitions have the same name but different layouts.
        self.assertEqual(len([m for m in db.messages
                              if m.name == 'SharedName']), 2)
        with self.assertRaises(AmbiguousFrameIdError):
            db.get_message_by_frame_id(0x18FF0102, is_extended_frame=True)


class AppendAndRefreshTests(unittest.TestCase):

    def test_append_conflict_is_not_resolved_by_load_order(self):
        db = Database(frame_id_mask=0x1FFFFF00)
        db.add_dbc_string(read(MASKED_DBC))
        self.assertEqual(
            db.get_message_by_frame_id(0x18FF0102,
                                       is_extended_frame=True).name,
            'J1939PG')
        # Load the same definitions again plus a second, different one.
        db.add_dbc_string(read(MASKED_CONFLICT_DBC))
        with self.assertRaises(AmbiguousFrameIdError):
            db.get_message_by_frame_id(0x18FF0102, is_extended_frame=True)

    def test_failed_refresh_keeps_the_previous_index(self):
        db = cantools.database.load_file(SAME_ID_DBC)
        standard = db.get_message_by_frame_id(0x123)
        original_length = standard.signals[0].length

        standard.signals[0].length = 0
        with self.assertRaises(cantools.database.Error):
            db.refresh()

        # The index still resolves, and still resolves the right message
        # rather than a partially rebuilt one.
        self.assertIs(db.get_message_by_frame_id(0x123), standard)
        self.assertIs(db.get_message_by_frame_id(0x123,
                                                 force_extended_id=True),
                      db.get_message_by_name('Extended'))

        standard.signals[0].length = original_length
        db.refresh()
        self.assertIs(db.get_message_by_frame_id(0x123), standard)

    def test_failed_append_is_rolled_back(self):
        db = cantools.database.load_file(ONLY_STANDARD_DBC)
        message_count = len(db.messages)
        with self.assertRaises(cantools.database.Error):
            # A zero-length signal is rejected by the message refresh.
            db.add_dbc_string(
                'BO_ 4 Broken: 8 ECU\n SG_ X : 0|0@1+ (1,0) [0|0] "" ECU\n')
        self.assertEqual(len(db.messages), message_count)
        self.assertEqual(db.get_message_by_frame_id(0x123).name,
                         'Standard')
        with self.assertRaises(KeyError):
            db.get_message_by_frame_id(4)

    def test_refresh_follows_the_conflict_policy(self):
        db = Database(frame_id_mask=0x1FFFFF00)
        db.add_dbc_string(read(MASKED_CONFLICT_DBC))
        with self.assertRaises(AmbiguousFrameIdError):
            db.get_message_by_frame_id(0x18FF0102, is_extended_frame=True)

        # Editing one layout back to agreement resolves the conflict.
        other = db.get_message_by_name('OtherPG')
        other.signals[0].start = 0
        other.signals[0].name = 'V'
        db.refresh()
        self.assertEqual(
            db.get_message_by_frame_id(0x18FF0102,
                                       is_extended_frame=True).name,
            'OtherPG')


class IndexCostTests(unittest.TestCase):
    """Lookups use the built index and repeated refreshes stay bounded."""

    def test_lookup_does_not_iterate_messages(self):
        db = cantools.database.load_file(SAME_ID_DBC)

        class GuardedList(list):
            def __iter__(self):
                raise AssertionError('lookup iterated all messages')

        guarded = GuardedList(db.messages)
        db._messages = guarded
        self.assertEqual(db.get_message_by_frame_id(0x123).name, 'Standard')
        self.assertEqual(db.get_message_by_frame_id(0x123,
                                                    force_extended_id=True).name,
                         'Extended')

    def test_repeated_refresh_does_not_grow_conflict_state(self):
        db = cantools.database.load_file(MASKED_CONFLICT_DBC,
                                         frame_id_mask=0x1FFFFF00)
        sizes = []
        for _ in range(5):
            db.refresh()
            sizes.append((len(db._frame_id_to_message),
                          len(db._ambiguous_frame_ids)))
        self.assertEqual(len(set(sizes)), 1)
        self.assertEqual(sizes[0], (0, 1))

    def test_index_is_one_entry_per_identity(self):
        db = cantools.database.load_file(SAME_ID_DBC)
        self.assertEqual(len(db._frame_id_to_message), 2)
        self.assertEqual(db._ambiguous_frame_ids, {})


class LegalIdBoundaryTests(unittest.TestCase):

    def test_standard_and_extended_boundaries(self):
        db = Database()
        db._messages = [make_message(0x7FF, 'StandardMax'),
                        make_message(0x1FFFFFFF, 'ExtendedMax',
                                     extended=True)]
        db.refresh()
        self.assertEqual(db.get_message_by_frame_id(0x7FF).name,
                         'StandardMax')
        self.assertEqual(
            db.get_message_by_frame_id(0x1FFFFFFF,
                                       is_extended_frame=True).name,
            'ExtendedMax')

    def test_illegal_id_is_not_indexed_but_kept_by_name(self):
        db = Database()
        # The Message constructor rejects this; simulate a message whose
        # id was changed through the public setter afterwards.
        too_big = make_message(0x100, 'TooBig', extended=True)
        too_big.frame_id = 0x20000000
        db._messages = [too_big]
        with self.assertLogs(level=logging.WARNING):
            db.refresh()
        self.assertEqual(db.get_message_by_name('TooBig').name, 'TooBig')
        with self.assertRaises(KeyError):
            db.get_message_by_frame_id(0x20000000,
                                       is_extended_frame=True)


class QualityPathTests(unittest.TestCase):

    def test_quality_selects_by_frame_type(self):
        db = cantools.database.load_file(SAME_ID_DBC)
        selections = select_signals(
            db, [SignalSelector('can0', 'Extended', 'E', True)])
        self.assertEqual(selections[0].is_extended_frame, True)

        source = CandumpLogSource(io.StringIO(read(SAME_ID_LOG)))
        series = QualityTimeSeries(
            db, source, selections,
            start_us=1_700_000_000_000_001,
            stop_us=1_700_000_000_100_001,
            step_us=100_000)
        points = [point for batch in series.iter_batches()
                  for point in batch]
        usable = [point for point in points
                  if point.quality.value in ('fresh', 'held')]
        self.assertTrue(usable)
        # E is 6 * 10; S would have been 6.
        self.assertTrue(all(point.value == 60 for point in usable))

    def test_quality_ambiguous_mask_is_rejected_at_selection(self):
        db = cantools.database.load_file(MASKED_CONFLICT_DBC,
                                         frame_id_mask=0x1FFFFF00)
        with self.assertRaises(ValueError):
            select_signals(db, [SignalSelector('can0', 'J1939PG', 'V')])

    def test_quality_unknown_type_is_a_diagnostic(self):
        db = cantools.database.load_file(ONLY_EXTENDED_DBC)
        selections = select_signals(
            db, [SignalSelector('can0', 'Extended', 'E')])
        source = CandumpLogSource(io.StringIO(read(STANDARD_FRAME_LOG)))
        series = QualityTimeSeries(
            db, source, selections,
            start_us=1_700_000_000_000_001,
            stop_us=1_700_000_000_000_001,
            step_us=100_000)
        list(series.iter_batches())
        unknowns = [diagnostic for diagnostic in series.diagnostics
                    if diagnostic.kind == DiagnosticKind.UNKNOWN_MESSAGE]
        self.assertEqual(len(unknowns), 1)


def record_plot(db, log_path):
    """Feed a log through the legacy plot path without matplotlib.

    Returns the plotter (whose ``signals.values`` holds the curves that
    would be drawn) and anything printed as an error.
    """
    from cantools.subparsers.plot import Plotter

    args = SimpleNamespace(
        no_decode_choices=False,
        show_invalid_syntax=False,
        show_unknown_frames=True,
        show_invalid_data=True,
        ignore_invalid_syntax=True,
        ignore_unknown_frames=False,
        ignore_invalid_data=False,
        output_file=None,
        signals='*',
        case_sensitive=False,
        break_time=-1,
        auto_color_ylabels=False,
        title=None,
        ylabel=None,
        xlabel=None,
        ymin=None,
        ymax=None,
        color=None,
        show_units=True,
    )
    plotter = Plotter(db, args)
    stdout = io.StringIO()
    with mock.patch('sys.stdout', stdout):
        for frame in frames(log_path):
            plotter.add_msg(0, frame.frame_id, frame.data,
                            is_extended_frame=frame.is_extended_frame)
    return plotter, stdout.getvalue()


if __name__ == '__main__':
    unittest.main()
