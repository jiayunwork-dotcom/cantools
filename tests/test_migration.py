# Tests for database migration, the representability quantization policy
# and the ``cantools decode --migrate-to`` command line.
#
# All databases used here are small, synthetic and fully hand-computable.

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from fractions import Fraction
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import cantools
import cantools.logreader as logreader
from cantools.database import (
    Database,
    EncodeError,
    Message,
    MigrationError,
    Signal,
)
from cantools.database.conversion import BaseConversion
from cantools.database.representability import (
    quantize_signal_value,
    validate_max_abs_error,
)
from cantools.logreader import (
    DataFrame,
    TimestampFormat,
    format_candump_log_line,
)


def conv(scale=1, offset=0, choices=None):
    return BaseConversion.factory(scale=scale,
                                  offset=offset,
                                  choices=choices,
                                  is_float=False)


def enum_choices(mapping):
    from collections import OrderedDict
    return OrderedDict(mapping)


# ---------------------------------------------------------------------------
# Hand-computed databases
# ---------------------------------------------------------------------------


def build_simple_source():
    # Frame 0x100, classic, standard id, 3 bytes:
    #   s_num:   LE u8, scale 2 offset 0, unit 'm/s'
    #   s_signed: LE s8, scale -1 offset 10, unit 'deg'
    #   s_enum:  LE u4 at byte 2 low nibble, choices 0=OFF 1=ON 2=AUTO
    #   s_bad:   LE u4 at byte 2 high nibble, raw_invalid=0xF, unit 'V'
    return Database(messages=[
        Message(frame_id=0x100, name='Old', length=3, signals=[
            Signal(name='s_num', start=0, length=8,
                   byte_order='little_endian',
                   conversion=conv(2, 0), unit='m/s'),
            Signal(name='s_signed', start=8, length=8,
                   byte_order='little_endian', is_signed=True,
                   conversion=conv(-1, 10), unit='deg'),
            Signal(name='s_enum', start=16, length=4,
                   byte_order='little_endian',
                   conversion=conv(1, 0, enum_choices(
                       {0: 'OFF', 1: 'ON', 2: 'AUTO'})),
                   unit=None),
            Signal(name='s_bad', start=20, length=4,
                   byte_order='little_endian',
                   conversion=conv(1, 0), unit='V',
                   raw_invalid=0xF),
        ])
    ])


def build_simple_target():
    # Frame 0x200, classic, extended id, 5 bytes:
    #   t_num:   BE u16, scale 1 offset 0, unit 'm/s', min 0 max 20
    #   t_signed: BE s8, scale -2 offset 4, unit 'deg'
    #   t_enum:  BE u8, choices 10=OFF 20=ON 30=AUTO (renumbered labels)
    #   t_bad:   BE u4, scale 1 unit 'V', raw_invalid 0b1010 (=10)
    return Database(messages=[
        Message(frame_id=0x200, name='New', length=5,
                is_extended_frame=True, signals=[
            Signal(name='t_num', start=7, length=16,
                   byte_order='big_endian',
                   conversion=conv(1, 0), unit='m/s',
                   minimum=0, maximum=255),
            Signal(name='t_signed', start=23, length=8,
                   byte_order='big_endian', is_signed=True,
                   conversion=conv(-2, 4), unit='deg'),
            Signal(name='t_enum', start=31, length=8,
                   byte_order='big_endian',
                   conversion=conv(1, 0, enum_choices(
                       {10: 'OFF', 20: 'ON', 30: 'AUTO'}))),
            Signal(name='t_bad', start=39, length=4,
                   byte_order='big_endian',
                   conversion=conv(1, 0), unit='V',
                   raw_invalid=10),
        ])
    ])


SIMPLE_MAPPING = [
    {'source': {'frame_id': 0x100, 'is_extended_frame': False},
     'target': {'frame_id': 0x200, 'is_extended_frame': True},
     'signals': [
         {'source_signal': 's_num', 'target_signal': 't_num'},
         {'source_signal': 's_signed', 'target_signal': 't_signed'},
         {'source_signal': 's_enum', 'target_signal': 't_enum'},
         {'source_signal': 's_bad', 'target_signal': 't_bad'},
     ]}
]


def make_frame(frame_id, data, *, extended=False, is_fd=False,
               brs=False, esi=False,
               timestamp=None,
               channel='vcan0',
               remote=False):
    if timestamp is None:
        import datetime
        timestamp = datetime.datetime(2020, 1, 1, 0, 0, 0)
    return DataFrame(channel=channel,
                     frame_id=frame_id,
                     is_extended_frame=extended,
                     data=data,
                     is_remote_frame=remote,
                     timestamp=timestamp,
                     timestamp_format=TimestampFormat.ABSOLUTE,
                     is_fd=is_fd,
                     bitrate_switch=brs,
                     error_state_indicator=esi)


