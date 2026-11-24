# Tests for the stateful AUTOSAR end-to-end protection checks
# (E2EStatus/E2EProtection), covering both the Python API and the
# "cantools decode" command line.
#
# All CRCs in the frame fixtures below were computed with cantools'
# own profile 2/profile 5 CRC implementation, and every expected
# sequence-counter outcome was derived by hand from the rules
# implemented in cantools/autosar/e2e_protection.py.

import unittest
from io import StringIO
from unittest.mock import patch

import cantools
import cantools.autosar
from cantools.autosar import E2EProtection, E2EStatus
from cantools.subparsers.__utils__ import format_message_by_frame_id


ARXML = 'tests/files/arxml/system-4.2.arxml'


def make_profile2_frame(message, seq, rest=b'\x11\x22\x33', crc_ok=True):
    """Build a 9 byte Profile 2 frame. The sequence counter goes into
    the low nibble of the second byte, as mandated by the profile."""

    frame = bytearray(message.length)
    frame[1] = seq & 0x0f
    frame[2:2 + len(rest)] = rest
    frame[0] = cantools.autosar.compute_profile2_crc(bytes(frame), message)
    if not crc_ok:
        frame[0] ^= 0xff
    return bytes(frame)


def make_profile5_frame(message, seq, third_byte=0x44,
                        trailer=b'\xaa\xbb', crc_ok=True, length=6):
    """Build a Profile 5 frame with a 4 byte protected prefix and a
    2 byte SecOC trailer."""

    frame = bytearray(6)
    frame[2] = seq & 0xff
    frame[3] = third_byte
    crc = cantools.autosar.compute_profile5_crc(bytes(frame[:4]), message)
    frame[0] = crc & 0xff
    frame[1] = (crc >> 8) & 0xff
    if not crc_ok:
        frame[0] ^= 0xff
    frame[4:6] = trailer
    return bytes(frame[:length])


def wrap_container(entries):
    result = b''
    for header_id, payload in entries:
        result += header_id.to_bytes(3, 'big')
        result += bytes([len(payload)])
        result += payload
    return result


class E2EProfile2Test(unittest.TestCase):

    def setUp(self):
        db = cantools.database.load_file(ARXML)
        self.message = db.get_message_by_name('Message1')
        self.checker = E2EProtection(self.message)

    def check(self, frame):
        return self.checker.check(frame)

    def test_first_frame_is_ok_without_baseline(self):
        frame = make_profile2_frame(self.message, 7)
        result = self.check(frame)
        self.assertEqual(result.status, E2EStatus.OK)
        self.assertEqual(result.sequence_counter, 7)
        self.assertIsNone(result.last_sequence_counter)
        self.assertEqual(self.checker.last_sequence_counter, 7)

    def test_wrap_around_and_in_order_deltas(self):
        # 14 -> 15 -> 0 (wrap) -> 1 -> 4 (delta 3): all in order.
        for seq in [14, 15, 0, 1, 4]:
            self.assertEqual(
                self.check(make_profile2_frame(self.message, seq)).status,
                E2EStatus.OK)

    def test_delta_four_is_a_jump(self):
        self.check(make_profile2_frame(self.message, 0))
        jump = self.check(make_profile2_frame(self.message, 4))
        self.assertEqual(jump.status, E2EStatus.SEQUENCE_JUMP)
        self.assertEqual(jump.sequence_counter, 4)
        self.assertEqual(jump.last_sequence_counter, 0)
        # the new counter is accepted immediately
        self.assertEqual(self.checker.last_sequence_counter, 4)
        self.assertEqual(
            self.check(make_profile2_frame(self.message, 5)).status,
            E2EStatus.OK)

    def test_repeated_counter(self):
        self.check(make_profile2_frame(self.message, 3))
        repeated = self.check(make_profile2_frame(self.message, 3))
        self.assertEqual(repeated.status, E2EStatus.REPEATED)
        self.assertEqual(repeated.sequence_counter, 3)
        self.assertEqual(repeated.last_sequence_counter, 3)
        # the baseline does not change on a repeat, so the following
        # in-order frame is judged against the same counter
        self.assertEqual(
            self.check(make_profile2_frame(self.message, 4)).status,
            E2EStatus.OK)

    def test_crc_error_does_not_advance_baseline(self):
        self.check(make_profile2_frame(self.message, 0))
        for seq in (5, 6, 1):
            result = self.check(
                make_profile2_frame(self.message, seq, crc_ok=False))
            self.assertEqual(result.status, E2EStatus.CRC_ERROR)
            # the baseline stays at the last accepted counter (0)
            self.assertEqual(result.last_sequence_counter, 0)
            self.assertEqual(self.checker.last_sequence_counter, 0)

        # the first valid frame after the CRC errors is compared
        # against the frozen baseline: delta 1 -> OK. No spurious jump.
        self.assertEqual(
            self.check(make_profile2_frame(self.message, 1)).status,
            E2EStatus.OK)

    def test_crc_error_dominates_sequence_findings(self):
        # a CRC-bad frame whose counter would also be a jump must be
        # reported as CRC_ERROR only
        self.check(make_profile2_frame(self.message, 0))
        result = self.check(
            make_profile2_frame(self.message, 9, crc_ok=False))
        self.assertEqual(result.status, E2EStatus.CRC_ERROR)
        self.assertNotEqual(result.status, E2EStatus.SEQUENCE_JUMP)

    def test_damaged_sequence_field_is_caught_by_crc(self):
        good = make_profile2_frame(self.message, 2)
        self.check(good)

        # flip the low nibble of the sequence counter byte without
        # repairing the CRC: this must surface as a CRC error
        damaged = bytearray(good)
        damaged[1] ^= 0x0f
        result = self.check(bytes(damaged))
        self.assertEqual(result.status, E2EStatus.CRC_ERROR)
        self.assertEqual(result.sequence_counter, 2 ^ 0x0f)

        # the frozen baseline keeps the next valid frame in order
        self.assertEqual(
            self.check(make_profile2_frame(self.message, 3)).status,
            E2EStatus.OK)

    def test_frame_too_short(self):
        full = make_profile2_frame(self.message, 7)
        self.check(full)

        for short in (full[:1], b''):
            result = self.check(short)
            self.assertEqual(result.status, E2EStatus.FRAME_TOO_SHORT)
            self.assertIsNone(result.sequence_counter)
            self.assertIsNone(result.last_sequence_counter)

        # too-short frames do not move the baseline
        self.assertEqual(self.checker.last_sequence_counter, 7)
        self.assertEqual(
            self.check(make_profile2_frame(self.message, 8)).status,
            E2EStatus.OK)

    def test_sequence_position_overrides_database_signal(self):
        # the database declares message1_SeqCounter at byte 0, but
        # Profile 2 puts the counter in the low nibble of byte 1
        frame = make_profile2_frame(self.message, 0x0a)
        result = self.check(frame)
        self.assertEqual(result.sequence_counter, 0x0a)

    def test_reset_forgets_baseline(self):
        self.check(make_profile2_frame(self.message, 1))
        self.checker.reset()
        self.assertIsNone(self.checker.last_sequence_counter)
        result = self.check(make_profile2_frame(self.message, 9))
        self.assertEqual(result.status, E2EStatus.OK)
        self.assertIsNone(result.last_sequence_counter)


