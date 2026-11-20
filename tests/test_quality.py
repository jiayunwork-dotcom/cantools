"""Tests for the quality-aware signal time series package.

These tests use only the synthetic, locally generated DBC/ARXML and
candump logs under tests/files/quality -- no bus hardware.
"""

from __future__ import annotations

import io
import os
import resource
import unittest

import cantools
from cantools.quality import (
    CandumpLogSource,
    DiagnosticKind,
    QualityTimeSeries,
    SignalQuality,
    SignalSelector,
    StalenessBasis,
    StalenessPolicy,
    TimeRegressionError,
    build_series_from_file,
    seconds_to_us,
    validate_grid,
    write_long_csv,
    write_wide_csv,
)
from cantools.quality.curves import build_curve

FILES = os.path.join(os.path.dirname(__file__), "files", "quality")
DBC = os.path.join(FILES, "quality_demo.dbc")
ARXML = os.path.join(FILES, "invalid_demo.arxml")
SHORT_LOG = os.path.join(FILES, "demo_short.log")
REGRESSION_LOG = os.path.join(FILES, "demo_regression.log")

# Grid in microseconds: 1000.0 .. 1000.9 step 0.1s.
T0 = 1_000_000_000
T9 = 1_000_900_000
STEP = 100_000


def load_db():
    return cantools.database.load_file(DBC)


def select(db, *specs):
    selectors = [
        SignalSelector(channel, message, signal)
        for channel, message, signal in specs
    ]
    return cantools.quality.select_signals(db, selectors)


def run_series(db, selections, log_path=SHORT_LOG, **kwargs):
    defaults = dict(start_us=T0, stop_us=T9, step_us=STEP)
    defaults.update(kwargs)
    stream = open(log_path, encoding="utf-8")
    source = CandumpLogSource(stream)
    series = QualityTimeSeries(db, source, selections, **defaults)
    points = [point for batch in series.iter_batches() for point in batch]
    return series, points


def grid_index(points, key):
    """Return {grid_offset_seconds: QualityPoint} for one key."""
    out = {}
    for point in points:
        if point.key == key:
            out[(point.timestamp_us - T0) / 1_000_000] = point
    return out


class LogSourceTests(unittest.TestCase):

    def test_microsecond_precision_and_order(self):
        source = CandumpLogSource(open(SHORT_LOG, encoding="utf-8"))
        frames = list(source)
        self.assertEqual(frames[0].timestamp_us, 1_000_000_000)
        # six fractional digits are preserved exactly
        self.assertEqual(frames[0].line, 1)
        timestamps = [f.timestamp_us for f in frames]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_diagnostics_keep_line_numbers(self):
        db = load_db()
        selections = select(
            db, ("can0", "EngineStatus", "EngineSpeed"))
        series, _ = run_series(db, selections)
        kinds = {d.line: d.kind for d in series.diagnostics}
        self.assertEqual(kinds[11], DiagnosticKind.REMOTE_FRAME)
        self.assertEqual(kinds[12], DiagnosticKind.UNKNOWN_MESSAGE)
        self.assertEqual(kinds[13], DiagnosticKind.BAD_PAYLOAD)
        self.assertEqual(kinds[14], DiagnosticKind.MALFORMED_LINE)

    def test_time_regression_raises(self):
        source = CandumpLogSource(open(REGRESSION_LOG, encoding="utf-8"))
        with self.assertRaises(TimeRegressionError) as ctx:
            list(source)
        self.assertEqual(ctx.exception.line, 2)

    def test_parse_timestamp_truncates_extra_digits(self):
        from cantools.quality.logsource import parse_timestamp_us
        # 1000 whole seconds + 123456us; extra digits are truncated
        self.assertEqual(parse_timestamp_us("1000.123456789"),
                         1_000_000_000 + 123_456)

    def test_early_close_releases_owned_file(self):
        series = build_series_from_file(
            DBC, SHORT_LOG,
            [SignalSelector("can0", "EngineStatus", "EngineSpeed")],
            start_s=1000.0, stop_s=1000.9, step_s=0.1,
        )
        for index, _batch in enumerate(series.iter_batches()):
            if index == 1:
                series.close()
                break
        self.assertTrue(series._source._stream.closed)