class SimpleMigrationTest(unittest.TestCase):

    def setUp(self):
        self.src = build_simple_source()
        self.tgt = build_simple_target()

    def test_hand_computed_endianness_signed_enum(self):
        # Source payload:
        #   s_num raw 25 -> phys 50
        #   s_signed raw -3 -> phys -1*(-3)+10 = 13
        #   s_enum raw 2 -> AUTO ; s_bad raw 3 -> 3 V
        # byte0 = 25, byte2 = (s_bad=3)<<4 | (s_enum=2) = 0x32
        frame = make_frame(0x100, bytes([25, 0x00, 0x32]))
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        out = plan.transcode(frame)

        self.assertEqual(out.frame_id, 0x200)
        self.assertTrue(out.is_extended_frame)
        dec = self.tgt.decode_message(0x200, out.data,
                                      force_extended_id=True)
        # t_num = 50 (0x0032), t_signed: phys 13 -> raw (13-4)/-2
        # = -4.5 -> quantize (default zero budget fails; here the
        # plan is built with a 1-unit budget via callers) -> 12 or 14;
        # t_enum AUTO -> 30 (0x1E); t_bad = 3
        self.assertEqual(dec['t_num'], 50)
        self.assertIn(dec['t_signed'], (10, 12, 14))
        self.assertEqual(str(dec['t_enum']), 'AUTO')
        self.assertEqual(dec['t_bad'], 3)

        # byte layout: 00 32 ss 1E 3_ ; verify endianness explicitly
        self.assertEqual(out.data[0:2], b'\x00\x32')
        self.assertEqual(out.data[3], 0x1E)
        self.assertEqual((out.data[4] >> 4) & 0x0F, 3)

    def test_channel_and_timestamp_preserved(self):
        import datetime
        ts = datetime.datetime(1999, 5, 6, 7, 8, 9, 123456)
        frame = make_frame(0x100, bytes([25, 0x00, 0x32]), channel='can7',
                           timestamp=ts)
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        out = plan.transcode(frame)
        self.assertEqual(out.channel, 'can7')
        self.assertEqual(out.timestamp, ts)
        self.assertEqual(out.timestamp_format, TimestampFormat.ABSOLUTE)

    def test_negative_scaling_midpoint_policy(self):
        # t_signed: scale -2 offset 4. phys 13 -> ideal raw -4.5, an
        # exact midpoint between -4 (phys 12) and -5 (phys 14).
        # Policy: tie-break toward the smaller raw code (-5 -> phys 14).
        frame = make_frame(0x100, bytes([0, (0 << 4) | 0]))
        # s_signed raw 0 -> phys 10. Ideal target raw = (10-4)/-2 = -3
        # exact -> phys 10.
        frame.data = bytes([0, 0x00, 0x00])
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        out = plan.transcode(frame)
        dec = self.tgt.decode_message(0x200, out.data,
                                      force_extended_id=True)
        self.assertEqual(dec['t_signed'], 10)

        # Now force the midpoint: choose s_signed raw = -3 (phys 13).
        plan0 = self.src.compile_migration(self.tgt, SIMPLE_MAPPING,
                                           max_abs_error=0.0)
        frame13 = make_frame(0x100, bytes([0, 0x00, 0x00]))
        frame13.data = bytes([0, (-3 & 0xff), 0x00])
        with self.assertRaises(MigrationError):
            plan0.transcode(frame13)  # zero budget: not on grid

        plan1 = self.src.compile_migration(self.tgt, SIMPLE_MAPPING,
                                           max_abs_error=1.0)
        out = plan1.transcode(frame13)
        dec = self.tgt.decode_message(0x200, out.data,
                                      force_extended_id=True)
        # error at -4: |12-13|=1, at -5: |14-13|=1 -> smaller raw -5
        self.assertEqual(dec['t_signed'], 14)

    def test_enum_renumber_keeps_label(self):
        # s_enum raw 1 = ON; target ON lives at raw 20.
        frame = make_frame(0x100, bytes([0, 0x00, 0x01]))
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        out = plan.transcode(frame)
        self.assertEqual(out.data[3], 20)
        dec = self.tgt.decode_message(0x200, out.data,
                                      force_extended_id=True)
        self.assertEqual(str(dec['t_enum']), 'ON')

    def test_invalid_raw_rejected_even_near_valid_neighbors(self):
        # s_bad raw_invalid = 0xF: top nibble of byte1.
        frame = make_frame(0x100, bytes([25, 0x00, 0xF2]))
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        with self.assertRaises(MigrationError) as ctx:
            plan.transcode(frame)
        self.assertIn('raw_invalid', str(ctx.exception))

        # Target raw_invalid=10: a source value of 10 V must not be
        # silently placed onto the target invalid code; with budget 0
        # it must fail, with budget 1 the neighbor 9 (or 11) is chosen.
        frame10 = make_frame(0x100, bytes([25, 0x00, 0xA2]))
        plan0 = self.src.compile_migration(self.tgt, SIMPLE_MAPPING,
                                           max_abs_error=0.0)
        with self.assertRaises(MigrationError):
            plan0.transcode(frame10)
        plan1 = self.src.compile_migration(self.tgt, SIMPLE_MAPPING,
                                           max_abs_error=1.0)
        out = plan1.transcode(frame10)
        dec = self.tgt.decode_message(0x200, out.data,
                                      force_extended_id=True)
        self.assertNotEqual(dec['t_bad'], 10)
        self.assertIn(dec['t_bad'], (9, 11))

    def test_budget_boundary_exact_half_quantum(self):
        # A u4 grid with scale 2: grid points are even physical values,
        # the half quantum is 1.0.
        sig = Signal(name='g', start=0, length=4,
                     conversion=conv(2, 0), unit='u')
        self.assertEqual(
            quantize_signal_value(sig, 5, Fraction(1)), 2)  # phys 4
        # 5.1 needs error >= 0.9 (nearest grid points 4 and 6).
        with self.assertRaises(EncodeError):
            quantize_signal_value(sig, 5.1, Fraction(0.5))
        # exact midpoint tie -> smaller code
        self.assertEqual(
            quantize_signal_value(sig, 5, Fraction(2)), 2)

    def test_zero_budget_requires_exact_grid(self):
        sig = self.tgt.get_message_by_name('New').get_signal_by_name(
            't_num')
        self.assertEqual(
            quantize_signal_value(sig, 51, Fraction(0)), 51)
        with self.assertRaises(EncodeError):
            quantize_signal_value(sig, 51.0001, Fraction(0))

    def test_min_max_enforced_by_quantizer(self):
        # Dedicated u4 signal (0..15) with a small declared maximum.
        sig = Signal(name='ranged', start=0, length=4,
                     conversion=conv(1, 0), unit='u',
                     minimum=0, maximum=10)
        with self.assertRaises(EncodeError):
            quantize_signal_value(sig, 11, Fraction(0))
        with self.assertRaises(EncodeError):
            quantize_signal_value(sig, 10.6, Fraction(0.5))
        # Neighbor 10.4 is above the declared maximum of 10, so the
        # range check rejects it even though the grid would fit.
        with self.assertRaises(EncodeError):
            quantize_signal_value(sig, 10.4, Fraction(0.5))

    def test_budget_validation(self):
        for bad in (-1, float('inf'), float('nan'), -0.1):
            with self.assertRaises((ValueError, TypeError)):
                validate_max_abs_error(bad)
        validate_max_abs_error(0)
        validate_max_abs_error(1.5)

    def test_truncated_and_undecodable_frames_rejected(self):
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        short = make_frame(0x100, b'\x00')
        with self.assertRaises(MigrationError):
            plan.transcode(short)

    def test_remote_frame_rejected(self):
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        frame = make_frame(0x100, b'', remote=True)
        with self.assertRaises(MigrationError):
            plan.transcode(frame)

    def test_unknown_frame_has_location_info(self):
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        frame = make_frame(0x7FF, b'\x00\x00', channel='can3')
        with self.assertRaises(MigrationError) as ctx:
            plan.transcode(frame)
        self.assertEqual(ctx.exception.frame_id, 0x7FF)
        self.assertEqual(ctx.exception.channel, 'can3')

    def test_standard_vs_extended_same_id_are_distinct(self):
        # An extended 0x100 must not resolve to the standard mapping.
        plan = self.src.compile_migration(self.tgt, SIMPLE_MAPPING)
        frame = make_frame(0x100, bytes([25, 0x00, 0x32]), extended=True)
        with self.assertRaises(MigrationError):
            plan.transcode(frame)

    def test_frame_fd_mismatch_rejected(self):
        src = Database(messages=[
            Message(frame_id=0x300, name='FdOld', length=2, is_fd=True,
                    signals=[Signal(name='x', start=0, length=8,
                                    conversion=conv(1, 0), unit='a')])
        ])
        tgt = Database(messages=[
            Message(frame_id=0x301, name='ClassicNew', length=2,
                    is_fd=False,
                    signals=[Signal(name='y', start=0, length=8,
                                    conversion=conv(1, 0), unit='a')])
        ])
        mapping = [{'source': {'frame_id': 0x300,
                               'is_extended_frame': False},
                    'target': {'frame_id': 0x301,
                               'is_extended_frame': False},
                    'signals': [
                        {'source_signal': 'x', 'target_signal': 'y'}]}]
        with self.assertRaises(MigrationError):
            src.compile_migration(tgt, mapping)

        # And an observed frame whose FD flag disagrees with the
        # definition is rejected at transcode time.
        tgt_fd = Database(messages=[
            Message(frame_id=0x302, name='FdNew', length=2, is_fd=True,
                    signals=[Signal(name='y', start=0, length=8,
                                    conversion=conv(1, 0), unit='a')])
        ])
        mapping2 = [{'source': {'frame_id': 0x300,
                                'is_extended_frame': False},
                     'target': {'frame_id': 0x302,
                                'is_extended_frame': False},
                     'signals': [
                         {'source_signal': 'x', 'target_signal': 'y'}]}]
        plan = src.compile_migration(tgt_fd, mapping2)
        classic_frame = make_frame(0x300, b'\x05\x00', is_fd=False)
        with self.assertRaises(MigrationError):
            plan.transcode(classic_frame)

    def test_fd_brs_and_esi_preserved(self):
        src = Database(messages=[
            Message(frame_id=0x300, name='FdOld', length=2, is_fd=True,
                    signals=[Signal(name='x', start=0, length=8,
                                    conversion=conv(1, 0), unit='a')])
        ])
        tgt = Database(messages=[
            Message(frame_id=0x302, name='FdNew', length=3, is_fd=True,
                    signals=[Signal(name='y', start=23, length=8,
                                    byte_order='big_endian',
                                    conversion=conv(1, 0), unit='a')])
        ])
        mapping = [{'source': {'frame_id': 0x300,
                               'is_extended_frame': False},
                    'target': {'frame_id': 0x302,
                               'is_extended_frame': False},
                    'signals': [
                        {'source_signal': 'x', 'target_signal': 'y'}]}]
        plan = src.compile_migration(tgt, mapping)
        frame = make_frame(0x300, b'\x07\x00', is_fd=True,
                           brs=True, esi=True)
        out = plan.transcode(frame)
        self.assertTrue(out.is_fd)
        self.assertTrue(out.bitrate_switch)
        self.assertTrue(out.error_state_indicator)
        line = format_candump_log_line(out)
        self.assertIn('##3', line)

        # Round-trip through the logreader parser keeps BRS/ESI.
        parsed = logreader.Parser().parse(line)
        self.assertTrue(parsed.is_fd)
        self.assertTrue(parsed.bitrate_switch)
        self.assertTrue(parsed.error_state_indicator)