class E2EProfile5SecOCTest(unittest.TestCase):

    def setUp(self):
        db = cantools.database.load_file(ARXML)
        # Message3 is SecOC secured: only its first 4 bytes are E2E
        # protected, the remaining 2 bytes are the security trailer.
        self.message = db.get_message_by_name('Message3')
        self.assertTrue(self.message.autosar.is_secured)
        self.assertEqual(self.message.autosar.e2e.payload_length, 4)
        self.checker = E2EProtection(self.message)

    def test_wrap_around_with_8bit_counter(self):
        for seq, expected in [(254, E2EStatus.OK),
                              (255, E2EStatus.OK),
                              (0, E2EStatus.OK),
                              (1, E2EStatus.OK)]:
            self.assertEqual(
                self.checker.check(
                    make_profile5_frame(self.message, seq)).status,
                expected)

    def test_jump_repeat_and_immediate_reacceptance(self):
        self.checker.check(make_profile5_frame(self.message, 3))
        jump = self.checker.check(make_profile5_frame(self.message, 9))
        self.assertEqual(jump.status, E2EStatus.SEQUENCE_JUMP)
        self.assertEqual(jump.last_sequence_counter, 3)

        repeated = self.checker.check(make_profile5_frame(self.message, 9))
        self.assertEqual(repeated.status, E2EStatus.REPEATED)

        self.assertEqual(
            self.checker.check(make_profile5_frame(self.message, 10)).status,
            E2EStatus.OK)

    def test_secoc_trailer_is_ignored(self):
        first = make_profile5_frame(self.message, 0, trailer=b'\x01\x02')
        self.assertEqual(self.checker.check(first).status, E2EStatus.OK)

        # changing the trailer must neither break the CRC nor count as
        # a length change; the protected prefix is untouched
        second = make_profile5_frame(self.message, 1, trailer=b'\xff\xee')
        self.assertEqual(self.checker.check(second).status, E2EStatus.OK)

    def test_protected_prefix_alone_is_sufficient(self):
        self.checker.check(make_profile5_frame(self.message, 5))
        prefix_only = make_profile5_frame(self.message, 6, length=4)
        self.assertEqual(len(prefix_only), 4)
        self.assertEqual(self.checker.check(prefix_only).status,
                         E2EStatus.OK)

    def test_frame_shorter_than_protected_prefix(self):
        result = self.checker.check(make_profile5_frame(
            self.message, 5, length=3))
        self.assertEqual(result.status, E2EStatus.FRAME_TOO_SHORT)
        self.assertIsNone(result.sequence_counter)

    def test_crc_error_and_frozen_baseline(self):
        self.checker.check(make_profile5_frame(self.message, 1))
        bad = make_profile5_frame(self.message, 2, crc_ok=False)
        self.assertEqual(self.checker.check(bad).status,
                         E2EStatus.CRC_ERROR)
        self.assertEqual(self.checker.last_sequence_counter, 1)
        self.assertEqual(
            self.checker.check(make_profile5_frame(
                self.message, 2)).status,
            E2EStatus.OK)

    def test_sequence_counter_read_from_third_byte(self):
        # the database signal message3_SeqCounter is declared at byte
        # 1, but Profile 5 uses the whole third byte
        frame = make_profile5_frame(self.message, 0x7b)
        result = self.checker.check(frame)
        self.assertEqual(result.sequence_counter, 0x7b)