class GridValidationTests(unittest.TestCase):

    def test_grid_rules(self):
        validate_grid(0, 0, 1)
        validate_grid(0, 10, 2)
        with self.assertRaises(ValueError):
            validate_grid(10, 9, 1)
        with self.assertRaises(ValueError):
            validate_grid(0, 10, 0)
        with self.assertRaises(ValueError):
            validate_grid(0, 10, -1)

    def test_seconds_to_us(self):
        self.assertEqual(seconds_to_us(1.5), 1_500_000)

    def test_explicit_limit_must_be_finite_positive(self):
        key = object()
        for bad in (0, -0.1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                cantools.quality.StalenessEstimator({key: bad})


class HandComputedDetailTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = load_db()
        cls.selections = select(
            cls.db,
            ("can0", "EngineStatus", "EngineSpeed"),
            ("can1", "EngineStatus", "EngineSpeed"),
            ("can0", "MuxMessage", "BranchA"),
            ("can0", "MuxMessage", "BranchB"),
            ("can0", "MuxMessage", "CommonSig"),
            ("can0", "ShortMessage", "FirstByte"),
            ("can0", "ShortMessage", "ThirdByte"),
            ("can0", "EventMessage", "EventState"),
        )
        cls.series, cls.points = run_series(cls.db, cls.selections)
        cls.by_key = {}
        for point in cls.points:
            cls.by_key.setdefault(point.key, []).append(point)

    def _key(self, channel, message, signal):
        return next(
            s.key for s in self.selections
            if s.channel == channel and s.message_name == message
            and s.signal_name == signal
        )

    def test_same_timestamp_line_order_wins(self):
        speed = grid_index(self.points,
                           self._key("can0", "EngineStatus", "EngineSpeed"))
        # lines 1 (raw400->100) then 3 (raw440->110) at the same time;
        # line 3 wins.
        point = speed[0.0]
        self.assertEqual(point.quality, SignalQuality.FRESH)
        self.assertEqual(point.value, 110.0)
        self.assertEqual(point.source.line, 3)
        self.assertEqual(point.age_us, 0)

    def test_two_channels_same_id_are_separate(self):
        can0 = grid_index(self.points,
                          self._key("can0", "EngineStatus", "EngineSpeed"))
        can1 = grid_index(self.points,
                          self._key("can1", "EngineStatus", "EngineSpeed"))
        self.assertNotEqual(can0[0.0].key, can1[0.0].key)
        self.assertEqual(can1[0.0].value, 200.0)  # raw 800 * 0.25
        self.assertEqual(can1[0.0].source.line, 2)
        # can1 never updates again -> held then expired, never mixed
        self.assertEqual(can1[0.1].quality, SignalQuality.HELD)
        self.assertEqual(can1[0.3].quality, SignalQuality.EXPIRED)

    def test_held_then_expired_at_limit(self):
        speed = grid_index(self.points,
                           self._key("can0", "EngineStatus", "EngineSpeed"))
        # last frame before silence at t=0.2; default limit 0.2s
        self.assertEqual(speed[0.3].quality, SignalQuality.HELD)
        self.assertEqual(speed[0.4].quality, SignalQuality.HELD)  # age == limit
        self.assertEqual(speed[0.5].quality, SignalQuality.EXPIRED)
        # recovery needs a new good frame (line 16 at t=0.7)
        self.assertEqual(speed[0.7].quality, SignalQuality.FRESH)
        self.assertEqual(speed[0.7].value, 140.0)

    def test_unseen_has_no_source_and_no_zero_fill(self):
        branch_a = grid_index(
            self.points, self._key("can0", "MuxMessage", "BranchA"))
        unseen = branch_a[0.0]
        self.assertEqual(unseen.quality, SignalQuality.UNSEEN)
        self.assertIsNone(unseen.value)
        self.assertIsNone(unseen.source)
        self.assertIsNone(unseen.age_us)

    def test_multiplex_switch_and_recovery(self):
        branch_a = grid_index(
            self.points, self._key("can0", "MuxMessage", "BranchA"))
        branch_b = grid_index(
            self.points, self._key("can0", "MuxMessage", "BranchB"))
        # t=0.1: branch A held from line 4; B inactive
        self.assertEqual(branch_a[0.1].quality, SignalQuality.HELD)
        self.assertEqual(branch_a[0.1].value, 40)  # raw 20 * scale 2
        self.assertEqual(branch_b[0.1].quality,
                         SignalQuality.BRANCH_INACTIVE)
        # t=0.2: switched to B at line 8
        self.assertEqual(branch_a[0.2].quality,
                         SignalQuality.BRANCH_INACTIVE)
        self.assertEqual(branch_b[0.2].quality, SignalQuality.HELD)
        self.assertEqual(branch_b[0.2].value, 30)
        # t=0.3: unknown selector line 10 -> decode failure blocks both,
        # and it must not revive any older good value
        self.assertEqual(branch_a[0.3].quality,
                         SignalQuality.DECODE_FAILED)
        self.assertEqual(branch_b[0.3].quality,
                         SignalQuality.DECODE_FAILED)
        self.assertIsNone(branch_a[0.3].value)
        self.assertEqual(branch_a[0.3].source.line, 10)

    def test_short_payload_truncation_blocks_old_value(self):
        first = grid_index(
            self.points, self._key("can0", "ShortMessage", "FirstByte"))
        third = grid_index(
            self.points, self._key("can0", "ShortMessage", "ThirdByte"))
        # line 5 is only 2 bytes: FirstByte complete, ThirdByte truncated
        self.assertEqual(first[0.1].quality, SignalQuality.HELD)
        self.assertEqual(first[0.1].value, 10)
        self.assertEqual(third[0.1].quality, SignalQuality.TRUNCATED)
        self.assertIsNone(third[0.1].value)
        # full frame line 15 recovers ThirdByte with a fresh sample
        self.assertEqual(third[0.4].quality, SignalQuality.HELD)
        self.assertEqual(third[0.4].value, 3)
        self.assertEqual(third[0.4].source.line, 15)

    def test_event_message_uses_provisional_limit(self):
        state = grid_index(
            self.points, self._key("can0", "EventMessage", "EventState"))
        self.assertEqual(state[0.1].quality, SignalQuality.FRESH)
        self.assertEqual(str(state[0.1].value), "Active")
        # no database cycle -> insufficient evidence basis, held across
        # the long gap because provisional limit is large
        self.assertEqual(state[0.7].quality, SignalQuality.HELD)
        self.assertIn(
            state[0.1].staleness.basis,
            (StalenessBasis.INSUFFICIENT_EVIDENCE,
             StalenessBasis.OBSERVED_HISTORY),
        )

    def test_every_point_carries_trace_fields(self):
        for point in self.points:
            self.assertIsNotNone(point.staleness)
            self.assertIn(point.quality, SignalQuality)
            if point.quality != SignalQuality.UNSEEN:
                self.assertIsNotNone(point.source)
                self.assertIsNotNone(point.source_timestamp_us)
                self.assertGreaterEqual(point.age_us, 0)
                self.assertEqual(
                    point.age_us,
                    point.timestamp_us - point.source_timestamp_us,
                )


class RawInvalidTests(unittest.TestCase):

    def test_plain_decode_unchanged_without_flag(self):
        db = cantools.database.load_file(ARXML)
        message = db.get_message_by_name("Test_Frame")
        data = message.encode({"IMessage_1": 31, "IMessage_2": 127},
                              scaling=False, strict=False)
        plain = message.decode(data)
        # historical behavior: raw invalid still comes back as a value
        self.assertEqual(set(plain), {"IMessage_1", "IMessage_2"})

    def test_quality_decode_flags_raw_invalid_without_scaling(self):
        db = cantools.database.load_file(ARXML)
        message = db.get_message_by_name("Test_Frame")
        data = message.encode({"IMessage_1": 31, "IMessage_2": 127},
                              scaling=False, strict=False)
        result = message.decode(data, with_quality=True)
        s1 = result.statuses["IMessage_1"]
        s2 = result.statuses["IMessage_2"]
        self.assertEqual(s1.quality, SignalQuality.INVALID)
        self.assertIsNone(s1.value)
        self.assertEqual(s1.raw_value, 31)  # raw meaning, not 25.5
        self.assertEqual(s2.quality, SignalQuality.INVALID)
        self.assertIsNone(s2.value)
        self.assertEqual(s2.raw_value, 127)  # not the enum label

    def test_invalid_blocks_then_good_recovers_in_series(self):
        db = cantools.database.load_file(ARXML)
        message = db.get_message_by_name("Test_Frame")
        good = message.encode({"IMessage_1": 5, "IMessage_2": 1},
                              scaling=False, strict=False)
        bad = message.encode({"IMessage_1": 31, "IMessage_2": 127},
                             scaling=False, strict=False)
        log = io.StringIO(
            "(1000.000000) can0 DA#" + good.hex() + "\n"
            "(1000.100000) can0 DA#" + bad.hex() + "\n"
            "(1000.200000) can0 DA#" + good.hex() + "\n"
        )
        selections = cantools.quality.select_signals(db, [
            SignalSelector("can0", "Test_Frame", "IMessage_1"),
            SignalSelector("can0", "Test_Frame", "IMessage_2"),
        ])
        source = CandumpLogSource(log)
        series = QualityTimeSeries(
            db, source, selections,
            start_us=T0, stop_us=T0 + 300_000, step_us=STEP,
            explicit_limits_s={selections[0].key: 100,
                               selections[1].key: 100},
        )
        points = [p for b in series.iter_batches() for p in b]
        im1 = grid_index(points, selections[0].key)
        self.assertEqual(im1[0.0].quality, SignalQuality.FRESH)
        self.assertEqual(im1[0.1].quality, SignalQuality.INVALID)
        self.assertIsNone(im1[0.1].value)
        self.assertEqual(im1[0.2].quality, SignalQuality.FRESH)


class CsvAndCurveTests(unittest.TestCase):

    def test_long_and_wide_csv_match_python_points(self):
        db = load_db()
        selections = select(
            db,
            ("can0", "EngineStatus", "EngineSpeed"),
            ("can0", "MuxMessage", "BranchA"),
        )
        stream = open(SHORT_LOG, encoding="utf-8")
        series = QualityTimeSeries(
            db, CandumpLogSource(stream), selections,
            start_us=T0, stop_us=T0 + 300_000, step_us=STEP,
        )
        long_buf = io.StringIO()
        long_rows = write_long_csv(series, long_buf)
        # 4 grid points * 2 signals
        self.assertEqual(long_rows, 8)
        # BranchA hits branch_inactive / decode_failed inside the window
        self.assertIn("decode_failed", long_buf.getvalue())
        self.assertIn("branch_inactive", long_buf.getvalue())

        stream2 = open(SHORT_LOG, encoding="utf-8")
        series2 = QualityTimeSeries(
            db, CandumpLogSource(stream2), selections,
            start_us=T0, stop_us=T0 + 300_000, step_us=STEP,
        )
        wide_buf = io.StringIO()
        wide_rows = write_wide_csv(series2, wide_buf)
        self.assertEqual(wide_rows, 4)
        # identity and quality survive in column headers
        self.assertIn("can0:EngineStatus.EngineSpeed", wide_buf.getvalue())
        self.assertIn(".quality", wide_buf.getvalue())

    def test_curve_breaks_across_unavailable_interval(self):
        db = load_db()
        selections = select(
            db, ("can0", "EngineStatus", "EngineSpeed"))
        _, points = run_series(db, selections)
        curve = build_curve(points)
        # fresh/held runs before expiry and after recovery are separate
        self.assertGreaterEqual(len(curve.segments), 2)
        for segment in curve.segments:
            self.assertTrue(all(y is not None for y in segment.y))
        qualities = {q for _, q, _ in curve.unavailable}
        self.assertIn(SignalQuality.EXPIRED, qualities)


class StalenessPolicyTests(unittest.TestCase):

    def _series_with_policy(self, log_text, policy, cycle_s=None):
        db = cantools.database.Database()
        # build a tiny in-memory message via the public constructor
        from cantools.database.can.message import Message
        from cantools.database.can.signal import Signal
        signal = Signal("S", start=0, length=8)
        message = Message(
            frame_id=0x100, name="M", length=1, signals=[signal],
            cycle_time=int(cycle_s * 1000) if cycle_s else None,
            strict=False,
        )
        db._messages = [message]
        db.refresh()
        selections = cantools.quality.select_signals(
            db, [SignalSelector("can0", "M", "S")])
        source = CandumpLogSource(io.StringIO(log_text))
        return QualityTimeSeries(
            db, source, selections,
            start_us=T0, stop_us=T0 + 2_000_000, step_us=STEP,
            policy=policy,
        )

    def test_database_cycle_anchors_until_enough_evidence(self):
        # 100ms cycle, jitter factor 2 -> 200ms limit initially
        log = "\n".join(
            f"(1000.{i * 1000:06d}) can0 100#0{i % 10:01x}"
            for i in range(0, 300, 100)
        ) + "\n"
        policy = StalenessPolicy(min_evidence=3, jitter_factor=2.0)
        series = self._series_with_policy(log, policy, cycle_s=0.1)
        batches = list(series.iter_batches())
        first = batches[0][0]
        self.assertEqual(first.staleness.basis,
                         StalenessBasis.DATABASE_CYCLE)
        self.assertEqual(first.staleness.limit_us, 200_000)

    def test_observed_history_takes_over_after_evidence(self):
        # steady 100ms arrivals give median 100ms -> limit 200ms
        rows = "\n".join(
            f"(1000.{t * 1000:06d}) can0 100#01" for t in range(0, 600, 100)
        ) + "\n"
        policy = StalenessPolicy(min_evidence=3, jitter_factor=2.0,
                                 history_size=4)
        series = self._series_with_policy(rows, policy, cycle_s=0.1)
        points = [p for b in series.iter_batches() for p in b]
        observed = [p for p in points
                    if p.staleness.basis == StalenessBasis.OBSERVED_HISTORY]
        self.assertTrue(observed)
        # bounded history: rhythm deque never exceeds history_size
        rhythm = list(series._estimator._rhythms.values())[0]
        self.assertLessEqual(len(rhythm.intervals), 4)

    def test_jitter_does_not_cause_false_expiry(self):
        # nominal 100ms with one 180ms gap; factor 2 tolerates it
        times_ms = [0, 100, 200, 380, 480]
        rows = "\n".join(
            f"(1000.{t * 1000:06d}) can0 100#01" for t in times_ms
        ) + "\n"
        policy = StalenessPolicy(min_evidence=2, jitter_factor=2.0)
        series = self._series_with_policy(rows, policy, cycle_s=0.1)
        points = [p for b in series.iter_batches() for p in b]
        # at t=0.4 the last frame is 0.38 -> age 20ms, definitely held
        at_400 = next(p for p in points if p.timestamp_us == T0 + 400_000)
        self.assertIn(at_400.quality,
                      (SignalQuality.FRESH, SignalQuality.HELD))

    def test_event_message_no_false_expiry_when_disabled(self):
        rows = "(1000.000000) can0 100#01\n"
        policy = StalenessPolicy(expire_without_evidence=False)
        series = self._series_with_policy(rows, policy, cycle_s=None)
        points = [p for b in series.iter_batches() for p in b]
        later = next(p for p in points if p.timestamp_us == T0 + 1_000_000)
        self.assertEqual(later.quality, SignalQuality.HELD)
        self.assertIsNone(later.staleness.limit_us)
        self.assertEqual(later.staleness.basis,
                         StalenessBasis.INSUFFICIENT_EVIDENCE)

    def test_event_message_expires_with_default_when_enabled(self):
        rows = "(1000.000000) can0 100#01\n"
        policy = StalenessPolicy(
            expire_without_evidence=True, default_event_limit_s=0.5)
        series = self._series_with_policy(rows, policy, cycle_s=None)
        points = [p for b in series.iter_batches() for p in b]
        later = next(p for p in points if p.timestamp_us == T0 + 600_000)
        self.assertEqual(later.quality, SignalQuality.EXPIRED)


class MemoryScaleTests(unittest.TestCase):

    def test_resident_state_bounded_on_long_log(self):
        db = load_db()
        selections = select(
            db, ("can0", "EngineStatus", "EngineSpeed"))
        # synthesize a long log of dense frames within a 1s window, then
        # a silent tail covered by the grid
        lines = []
        for ms in range(0, 1000, 50):  # 20 dense frames, valid fractions
            lines.append(f"(1000.{ms * 1000:06d}) can0 064#90013c00")
        log_path = os.path.join(FILES, "demo_long.tmp.log")
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        try:
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            stream = open(log_path, encoding="utf-8")
            series = QualityTimeSeries(
                db, CandumpLogSource(stream), selections,
                start_us=T0, stop_us=T0 + 2_000_000, step_us=STEP,
                policy=StalenessPolicy(history_size=8),
            )
            count = 0
            for batch in series.iter_batches():
                count += len(batch)
            after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # all 21 grid points produced, no raw frame accumulation
            self.assertEqual(count, 21)
            rhythm = list(series._estimator._rhythms.values())[0]
            self.assertLessEqual(len(rhythm.intervals), 8)
            # memory growth stays modest (generous CI headroom: <64 MiB)
            self.assertLess(after - before, 64 * 1024)
        finally:
            os.remove(log_path)


if __name__ == "__main__":
    unittest.main()