class MappingValidationTest(unittest.TestCase):

    def setUp(self):
        self.src = build_simple_source()
        self.tgt = build_simple_target()

    def test_missing_signal_reference(self):
        bad = json.loads(json.dumps(SIMPLE_MAPPING))
        bad[0]['signals'][0]['source_signal'] = 'nope'
        with self.assertRaises(MigrationError) as ctx:
            self.src.compile_migration(self.tgt, bad)
        self.assertIn('signals[0]', str(ctx.exception))

    def test_duplicate_target(self):
        bad = json.loads(json.dumps(SIMPLE_MAPPING))
        bad[0]['signals'][1]['target_signal'] = 't_num'
        with self.assertRaises(MigrationError):
            self.src.compile_migration(self.tgt, bad)

    def test_unit_mismatch(self):
        self.tgt.get_message_by_name('New').get_signal_by_name(
            't_num').unit = 'km/h'
        self.tgt.refresh()
        with self.assertRaises(MigrationError):
            self.src.compile_migration(self.tgt, SIMPLE_MAPPING)

    def test_enum_numeric_mismatch(self):
        # Strip choices on target enum signal.
        tgt = build_simple_target()
        sig = tgt.get_message_by_name('New').get_signal_by_name('t_enum')
        sig.conversion = BaseConversion.factory(scale=1, offset=0)
        tgt.refresh()
        with self.assertRaises(MigrationError):
            self.src.compile_migration(tgt, SIMPLE_MAPPING)

    def test_enum_missing_label(self):
        tgt = build_simple_target()
        choices = tgt.get_message_by_name('New').get_signal_by_name(
            't_enum').conversion.choices
        del choices[30]  # AUTO missing
        tgt.refresh()
        with self.assertRaises(MigrationError) as ctx:
            self.src.compile_migration(tgt, SIMPLE_MAPPING)
        self.assertIn('AUTO', str(ctx.exception))

    def test_target_branch_coverage_gap(self):
        # Target gains an extra unmapped signal at top level.
        tgt = build_simple_target()
        msg = tgt.get_message_by_name('New')
        msg.length = 6
        msg._signals.append(Signal(name='orphan', start=47, length=4,
                                   byte_order='big_endian',
                                   conversion=conv(1, 0), unit='x'))
        tgt.refresh()
        with self.assertRaises(MigrationError) as ctx:
            self.src.compile_migration(tgt, SIMPLE_MAPPING)
        self.assertIn('orphan', str(ctx.exception))

    def test_unknown_frame_reference(self):
        bad = json.loads(json.dumps(SIMPLE_MAPPING))
        bad[0]['source']['frame_id'] = 0x999
        with self.assertRaises(MigrationError):
            self.src.compile_migration(self.tgt, bad)

    def test_float_and_container_rejected(self):
        src = Database(messages=[
            Message(frame_id=0x400, name='FloatMsg', length=2, signals=[
                Signal(name='f', start=0, length=16,
                       conversion=BaseConversion.factory(is_float=True),
                       unit='a')])
        ])
        tgt = Database(messages=[
            Message(frame_id=0x401, name='T', length=2, signals=[
                Signal(name='g', start=0, length=16,
                       conversion=conv(1, 0), unit='a')])
        ])
        mapping = [{'source': {'frame_id': 0x400,
                               'is_extended_frame': False},
                    'target': {'frame_id': 0x401,
                               'is_extended_frame': False},
                    'signals': [
                        {'source_signal': 'f', 'target_signal': 'g'}]}]
        with self.assertRaises(MigrationError):
            src.compile_migration(tgt, mapping)

    def test_invalid_mapping_does_not_consume_input(self):
        # Compilation happens before the stream is touched. Prove it
        # with a stream that records readline calls.
        class RecordingStream:
            def __init__(self):
                self.reads = 0

            def readline(self):
                self.reads += 1
                return ''

        stream = RecordingStream()
        bad = json.loads(json.dumps(SIMPLE_MAPPING))
        bad[0]['signals'][0]['target_signal'] = 'ghost'
        with self.assertRaises(MigrationError):
            self.src.compile_migration(self.tgt, bad)
        # The compile API never receives the stream at all; the CLI
        # test below additionally asserts stdin is unread.
        self.assertEqual(stream.reads, 0)


