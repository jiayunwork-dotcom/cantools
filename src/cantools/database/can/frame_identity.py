"""CAN frame identity: arbitration id bits *and* frame type.

A frame is identified by two independent pieces of information:

* the arbitration id (11 bits for a standard frame, 29 bits for an
  extended frame), and
* the standard/extended frame type flag.

DBC files encode the frame type in bit 31 of the ``BO_`` message id
(``0x80000000``). That flag is a property of the *file format*, not part
of the arbitration id, and it is stripped when a message is loaded.
Logs such as ``candump`` encode the frame type in the textual width of
the id (three hex digits for standard frames, eight for extended ones).
Those two representations never share a number space: the identity used
internally is the pair ``(arbitration_id, is_extended_frame)``.

The helpers below are deliberately tiny so that every layer (database
lookup, ``decode``, ``plot`` and the quality engine) resolves frame
identities the same way.
"""

from __future__ import annotations

from typing import Optional

#: Largest legal standard (11 bit) arbitration id.
STANDARD_FRAME_ID_MAX = 0x7FF

#: Largest legal extended (29 bit) arbitration id.
EXTENDED_FRAME_ID_MAX = 0x1FFFFFFF

#: Bit used by the DBC *file format* to mark a ``BO_`` entry as extended.
#: It is stripped by the DBC loader and is never part of an arbitration
#: id, so it must not be carried around as part of one.
DBC_EXTENDED_FLAG = 0x80000000

#: Internal identity of a frame: (masked arbitration id, extended flag).
FrameIdentity = tuple[int, bool]


def normalize_frame_id_mask(frame_id_mask: Optional[int]) -> int:
    """Return the effective arbitration-id mask.

    ``None`` selects the default which keeps every arbitration id bit
    (that is what "no mask" means now that the frame type is tracked
    separately). Bits above bit 28 are not part of any CAN arbitration
    id and are ignored, which keeps callers that used to pass
    ``0xffffffff`` working.
    """

    if frame_id_mask is None:
        return EXTENDED_FRAME_ID_MAX

    if not isinstance(frame_id_mask, int):
        raise TypeError(
            f"frame_id_mask must be an int, got {type(frame_id_mask).__name__}")

    if frame_id_mask < 0:
        raise ValueError(
            f"frame_id_mask must not be negative, got 0x{frame_id_mask:x}")

    return frame_id_mask & EXTENDED_FRAME_ID_MAX


def mask_frame_id(frame_id: int, frame_id_mask: int) -> int:
    """Keep only the arbitration id bits selected by ``frame_id_mask``."""

    return frame_id & frame_id_mask & EXTENDED_FRAME_ID_MAX


def resolve_frame_identity(
    frame_id: int,
    force_extended_id: bool = False,
    *,
    is_extended_frame: Optional[bool] = None,
) -> tuple[int, bool]:
    """Resolve a caller supplied id into an arbitration id and frame type.

    ``is_extended_frame`` is used by log readers, where the frame type is
    stated explicitly and is authoritative even for extended ids that
    happen to be numerically small (``0x123`` is a legal extended id).
    In that case ``force_extended_id`` must not contradict it.

    Without an explicit type, the historical numeric inference is kept:

    * a caller supplied DBC-style flag (bit 31) is treated as
      "extended" and stripped from the arbitration id -- bit 31 is a
      file-format marker, never a real id bit;
    * otherwise ids above ``0x7FF`` are extended and the rest are
      standard;
    * ``force_extended_id=True`` forces the extended interpretation.
    """

    if not isinstance(frame_id, int):
        raise TypeError(
            f"frame id must be an int, got {type(frame_id).__name__}")

    if is_extended_frame is not None:
        if force_extended_id and not is_extended_frame:
            raise ValueError(
                "is_extended_frame=False conflicts with "
                "force_extended_id=True")
        return frame_id, bool(is_extended_frame)

    if frame_id >= 0 and frame_id & DBC_EXTENDED_FLAG:
        return frame_id & EXTENDED_FRAME_ID_MAX, True

    return frame_id, bool(force_extended_id or frame_id > STANDARD_FRAME_ID_MAX)


def frame_id_is_legal(frame_id: int, is_extended_frame: bool) -> bool:
    """Whether ``frame_id`` is a legal arbitration id of the given type."""

    if not isinstance(frame_id, int) or frame_id < 0:
        return False

    limit = (EXTENDED_FRAME_ID_MAX
             if is_extended_frame
             else STANDARD_FRAME_ID_MAX)
    return frame_id <= limit
