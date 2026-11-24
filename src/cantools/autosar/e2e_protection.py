# Stateful verification of AUTOSAR end-to-end (E2E) protection.
#
# In contrast to the stateless CRC helpers in ``end_to_end.py``, the
# classes in this module track the sequence counter history of a
# message and produce a single integrity status for every frame:
#
# - ``E2EStatus.OK``: CRC correct and the sequence counter is in
#   order.
# - ``E2EStatus.CRC_ERROR``: the CRC of the frame does not match.
# - ``E2EStatus.REPEATED``: the CRC is correct, but the sequence
#   counter equals the one of the previously accepted frame.
# - ``E2EStatus.SEQUENCE_JUMP``: the CRC is correct, but the
#   sequence counter advanced by more than 3 (taking the counter
#   width and its wrap-around into account).
# - ``E2EStatus.FRAME_TOO_SHORT``: the frame does not contain enough
#   bytes to hold the protected data. Such frames cannot be CRC
#   checked, and their sequence counter is not evaluated.
#
# The location and width of the sequence counter are taken from the
# E2E profile (low nibble of the second byte for profile 2, the full
# third byte for profile 5), not from any signal declared in the
# database.

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional

from .end_to_end import check_profile2_crc, check_profile5_crc

if TYPE_CHECKING:
    from ..database.can.message import Message


class E2EStatus(Enum):
    """The integrity status of a single E2E protected frame."""

    OK = 'OK'
    CRC_ERROR = 'CRC_ERROR'
    REPEATED = 'REPEATED'
    SEQUENCE_JUMP = 'SEQUENCE_JUMP'
    FRAME_TOO_SHORT = 'FRAME_TOO_SHORT'

    def __str__(self) -> str:
        return self.value


# Number of counter bits of the sequence counter used by each profile.
_SEQ_COUNTER_WIDTHS = {
    'p2': 4,
    'profile2': 4,
    'p5': 8,
    'profile5': 8,
}

# Minimum number of bytes a frame must exhibit so that the profile's
# CRC and sequence counter fields are present.
_MIN_FRAME_LENGTHS = {
    'p2': 2,
    'profile2': 2,
    'p5': 4,
    'profile5': 4,
}

# Sequence counter deltas (taking the counter width into account) up to
# and including this value are considered to be in order. A delta of
# zero means that the counter did not advance (i.e., the frame is a
# repeated one), which is reported separately.
_MAX_IN_ORDER_DELTA = 3


@dataclass(frozen=True)
class E2EResult:
    """The result of checking a single frame.

    ``status`` is the integrity status of the frame.

    ``sequence_counter`` is the value of the sequence counter as
    extracted according to the E2E profile, or ``None`` if it could
    not be determined (e.g., because the frame is too short).

    ``last_sequence_counter`` is the sequence counter baseline used
    for the comparison (i.e., the counter of the most recently
    accepted frame), or ``None`` if no baseline exists yet.
    """

    status: E2EStatus
    sequence_counter: Optional[int] = None
    last_sequence_counter: Optional[int] = None


