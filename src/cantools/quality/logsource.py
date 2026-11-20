# Forward-only reader for absolute-time ``candump -L`` text logs.

from __future__ import annotations

import binascii
import datetime
import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Optional

from ..logreader import DataFrame, TimestampFormat
from .types import Diagnostic, DiagnosticKind

# candump -L / -l, e.g.
#   (1579857014.345944) can2 486#82967A6B006B07F8
#   (1579857014.345944) can2 14C##16A0...        (CAN FD)
#   (1579857014.345944) can2 486#R              (remote request)
_RE_LOG = re.compile(
    r"^\s*\((?P<timestamp>\d+\.\d+)\)\s+"
    r"(?P<channel>[A-Za-z0-9_]+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]+)#"
    r"(?:##?(?P<fd_flags>[0-9A-Fa-f]))?"
    r"(?P<can_data>[0-9A-Fa-f]*)\s*$"
)
# Same overall shape as a candump -L entry, but with a payload that is
# not valid byte hex. It is a recognized frame location with a bad
# payload, kept distinct from a wholly malformed line.
_RE_BAD_PAYLOAD = re.compile(
    r"^\s*\((?P<timestamp>\d+\.\d+)\)\s+"
    r"(?P<channel>[A-Za-z0-9_]+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]+)#+"
    r"(?P<can_data>[0-9A-Za-z]*)\s*$"
)
_RE_REMOTE = re.compile(
    r"^\s*\((?P<timestamp>\d+\.\d+)\)\s+"
    r"(?P<channel>[A-Za-z0-9_]+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]+)#(?P<remote>R)\s*$"
)


class TimeRegressionError(ValueError):
    """Raised when a recognized frame's timestamp is older than the
    previous recognized frame's timestamp (ties are allowed and resolved
    by line order)."""

    def __init__(self, line: int, previous_us: int, current_us: int,
                 raw_line: str):
        super().__init__(
            f"line {line}: timestamp {current_us}us is older than the "
            f"previous recognized frame at {previous_us}us; logs must be "
            "read forward in time"
        )
        self.line = line
        self.previous_us = previous_us
        self.current_us = current_us
        self.raw_line = raw_line


@dataclass(frozen=True)
class LogLine:
    """One successfully parsed, forward-in-time data frame with its
    origin."""

    frame: DataFrame
    line: int
    raw_line: str
    timestamp_us: int


def parse_timestamp_us(seconds_text: str) -> int:
    """Parse an absolute seconds-since-epoch fractional timestamp into
    whole microseconds, keeping the full microsecond precision of
    ``candump -L`` (6 fractional digits).

    More than six fractional digits are truncated rather than rounded so
    the result always corresponds to a microsecond grid point present in
    the source text.
    """
    integer, _, fraction = seconds_text.partition(".")
    fraction = (fraction + "000000")[:6]
    return int(integer) * 1_000_000 + int(fraction)


def parse_log_line(line: str) -> tuple[Optional[DataFrame], Optional[DiagnosticKind], Optional[str]]:
    """Parse one ``candump -L`` line.

    Returns ``(frame, diagnostic_kind, message)``. Exactly one of
    ``frame`` / ``diagnostic_kind`` is non-``None``. A remote request is
    reported as a diagnostic and never becomes a frame.
    """
    remote = _RE_REMOTE.match(line)
    if remote is not None:
        return None, DiagnosticKind.REMOTE_FRAME, "remote request frame"

    mo = _RE_LOG.match(line)
    if mo is None:
        bad = _RE_BAD_PAYLOAD.match(line)
        if bad is not None:
            return None, DiagnosticKind.BAD_PAYLOAD, (
                f"payload {bad.group('can_data')!r} is not valid byte hex"
            )
        return None, DiagnosticKind.MALFORMED_LINE, "line is not a candump -L log entry"

    hex_data = mo.group("can_data") or ""
    # A payload must be whole bytes (pairs of hex digits). A line with
    # the right overall shape but an odd/non-byte payload is a bad
    # payload, not a syntactically malformed line.
    if len(hex_data) % 2:
        return None, DiagnosticKind.BAD_PAYLOAD, (
            f"payload {hex_data!r} does not consist of whole bytes"
        )

    try:
        timestamp_us = parse_timestamp_us(mo.group("timestamp"))
        timestamp = datetime.datetime.fromtimestamp(
            timestamp_us / 1_000_000, tz=datetime.timezone.utc
        )
    except (ValueError, OverflowError) as e:
        return None, DiagnosticKind.MALFORMED_LINE, f"bad timestamp: {e}"

    frame_id = int(mo.group("can_id"), 16)
    is_extended = len(mo.group("can_id")) > 3

    try:
        data = binascii.unhexlify(hex_data)
    except (binascii.Error, ValueError) as e:
        return None, DiagnosticKind.BAD_PAYLOAD, f"bad payload hex: {e}"

    frame = DataFrame(
        channel=mo.group("channel"),
        frame_id=frame_id,
        is_extended_frame=is_extended,
        data=data,
        is_remote_frame=False,
        timestamp=timestamp,
        timestamp_format=TimestampFormat.ABSOLUTE,
    )
    frame.timestamp_us = timestamp_us  # type: ignore[attr-defined]
    return frame, None, None