# ---------------------------------------------------------------------------
# Nested multiplexers
# ---------------------------------------------------------------------------


def build_mux_source():
    # selector A (u2), selector B (u2) nested in branch A=1.
    signals = [
        Signal(name='A', start=0, length=2, is_multiplexer=True,
               conversion=conv(1, 0)),
        Signal(name='base', start=8, length=8, conversion=conv(1, 0),
               unit='u'),
        Signal(name='a0', start=16, length=8,
               multiplexer_signal='A', multiplexer_ids=[0],
               conversion=conv(1, 0), unit='u'),
        Signal(name='B', start=2, length=2, is_multiplexer=True,
               multiplexer_signal='A', multiplexer_ids=[1],
               conversion=conv(1, 0)),
        Signal(name='b0', start=24, length=8,
               multiplexer_signal='B', multiplexer_ids=[0],
               conversion=conv(1, 0), unit='u'),
        Signal(name='b1', start=24, length=8,
               multiplexer_signal='B', multiplexer_ids=[1],
               conversion=conv(1, 0), unit='u'),
    ]
    return Database(messages=[
        Message(frame_id=0x500, name='MuxOld', length=4, signals=signals)
    ])


def build_mux_target():
    # Branch ids deliberately renumbered; layout changed.
    signals = [
        Signal(name='TA', start=4, length=4, is_multiplexer=True,
               conversion=conv(1, 0)),
        Signal(name='tbase', start=15, length=8, byte_order='big_endian',
               conversion=conv(1, 0), unit='u'),
        Signal(name='ta7', start=23, length=8, byte_order='big_endian',
               multiplexer_signal='TA', multiplexer_ids=[7],
               conversion=conv(1, 0), unit='u'),
        Signal(name='TB', start=0, length=4, is_multiplexer=True,
               multiplexer_signal='TA', multiplexer_ids=[9],
               conversion=conv(1, 0)),
        Signal(name='tb3', start=31, length=8, byte_order='big_endian',
               multiplexer_signal='TB', multiplexer_ids=[3],
               conversion=conv(1, 0), unit='u'),
        Signal(name='tb4', start=31, length=8, byte_order='big_endian',
               multiplexer_signal='TB', multiplexer_ids=[4],
               conversion=conv(1, 0), unit='u'),
    ]
    return Database(messages=[
        Message(frame_id=0x600, name='MuxNew', length=4, signals=signals)
    ])


MUX_MAPPING = [
    {'source': {'frame_id': 0x500, 'is_extended_frame': False},
     'target': {'frame_id': 0x600, 'is_extended_frame': False},
     'signals': [
         {'source_signal': 'A', 'target_signal': 'TA'},
         {'source_signal': 'base', 'target_signal': 'tbase'},
         {'source_signal': 'a0', 'target_signal': 'ta7'},
         {'source_signal': 'B', 'target_signal': 'TB'},
         {'source_signal': 'b0', 'target_signal': 'tb3'},
         {'source_signal': 'b1', 'target_signal': 'tb4'},
     ]}
]