class E2EMessageApiTest(unittest.TestCase):

    def setUp(self):
        self.db = cantools.database.load_file(ARXML)

    def test_protection_properties(self):
        message = self.db.get_message_by_name('Message1')
        self.assertTrue(message.is_e2e_protected())
        self.assertIs(message.get_e2e_protection(),
                      message.get_e2e_protection())
        self.assertEqual(message.get_e2e_protection().profile, 'profile2')

    def test_unprotected_message_rejects_check(self):
        message = self.db.get_message_by_name('Message2')
        self.assertFalse(message.is_e2e_protected())
        with self.assertRaises(ValueError):
            message.get_e2e_protection()
        with self.assertRaises(ValueError):
            message.check_e2e(b'\x00' * message.length)

    def test_state_is_kept_per_message(self):
        message1 = self.db.get_message_by_name('Message1')
        message3 = self.db.get_message_by_name('Message3')

        f1 = make_profile2_frame(message1, 0)
        f3 = make_profile5_frame(message3, 0)
        self.assertEqual(message1.check_e2e(f1).status, E2EStatus.OK)
        self.assertEqual(message3.check_e2e(f3).status, E2EStatus.OK)

        # each message keeps its own baseline and does not see the
        # frames of the other one
        self.assertEqual(message1.check_e2e(
            make_profile2_frame(message1, 1)).status, E2EStatus.OK)
        self.assertEqual(message3.check_e2e(
            make_profile5_frame(message3, 1)).status, E2EStatus.OK)


class E2EContainerTest(unittest.TestCase):

    def setUp(self):
        db = cantools.database.load_file(ARXML)
        self.db = db
        self.container = db.get_message_by_name('OneToContainThemAll')
        self.message1 = db.get_message_by_name('Message1')
        self.message3 = db.get_message_by_name('Message3')

    def test_contained_messages_keep_their_own_state(self):
        frame1 = wrap_container([
            (0x0a0b0c, make_profile2_frame(self.message1, 3)),
            (0x040506, make_profile5_frame(self.message3, 5)),
            (0x0a0b0c, make_profile2_frame(self.message1, 4, crc_ok=False)),
            (0x040506, make_profile5_frame(self.message3, 6, length=2)),
        ])

        self.container.decode(frame1,
                              decode_containers=True,
                              check_e2e=True)
        results = self.container.get_contained_e2e_results()
        self.assertEqual([r.status for r in results],
                         [E2EStatus.OK,
                          E2EStatus.OK,
                          E2EStatus.CRC_ERROR,
                          E2EStatus.FRAME_TOO_SHORT])

        # second container frame: the bad-CRC and too-short entries of
        # the first frame did not move the baselines, so both counters
        # are still in order
        frame2 = wrap_container([
            (0x0a0b0c, make_profile2_frame(self.message1, 5)),
            (0x040506, make_profile5_frame(self.message3, 6)),
        ])
        self.container.decode(frame2,
                              decode_containers=True,
                              check_e2e=True)
        results = self.container.get_contained_e2e_results()
        self.assertEqual([r.status for r in results],
                         [E2EStatus.OK, E2EStatus.OK])

        # repeating the very same container frame must flag repeats
        self.container.decode(frame2,
                              decode_containers=True,
                              check_e2e=True)
        results = self.container.get_contained_e2e_results()
        self.assertEqual([r.status for r in results],
                         [E2EStatus.REPEATED, E2EStatus.REPEATED])

    def test_e2e_results_unavailable_without_check(self):
        # a container that never had E2E checking enabled reports no
        # results (its baseline is also still untouched)
        fresh_db = cantools.database.load_file(ARXML)
        fresh_container = fresh_db.get_message_by_name('OneToContainThemAll')
        self.assertEqual(fresh_container.get_contained_e2e_results(), [])

        frame = wrap_container([
            (0x0a0b0c, make_profile2_frame(
                fresh_db.get_message_by_name('Message1'), 0)),
        ])
        fresh_container.decode(frame, decode_containers=True)
        # decoding without check_e2e leaves the result list empty
        self.assertEqual(fresh_container.get_contained_e2e_results(), [])

    def test_python_and_cli_report_the_same_status(self):
        # two independent containers walk the same frame sequence
        api_db = cantools.database.load_file(ARXML)
        api_container = api_db.get_message_by_name('OneToContainThemAll')
        api_message1 = api_db.get_message_by_name('Message1')

        frame = wrap_container([
            (0x0a0b0c, make_profile2_frame(api_message1, 9)),
        ])

        api_container.decode(frame,
                             decode_containers=True,
                             check_e2e=True)
        python_status = api_container.get_contained_e2e_results()[0].status

        cli = format_message_by_frame_id(
            self.db,
            self.container.frame_id,
            frame,
            decode_choices=True,
            single_line=True,
            decode_containers=True,
            allow_truncated=False,
            allow_excess=True,
            check_e2e=True)
        self.assertIn(f'[E2E: {python_status}]', cli)