class CandumpLogSource:
    """Iterate an absolute-time candump log exactly once, forward only.

    Recognized data frames are yielded as :class:`LogLine` in file order
    (same-timestamp frames keep their line order). Everything else is
    collected into :attr:`diagnostics` with its 1-based source line
    number. A recognized frame whose timestamp is older than the previous
    recognized frame raises :class:`TimeRegressionError`.

    The source owns the underlying stream only when it opened the file
    itself; in that case the file is closed when the iterator is closed
    or garbage collected.
    """

    def __init__(self, stream: io.TextIOBase, *,
                 diagnostics: Optional[list[Diagnostic]] = None):
        self._stream = stream
        self._owns_stream = False
        self._line_number = 0
        self._last_timestamp_us: Optional[int] = None
        self._closed = False
        self._consumed = False
        self.diagnostics: list[Diagnostic] = (
            diagnostics if diagnostics is not None else []
        )

    @classmethod
    def open(cls, path: str, *,
             diagnostics: Optional[list[Diagnostic]] = None) -> CandumpLogSource:
        stream = open(path, encoding="utf-8", newline="")
        source = cls(stream, diagnostics=diagnostics)
        source._owns_stream = True
        return source

    def __iter__(self) -> Iterator[LogLine]:
        return self.iter_frames()

    def iter_frames(self) -> Iterator[LogLine]:
        if self._consumed:
            raise RuntimeError("log source can only be consumed once")
        self._consumed = True
        while True:
            raw = self._stream.readline()
            if raw == "":
                return
            self._line_number += 1
            line = raw.strip("\r\n")
            if line == "":
                continue

            frame, kind, message = parse_log_line(line)
            if kind is not None:
                ts = self._timestamp_if_possible(line)
                self.diagnostics.append(Diagnostic(
                    line=self._line_number,
                    kind=kind,
                    timestamp_us=ts,
                    message=message or "",
                    raw_line=line,
                ))
                continue

            assert frame is not None
            ts_us = frame.timestamp_us  # type: ignore[attr-defined]
            if (
                self._last_timestamp_us is not None
                and ts_us < self._last_timestamp_us
            ):
                raise TimeRegressionError(
                    self._line_number, self._last_timestamp_us,
                    ts_us, line,
                )
            self._last_timestamp_us = ts_us
            yield LogLine(
                frame=frame,
                line=self._line_number,
                raw_line=line,
                timestamp_us=ts_us,
            )

    @staticmethod
    def _timestamp_if_possible(line: str) -> Optional[int]:
        mo = re.match(r"^\s*\((?P<timestamp>\d+\.\d+)\)", line)
        if mo is None:
            return None
        try:
            return parse_timestamp_us(mo.group("timestamp"))
        except ValueError:
            return None

    def close(self) -> None:
        if not self._closed and self._owns_stream:
            self._stream.close()
        self._closed = True

    def __enter__(self) -> CandumpLogSource:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