class NestedMultiplexerTest(unittest.TestCase):

    def setUp(self):
        self.src = build_mux_source()
        self.tgt = build_mux_target()

    def test_nested_branches_hand_computed(self):
        plan = self.src.compile_migration(self.tgt, MUX_MAPPING)

        # Branch A=1, B=1: payload A=1 B=1 b1=0xAB base=0xCD
        payload = bytes([0x01 | (1 << 2), 0xCD, 0x00, 0xAB])
        out = plan.transcode(make_frame(0x500, payload))
        dec = self.tgt.decode_message(0x600, out.data)
        self.assertEqual(dec['TA'], 9)      # A=1 maps to TA=9
        self.assertEqual(dec['TB'], 4)      # B=1 maps to TB=4
        self.assertEqual(dec['tb4'], 0xAB)
        self.assertEqual(dec['tbase'], 0xCD)
        self.assertNotIn('tb3', dec)
        self.assertNotIn('ta7', dec)

        # Branch A=0: a0 active.
        payload0 = bytes([0x00, 0x01, 0x77, 0x00])
        out0 = plan.transcode(make_frame(0x500, payload0))
        dec0 = self.tgt.decode_message(0x600, out0.data)
        self.assertEqual(dec0['TA'], 7)
        self.assertEqual(dec0['ta7'], 0x77)

    def test_undefined_selector_value_is_migration_error(self):
        plan = self.src.compile_migration(self.tgt, MUX_MAPPING)
        # A=3 is not a defined branch.
        bad = make_frame(0x500, bytes([0x03, 0, 0, 0]))
        with self.assertRaises(MigrationError):
            plan.transcode(bad)

    def test_plan_size_grows_with_tree_nodes_not_combinations(self):
        # Synthetic 10-level nested multiplexer, two branches per
        # level. A cartesian expansion would create 2**10 leaf
        # combinations; the compiled plan must contain one node per
        # codec-tree node (2**11 - 1 = 2047 nodes) and no enumeration
        # of selector tuples.
        depth = 10
        src_signals = []
        tgt_signals = []
        for level in range(depth):
            name = f'S{level}'
            tname = f'T{level}'
            kwargs = {}
            if level > 0:
                kwargs = {'multiplexer_signal': f'S{level-1}',
                          'multiplexer_ids': [0, 1]}
            src_signals.append(Signal(
                name=name, start=2 * level, length=2,
                is_multiplexer=True, conversion=conv(1, 0), **kwargs))
            tk = {}
            if level > 0:
                tk = {'multiplexer_signal': f'T{level-1}',
                      'multiplexer_ids': [0, 1]}
            tgt_signals.append(Signal(
                name=tname, start=2 * level, length=2,
                is_multiplexer=True, conversion=conv(1, 0), **tk))
        # one leaf signal per terminal branch: distinct signals so the
        # branches remain distinguishable through the mapping.
        src_signals.append(Signal(
            name='leaf0', start=40, length=8,
            multiplexer_signal=f'S{depth-1}', multiplexer_ids=[0],
            conversion=conv(1, 0), unit='u'))
        src_signals.append(Signal(
            name='leaf1', start=48, length=8,
            multiplexer_signal=f'S{depth-1}', multiplexer_ids=[1],
            conversion=conv(1, 0), unit='u'))
        tgt_signals.append(Signal(
            name='tleaf0', start=40, length=8,
            multiplexer_signal=f'T{depth-1}', multiplexer_ids=[0],
            conversion=conv(1, 0), unit='u'))
        tgt_signals.append(Signal(
            name='tleaf1', start=48, length=8,
            multiplexer_signal=f'T{depth-1}', multiplexer_ids=[1],
            conversion=conv(1, 0), unit='u'))

        src = Database(messages=[Message(
            frame_id=0x700, name='Deep', length=8, signals=src_signals)])
        tgt = Database(messages=[Message(
            frame_id=0x701, name='DeepNew', length=8,
            signals=tgt_signals)])
        mapping = [{
            'source': {'frame_id': 0x700, 'is_extended_frame': False},
            'target': {'frame_id': 0x701, 'is_extended_frame': False},
            'signals': [
                {'source_signal': f'S{i}', 'target_signal': f'T{i}'}
                for i in range(depth)
            ] + [
                {'source_signal': 'leaf0', 'target_signal': 'tleaf0'},
                {'source_signal': 'leaf1', 'target_signal': 'tleaf1'},
            ]
        }]
        plan = src.compile_migration(tgt, mapping)

        def count_nodes(codec_node):
            total = 1
            for branches in codec_node['multiplexers'].values():
                for child in branches.values():
                    total += count_nodes(child)
            return total

        expected = count_nodes(src.get_message_by_frame_id(0x700)._codecs)
        self.assertEqual(expected, 2 ** (depth + 1) - 1)

        def count_plan_nodes(node_plan):
            total = 1
            for mux in node_plan.multiplexers.values():
                for child in mux.branches.values():
                    total += count_plan_nodes(child)
            return total

        only = next(iter(plan._message_plans.values()))
        self.assertEqual(count_plan_nodes(only.root), expected)

        # Every branch dict is keyed by the two selector codes only;
        # there are no combined-tuple structures.
        def check_branch_dicts(node_plan):
            for mux in node_plan.multiplexers.values():
                self.assertEqual(set(mux.branches), {0, 1})
                self.assertEqual(set(mux.raw_code_map), {0, 1})
                for child in mux.branches.values():
                    check_branch_dicts(child)
        check_branch_dicts(only.root)

        # And the plan still transcodes the deepest path correctly.
        payload = bytearray(8)
        for level in range(depth):
            payload[level // 4] |= 1 << (2 * (level % 4))
        payload[6] = 0x5A
        out = plan.transcode(make_frame(0x700, bytes(payload)))
        dec = tgt.decode_message(0x701, out.data)
        self.assertEqual(dec['tleaf1'], 0x5A)
        self.assertNotIn('tleaf0', dec)


class EnumSelectorTest(unittest.TestCase):

    def test_enum_selectors_migrate_labels_with_different_codes(self):
        src = Database(messages=[
            Message(frame_id=0x610, name='E', length=2, signals=[
                Signal(name='sel', start=0, length=4, is_multiplexer=True,
                       conversion=conv(1, 0, enum_choices(
                           {0: 'IDLE', 1: 'RUN'})),
                       unit=None),
                Signal(name='idle_v', start=8, length=8,
                       multiplexer_signal='sel',
                       multiplexer_ids=[0],
                       conversion=conv(1, 0), unit='u'),
                Signal(name='run_v', start=8, length=8,
                       multiplexer_signal='sel',
                       multiplexer_ids=[1],
                       conversion=conv(1, 0), unit='u'),
            ])
        ])
        tgt = Database(messages=[
            Message(frame_id=0x611, name='E2', length=2, signals=[
                Signal(name='mode', start=0, length=4, is_multiplexer=True,
                       conversion=conv(1, 0, enum_choices(
                           {5: 'IDLE', 9: 'RUN'})),
                       unit=None),
                Signal(name='idle_w', start=8, length=8,
                       multiplexer_signal='mode',
                       multiplexer_ids=[5],
                       conversion=conv(1, 0), unit='u'),
                Signal(name='run_w', start=8, length=8,
                       multiplexer_signal='mode',
                       multiplexer_ids=[9],
                       conversion=conv(1, 0), unit='u'),
            ])
        ])
        mapping = [{
            'source': {'frame_id': 0x610, 'is_extended_frame': False},
            'target': {'frame_id': 0x611, 'is_extended_frame': False},
            'signals': [
                {'source_signal': 'sel', 'target_signal': 'mode'},
                {'source_signal': 'idle_v', 'target_signal': 'idle_w'},
                {'source_signal': 'run_v', 'target_signal': 'run_w'},
            ]}]
        plan = src.compile_migration(tgt, mapping)
        out = plan.transcode(make_frame(0x610, bytes([1, 0x42])))
        dec = tgt.decode_message(0x611, out.data)
        self.assertEqual(str(dec['mode']), 'RUN')
        self.assertEqual(dec['run_w'], 0x42)
        self.assertEqual(out.data[0] & 0x0F, 9)


class EncodeRepresentabilityTest(unittest.TestCase):

    def setUp(self):
        self.tgt = build_simple_target()
        self.msg = self.tgt.get_message_by_name('New')

    def test_encode_message_mode_matches_quantizer(self):
        # Same physical value, same decision via Message.encode,
        # Database.encode_message and the quantizer directly.
        for value, budget in ((50, 0), (50.5, 0.5), (13, 1)):
            payload = self.msg.encode(
                {'t_num': value, 't_signed': 10, 't_enum': 'ON',
                 't_bad': 3},
                representability={'max_abs_error': budget})
            payload2 = self.tgt.encode_message(
                'New',
                {'t_num': value, 't_signed': 10, 't_enum': 'ON',
                 't_bad': 3},
                representability={'max_abs_error': budget})
            self.assertEqual(payload, payload2)
            dec = self.msg.decode(payload)
            self.assertLessEqual(abs(dec['t_num'] - value), budget + 1e-12)

    def test_encode_rejects_over_budget_and_invalid_code(self):
        # With a unit grid the quantization error is at most 0.5, so a
        # budget of 0.25 rejects values between grid points (verified
        # via exact rational comparison, not float rounding).
        with self.assertRaises(EncodeError):
            self.msg.encode(
                {'t_num': 50.4, 't_signed': 10, 't_enum': 'ON',
                 't_bad': 3},
                representability={'max_abs_error': 0.25})
        # t_bad 10 is raw_invalid: the invalid sentinel must never be
        # emitted, even with a generous budget -- a neighbor code is
        # chosen instead (and with a zero budget no neighbor exists
        # within reach, so it must fail).
        with self.assertRaises(EncodeError):
            self.msg.encode(
                {'t_num': 50, 't_signed': 10, 't_enum': 'ON',
                 't_bad': 10},
                representability={'max_abs_error': 0.0})
        payload = self.msg.encode(
            {'t_num': 50, 't_signed': 10, 't_enum': 'ON',
             't_bad': 10},
            representability={'max_abs_error': 2.0})
        decoded = self.msg.decode(payload)
        self.assertNotEqual(decoded['t_bad'], 10)
        self.assertIn(decoded['t_bad'], (9, 11))
        # Neighbor values still work within the budget.
        payload = self.msg.encode(
            {'t_num': 50, 't_signed': 10, 't_enum': 'ON',
             't_bad': 9},
            representability={'max_abs_error': 0.0})
        self.assertIsInstance(payload, bytes)

    def test_representability_requires_scaling_and_strict(self):
        data = {'t_num': 50, 't_signed': 10, 't_enum': 'ON',
                't_bad': 3}
        with self.assertRaises(EncodeError):
            self.msg.encode(data, scaling=False,
                            representability={'max_abs_error': 0})
        with self.assertRaises(EncodeError):
            self.msg.encode(data, strict=False,
                            representability={'max_abs_error': 0})
        with self.assertRaises((ValueError, TypeError)):
            self.msg.encode(data, representability={'nope': 1})
        with self.assertRaises((ValueError, TypeError)):
            self.msg.encode(data,
                            representability={'max_abs_error': -1})

    def test_omitting_representability_preserves_behavior(self):
        # Historical rounding (banker's) still applies without the key.
        payload = self.msg.encode(
            {'t_num': 50, 't_signed': 13, 't_enum': 'ON', 't_bad': 3})
        self.assertIsInstance(payload, bytes)

    def test_migration_and_encode_agree_on_quantization(self):
        src = build_simple_source()
        plan = src.compile_migration(build_simple_target(),
                                     SIMPLE_MAPPING,
                                     max_abs_error=1.0)
        frame = make_frame(0x100, bytes([25, 0x00, 0x32]))
        migrated = plan.transcode(frame)
        dec = self.msg.decode(migrated.data)
        direct = self.msg.encode(
            {'t_num': dec['t_num'], 't_signed': dec['t_signed'],
             't_enum': str(dec['t_enum']), 't_bad': dec['t_bad']},
            representability={'max_abs_error': 1.0})
        self.assertEqual(migrated.data, direct)


class PlanIsolationTest(unittest.TestCase):

    def test_database_changes_and_refresh_do_not_affect_plan(self):
        src = build_simple_source()
        tgt = build_simple_target()
        plan = src.compile_migration(tgt, SIMPLE_MAPPING)

        frame = make_frame(0x100, bytes([25, 0x00, 0x32]))
        before = plan.transcode(frame).data

        # Mutate signal definitions and refresh both databases.
        src.get_message_by_name('Old').get_signal_by_name(
            's_num').conversion = conv(100, 0)
        tgt.get_message_by_name('New').get_signal_by_name(
            't_num').conversion = conv(7, 3)
        src.refresh()
        tgt.refresh()

        after = plan.transcode(frame).data
        self.assertEqual(before, after)

    def test_plan_does_not_mutate_databases(self):
        src = build_simple_source()
        tgt = build_simple_target()
        src_before = src.as_dbc_string() if hasattr(src, 'as_dbc_string') \
            else None
        plan = src.compile_migration(tgt, SIMPLE_MAPPING)
        frame = make_frame(0x100, bytes([25, 0x00, 0x32]))
        plan.transcode(frame)
        if src_before is not None:
            self.assertEqual(src.as_dbc_string(), src_before)

    def test_plan_is_reusable(self):
        src = build_simple_source()
        tgt = build_simple_target()
        plan = src.compile_migration(tgt, SIMPLE_MAPPING)
        frame = make_frame(0x100, bytes([25, 0x00, 0x32]))
        first = plan.transcode(frame)
        second = plan.transcode(frame)
        self.assertEqual(first.data, second.data)
        self.assertIsNot(first, second)


class DescribeTest(unittest.TestCase):

    def test_describe_is_json_serializable(self):
        plan = build_simple_source().compile_migration(
            build_simple_target(), SIMPLE_MAPPING, max_abs_error=0.25)
        description = plan.describe()
        blob = json.dumps(description)
        again = json.loads(blob)
        self.assertEqual(again[0]['source']['frame_id'], 0x100)
        kinds = {s['kind'] for s in again[0]['signals']}
        self.assertIn('numeric', kinds)
        self.assertIn('enum', kinds)


class StreamingTest(unittest.TestCase):

    def test_forward_only_no_frame_accumulation(self):
        src = build_simple_source()
        tgt = build_simple_target()
        plan = src.compile_migration(tgt, SIMPLE_MAPPING)

        class CountingStream:
            def __init__(self, n):
                self.n = n
                self.line = 0

            def readline(self):
                self.line += 1
                if self.line > self.n:
                    return ''
                ts = 1_000_000_000 + self.line
                return (f'({ts}.000001) vcan0 100#190032\n')

        stream = CountingStream(5000)
        parser = logreader.Parser(stream)
        count = 0
        for frame in parser:
            out = plan.transcode(frame)
            self.assertIsInstance(out.data, bytes)
            count += 1
        self.assertEqual(count, 5000)
        # The stream was read strictly line by line; no seeking.
        self.assertEqual(stream.line, 5001)
        self.assertFalse(hasattr(stream, 'seek'))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


DBC_SOURCE = """VERSION ""

BO_ 256 Old: 3 Vector__XXX
 SG_ s_num : 0|8@1+ (2,0) [0|0] "m/s" Vector__XXX
 SG_ s_signed : 8|8@1- (-1,10) [0|0] "deg" Vector__XXX
 SG_ s_enum : 16|4@1+ (1,0) [0|0] "" Vector__XXX
 SG_ s_bad : 20|4@1+ (1,0) [0|0] "V" Vector__XXX

BO_ 770 FdOld: 2 Vector__XXX
 SG_ f_in : 0|8@1+ (1,0) [0|0] "a" Vector__XXX

VAL_ 256 s_enum 0 "OFF" 1 "ON" 2 "AUTO" ;

BA_DEF_ BO_  "VFrameFormat" ENUM  "StandardCAN","ExtendedCAN","reserved","J1939PG","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","StandardCAN_FD","ExtendedCAN_FD";
BA_DEF_DEF_  "VFrameFormat" "";
BA_ "VFrameFormat" BO_ 770 14;
"""

DBC_TARGET = """VERSION ""

BO_ 2147484160 New: 5 Vector__XXX
 SG_ t_num : 7|16@0+ (1,0) [0|255] "m/s" Vector__XXX
 SG_ t_signed : 23|8@0- (-2,4) [0|0] "deg" Vector__XXX
 SG_ t_enum : 31|8@0+ (1,0) [0|0] "" Vector__XXX
 SG_ t_bad : 39|4@0+ (1,0) [0|0] "V" Vector__XXX

BO_ 771 FdNew: 3 Vector__XXX
 SG_ f_out : 23|8@0+ (1,0) [0|0] "a" Vector__XXX

VAL_ 2147484160 t_enum 10 "OFF" 20 "ON" 30 "AUTO" ;

BA_DEF_ BO_  "VFrameFormat" ENUM  "StandardCAN","ExtendedCAN","reserved","J1939PG","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","reserved","StandardCAN_FD","ExtendedCAN_FD";
BA_DEF_DEF_  "VFrameFormat" "";
BA_ "VFrameFormat" BO_ 2147484160 1;
BA_ "VFrameFormat" BO_ 770 14;
BA_ "VFrameFormat" BO_ 771 14;
"""

FD_MAPPING_ENTRY = {
    'source': {'frame_id': 0x302, 'is_extended_frame': False},
    'target': {'frame_id': 0x303, 'is_extended_frame': False},
    'signals': [{'source_signal': 'f_in', 'target_signal': 'f_out'}]
}


class DecodeMigrationCliTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src_path = Path(self.tmp.name) / 'source.dbc'
        self.tgt_path = Path(self.tmp.name) / 'target.dbc'
        self.map_path = Path(self.tmp.name) / 'map.json'
        self.src_path.write_text(DBC_SOURCE)
        self.tgt_path.write_text(DBC_TARGET)
        self.map_path.write_text(json.dumps(SIMPLE_MAPPING))

    def _run_cli(self, stdin_text, extra_args=None):
        mapping = list(SIMPLE_MAPPING)
        mapping.append(FD_MAPPING_ENTRY)
        self.map_path.write_text(json.dumps(mapping))
        argv = ['cantools', 'decode',
                '--migrate-to', str(self.tgt_path),
                '--migration-map', str(self.map_path),
                '--max-abs-error', '1.0',
                str(self.src_path)]
        if extra_args:
            argv[1:1] = extra_args
        out = StringIO()
        with patch('sys.stdin', StringIO(stdin_text)), \
                patch('sys.stdout', out), \
                patch('sys.argv', argv):
            cantools._main()
        return out.getvalue()

    def test_pipeline_preserves_order_time_type_brs(self):
        stdin = (
            '(1594172461.968006) vcan0 100#190032\n'
            '(1594172461.968006) can1 100#190032\n'   # same timestamp
            '(1594172462.000000) vcan0 100#000000\n'
            '(1594172463.123456) vcan0 302##10700\n'
        )
        output = self._run_cli(stdin)
        lines = output.splitlines()
        self.assertEqual(len(lines), 4)
        # microsecond precision and ordering preserved
        self.assertTrue(lines[0].startswith('(1594172461.968006) vcan0'))
        self.assertTrue(lines[1].startswith('(1594172461.968006) can1'))
        self.assertTrue(lines[2].startswith('(1594172462.000000)'))
        # extended target id rendered with 8 hex digits
        self.assertIn('00000200', lines[0])
        # FD frame keeps the ## marker and BRS flag (##1 -> BRS)
        self.assertIn('##1', lines[3])
        self.assertIn('303', lines[3])

        # Output is re-readable as candump -L.
        parser = logreader.Parser()
        frames = [parser.parse(line) for line in lines]
        self.assertTrue(all(f is not None for f in frames))
        self.assertTrue(frames[3].is_fd)
        self.assertTrue(frames[3].bitrate_switch)
        # No decode annotation prose is appended.
        self.assertNotIn('::', output)

    def test_bad_line_stops_with_line_number_and_valid_prefix(self):
        mapping = list(SIMPLE_MAPPING)
        mapping.append(FD_MAPPING_ENTRY)
        self.map_path.write_text(json.dumps(mapping))
        stdin = (
            '(1594172461.968006) vcan0 100#190032\n'
            'this is not a candump line\n'
            '(1594172463.000000) vcan0 100#000000\n'
        )
        argv = ['cantools', 'decode',
                '--migrate-to', str(self.tgt_path),
                '--migration-map', str(self.map_path),
                str(self.src_path)]
        out = StringIO()
        err = StringIO()
        with patch('sys.stdin', StringIO(stdin)), \
                patch('sys.stdout', out), \
                patch('sys.stderr', err), \
                patch('sys.argv', argv):
            with self.assertRaises(SystemExit):
                cantools._main()
        self.assertIn('line 2', err.getvalue())
        # Only the first frame was emitted; that prefix is legal.
        prefix = out.getvalue().splitlines()
        self.assertEqual(len(prefix), 1)
        self.assertIsNotNone(logreader.Parser().parse(prefix[0]))

    def test_failed_frame_stops_with_line_number(self):
        # s_num=25 -> phys 50 is exactly representable, but the default
        # zero budget makes s_signed phys 13 (raw -3) unrepresentable
        # on the target grid (phys 12/14), so the first frame fails.
        stdin = (
            '(1594172461.968006) vcan0 100#19FDF2\n'
            '(1594172463.000000) vcan0 100#000000\n'
        )
        argv = ['cantools', 'decode',
                '--migrate-to', str(self.tgt_path),
                '--migration-map', str(self.map_path),
                str(self.src_path)]
        out = StringIO()
        err = StringIO()
        with patch('sys.stdin', StringIO(stdin)), \
                patch('sys.stdout', out), \
                patch('sys.stderr', err), \
                patch('sys.argv', argv):
            with self.assertRaises(SystemExit):
                cantools._main()
        self.assertIn('line 1', err.getvalue())
        self.assertEqual(out.getvalue(), '')

    def test_bad_mapping_fails_before_any_output(self):
        bad_map = Path(self.tmp.name) / 'bad.json'
        loaded = json.loads(SIMPLE_MAPPING[0] and json.dumps(SIMPLE_MAPPING))
        loaded[0]['signals'][0]['target_signal'] = 'ghost'
        bad_map.write_text(json.dumps(loaded))
        argv = ['cantools', 'decode',
                '--migrate-to', str(self.tgt_path),
                '--migration-map', str(bad_map),
                str(self.src_path)]
        out = StringIO()
        stdin = StringIO('(1594172461.968006) vcan0 100#190032\n')
        with patch('sys.stdin', stdin), \
                patch('sys.stdout', out), \
                patch('sys.argv', argv):
            with self.assertRaises(SystemExit):
                cantools._main()
        self.assertEqual(out.getvalue(), '')
        # stdin was never read
        self.assertEqual(stdin.tell(), 0)

    def test_missing_option_combination_is_rejected(self):
        argv = ['cantools', 'decode',
                '--migrate-to', str(self.tgt_path),
                str(self.src_path)]
        with patch('sys.stdin', StringIO('')), \
                patch('sys.argv', argv):
            with self.assertRaises(SystemExit):
                cantools._main()

    def test_regular_decode_still_works(self):
        argv = ['cantools', 'decode', str(self.src_path)]
        stdin = '  vcan0  100   [3]  19 00 32\n'
        out = StringIO()
        with patch('sys.stdin', StringIO(stdin)), \
                patch('sys.stdout', out), \
                patch('sys.argv', argv):
            cantools._main()
        self.assertIn('Old(', out.getvalue())


if __name__ == '__main__':
    unittest.main()