class E2EProtection:
    """Stateful per-message E2E protection checker.

    An instance keeps the sequence counter baseline of exactly one
    message, i.e., every E2E protected message of a database gets its
    own checker object. Frames of the same message must be fed in
    arrival order via :meth:`check`.

    Design decisions (see the module documentation of the project for
    the rationale):

    * The sequence counter baseline is only advanced by frames whose
      CRC is correct. Frames with a bad CRC leave the baseline
      untouched, so a damaged counter cannot poison the reference
      used for the following frames.

    * After a sequence jump the new counter is accepted immediately:
      the first frame exhibiting the new counter value is reported as
      ``SEQUENCE_JUMP`` and every subsequent frame is compared
      against the new baseline without any additional grace period.

    * Status precedence: a frame that is too short to contain the
      protected data is always reported as ``FRAME_TOO_SHORT``.
      Otherwise a bad CRC dominates every sequence counter finding,
      i.e., a CRC-bad frame is never additionally reported as
      repeated or as a jump.
    """

    def __init__(self, message: 'Message') -> None:
        if message.autosar is None or message.autosar.e2e is None:
            raise ValueError(
                f'Message "{message.name}" is not E2E protected')

        self._message = message
        category = message.autosar.e2e.category
        if category is None:
            raise ValueError(
                f'Message "{message.name}" does not specify an E2E profile')

        self._profile = category.lower()
        if self._profile not in _SEQ_COUNTER_WIDTHS:
            raise ValueError(
                f'Unsupported E2E profile "{category}" of message '
                f'"{message.name}"')

        self._seq_counter_width = _SEQ_COUNTER_WIDTHS[self._profile]
        self._seq_counter_mask = (1 << self._seq_counter_width) - 1
        self._last_sequence_counter: Optional[int] = None

    @property
    def profile(self) -> str:
        """The normalized name of the E2E profile (``'profile2'`` or
        ``'profile5'``)."""

        return self._profile

    @property
    def last_sequence_counter(self) -> Optional[int]:
        """The sequence counter baseline, i.e., the counter of the most
        recently accepted frame, or ``None`` if none has been accepted
        yet."""

        return self._last_sequence_counter

    def reset(self) -> None:
        """Forget the sequence counter baseline.

        After calling this method the next frame is treated like the
        first frame of a fresh log.
        """

        self._last_sequence_counter = None

    def _extract_sequence_counter(self, data: bytes) -> int:
        if self._profile == 'profile2':
            # Profile 2: the sequence counter is the low nibble of the
            # second byte.
            return data[1] & 0x0f

        # Profile 5: the sequence counter is the whole third byte.
        return data[2]

    def _check_crc(self, data: bytes) -> bool:
        if self._profile == 'profile2':
            result = check_profile2_crc(data, self._message)
        else:
            result = check_profile5_crc(data, self._message)

        # The message is known to be E2E protected with the profile in
        # question, so the stateless helpers must return a boolean.
        return bool(result)

    def check(self, data: bytes) -> E2EResult:
        """Check the integrity of a single frame.

        The frame data must be passed exactly as it appears on the
        bus. For SecOC secured frames this includes the security
        trailer: only the protected prefix given by the E2E
        properties enters the CRC and the frame-length check, and the
        trailer is simply ignored.
        """

        e2e = self._message.autosar.e2e
        assert e2e is not None

        # The protected payload length includes the CRC and the
        # sequence counter themselves. A SecOC trailer beyond it is
        # neither part of the CRC nor of the frame length.
        required_length = max(_MIN_FRAME_LENGTHS[self._profile],
                              e2e.payload_length)

        if len(data) < required_length:
            return E2EResult(E2EStatus.FRAME_TOO_SHORT)

        sequence_counter = self._extract_sequence_counter(data)

        if not self._check_crc(data):
            # The CRC is bad: report it and keep the sequence counter
            # baseline untouched. The counter of a CRC-bad frame cannot
            # be trusted, and advancing the baseline with it would cause
            # spurious jump reports for the following valid frames.
            return E2EResult(E2EStatus.CRC_ERROR,
                             sequence_counter,
                             self._last_sequence_counter)

        if self._last_sequence_counter is None:
            # The first frame has nothing to be compared against. Its
            # sequence counter is accepted unconditionally.
            self._last_sequence_counter = sequence_counter
            return E2EResult(E2EStatus.OK,
                             sequence_counter,
                             None)

        delta = (sequence_counter - self._last_sequence_counter) \
            & self._seq_counter_mask

        if delta == 0:
            # The counter did not advance: a repeated frame. The
            # baseline does not change (it already has this value).
            return E2EResult(E2EStatus.REPEATED,
                             sequence_counter,
                             self._last_sequence_counter)

        if delta <= _MAX_IN_ORDER_DELTA:
            self._last_sequence_counter = sequence_counter
            return E2EResult(E2EStatus.OK,
                             sequence_counter,
                             self._last_sequence_counter)

        # The counter advanced by more than 3. The new value is
        # accepted as the baseline immediately, i.e., subsequent frames
        # are judged against it without a grace period.
        previous = self._last_sequence_counter
        self._last_sequence_counter = sequence_counter
        return E2EResult(E2EStatus.SEQUENCE_JUMP,
                         sequence_counter,
                         previous)