class E2ECommandLineTest(unittest.TestCase):

    def setUp(self):
        self.db = cantools.database.load_file(ARXML)
        self.message = self.db.get_message_by_name('Message1')

    def _cli(self, frame, single_line=True, check_e2e=True):
        return format_message_by_frame_id(
            self.db,
            self.message.frame_id,
            frame,
            decode_choices=True,
            single_line=single_line,
            decode_containers=False,
            allow_truncated=False,
            allow_excess=True,
            check_e2e=check_e2e)

    def test_cli_status_matches_python_status_for_every_state(self):
        # Two independent checkers walk the same frame sequence: one
        # is driven through the CLI formatting path and one through
        # the Python API directly.
        cli_message = self.db.get_message_by_name('Message1')
        api_message = cantools.database.load_file(ARXML) \
            .get_message_by_name('Message1')

        cases = [
            (make_profile2_frame(cli_message, 0), E2EStatus.OK),
            (make_profile2_frame(cli_message, 0), E2EStatus.REPEATED),
            (make_profile2_frame(cli_message, 4), E2EStatus.SEQUENCE_JUMP),
            (make_profile2_frame(cli_message, 5, crc_ok=False),
             E2EStatus.CRC_ERROR),
            (make_profile2_frame(cli_message, 6)[:1],
             E2EStatus.FRAME_TOO_SHORT),
        ]

        for frame, expected in cases:
            python_status = api_message.check_e2e(frame).status
            self.assertEqual(python_status, expected)

            cli = format_message_by_frame_id(
                self.db,
                cli_message.frame_id,
                frame,
                decode_choices=True,
                single_line=True,
                decode_containers=False,
                allow_truncated=False,
                allow_excess=True,
                check_e2e=True)
            self.assertIn(f'[E2E: {expected}]', cli)

    def test_too_short_keeps_decode_error_and_adds_status(self):
        frame = make_profile2_frame(self.message, 1)[:1]
        cli = self._cli(frame)
        self.assertIn('Wrong data size: 1 instead of 9 bytes', cli)
        self.assertIn('[E2E: FRAME_TOO_SHORT]', cli)

    def test_output_without_flag_is_unchanged(self):
        frame = make_profile2_frame(self.message, 2, crc_ok=False)
        without = self._cli(frame, check_e2e=False)
        with_flag = self._cli(frame, check_e2e=True)
        self.assertNotIn('[E2E:', without)
        self.assertEqual(without, with_flag.replace(' [E2E: CRC_ERROR]', ''))

    def test_decode_subparser_end_to_end(self):
        argv = ['cantools', 'decode', '--single-line', '--e2e', ARXML]
        good = make_profile2_frame(self.message, 3)
        bad = make_profile2_frame(self.message, 3, crc_ok=False)

        def candump_line(frame):
            data = ' '.join(f'{byte:02X}' for byte in frame)
            return f'  vcan0  005   [{len(frame)}]  {data}\n'

        input_data = candump_line(good) + candump_line(bad)

        stdout = StringIO()
        with patch('sys.stdin', StringIO(input_data)):
            with patch('sys.stdout', stdout):
                with patch('sys.argv', argv):
                    cantools._main()

        output = stdout.getvalue()
        self.assertIn('[E2E: OK]', output.splitlines()[0])
        self.assertIn('[E2E: CRC_ERROR]', output.splitlines()[1])


if __name__ == '__main__':
    unittest.main()
