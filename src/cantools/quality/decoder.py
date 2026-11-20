# Quality-aware frame decoding built on top of the existing codec chain.

from __future__ import annotations

from typing import Callable, Optional, Union

try:
    import bitstruct.c
except ImportError:
    import bitstruct

from ..database.can.message import Message
from ..database.can.signal import Signal
from ..database.errors import DecodeError
from ..database.utils import start_bit
from .types import (
    DecodedFrame,
    SignalKey,
    SignalObservation,
    SignalQuality,
    Source,
)

#: Exceptions the codec backends raise on malformed/truncated payloads.
#: Mirrors the (bitstruct.Error, ValueError) tuple used by
#: cantools.database.utils, because the C and pure-Python backends
#: surface different concrete exceptions.
_UNPACK_ERRORS = (bitstruct.Error, ValueError) \
    if hasattr(bitstruct, "Error") else (ValueError,)

#: Builds the stable identity object for one signal of one message.
KeyFactory = Callable[[str], SignalKey]


def _is_signal_complete(signal: Signal, payload_length: int) -> bool:
    """Whether the full bit field of ``signal`` is present in a payload of
    ``payload_length`` bytes.

    Mirrors the byte-order handling in
    :func:`cantools.database.utils.decode_data`.
    """
    if signal.byte_order == "little_endian":
        sequential_start_bit = signal.start
    else:
        sequential_start_bit = (8 * (signal.start // 8)) + (
            7 - (signal.start % 8)
        )

    return sequential_start_bit + signal.length <= payload_length * 8


def _active_mux_ids(message: Message, data: bytes) -> tuple[
    Optional[dict[str, int]], Optional[str]
]:
    """Walk the multiplexer tree reading only selector values.

    Returns ``(mux_values, error_message)``. On success each active
    multiplexer selector name maps to its raw value. If a selector value
    is unknown (or its bit field is missing) an error message is
    returned instead.
    """
    node = message._codecs
    assert node is not None

    mux_values: dict[str, int] = {}
    try:
        unpacked = {
            **node["formats"].big_endian.unpack(data),
            **node["formats"].little_endian.unpack(data[::-1]),
        }
    except _UNPACK_ERRORS as e:
        return None, f"unpacking failed: {e}"

    current = node
    while current["multiplexers"]:
        for mux_name in current["multiplexers"]:
            if mux_name not in unpacked:
                return None, (
                    f"multiplexer selector {mux_name!r} is not fully "
                    "present in the payload"
                )
            mux_raw = int(unpacked[mux_name])
            mux_values[mux_name] = mux_raw
            child = current["multiplexers"][mux_name].get(mux_raw)
            if child is None:
                return None, (
                    f"expected multiplexer id "
                    f"{sorted(current['multiplexers'][mux_name].keys())}, "
                    f"but got {mux_raw}"
                )
            current = child
            try:
                child_unpacked = {
                    **current["formats"].big_endian.unpack(data),
                    **current["formats"].little_endian.unpack(data[::-1]),
                }
            except _UNPACK_ERRORS as e:
                return None, f"unpacking failed: {e}"
            unpacked.update(child_unpacked)

    return mux_values, None


def _branch_active(signal: Signal,
                   mux_values: Optional[dict[str, int]]) -> bool:
    """Whether ``signal``'s multiplexer branch (if any) is active."""
    if signal.multiplexer_signal is None:
        return True

    assert mux_values is not None
    selector = mux_values.get(signal.multiplexer_signal)
    if selector is None:
        return False
    return (signal.multiplexer_ids is not None
            and selector in signal.multiplexer_ids)


def decode_frame_with_quality(
    message: Message,
    data: bytes,
    source: Source,
    key_factory: KeyFactory,
    decode_choices: bool = True,
    scaling: bool = True,
    selected_signals: Optional[set[str]] = None,
) -> DecodedFrame:
    """Decode one data frame, attaching a per-signal quality.

    The regular :meth:`Message.decode` behavior is reused for every
    signal that can be decoded; signals that cannot be decoded get an
    explicit quality instead of aborting the whole frame.

    :param key_factory: Returns the :class:`SignalKey` for a given
        signal name of ``message``.
    :param selected_signals: Restrict the output to these signal names.
        ``None`` returns every signal of the message.
    """
    if message.is_container:
        return _decode_container(message, data, source, key_factory,
                                 decode_choices, scaling, selected_signals)

    signals = [
        sig for sig in message.signals
        if selected_signals is None or sig.name in selected_signals
    ]

    observations: dict[str, SignalObservation] = {}
    payload_length = len(data)

    # First, establish whether the frame as a whole decodes and which
    # multiplexer branches are active.
    frame_error: Optional[str] = None
    mux_values: Optional[dict[str, int]] = None
    decoded: dict = {}

    if message.is_multiplexed():
        mux_values, mux_error = _active_mux_ids(message, data)
        if mux_error is not None:
            frame_error = mux_error
    if frame_error is None:
        try:
            decoded = message.decode_simple(
                data,
                decode_choices=decode_choices,
                scaling=scaling,
                allow_truncated=True,
                allow_excess=True,
            )
        except DecodeError as e:
            frame_error = str(e)
        except ValueError as e:
            frame_error = str(e)

    for signal in signals:
        observations[signal.name] = _decode_one_signal(
            signal=signal,
            payload_length=payload_length,
            source=source,
            key_factory=key_factory,
            decode_choices=decode_choices,
            decoded=decoded,
            mux_values=mux_values,
            frame_error=frame_error,
            data=data,
        )

    return DecodedFrame(
        channel=source.channel,
        frame_id=source.frame_id,
        is_extended_frame=source.is_extended_frame,
        message_name=message.name,
        timestamp_us=source.timestamp_us,
        source=source,
        observations=observations,
        error=frame_error,
    )


def _decode_one_signal(
    *,
    signal: Signal,
    payload_length: int,
    source: Source,
    key_factory: KeyFactory,
    decode_choices: bool,
    decoded: dict,
    mux_values: Optional[dict[str, int]],
    frame_error: Optional[str],
    data: bytes,
) -> SignalObservation:

    def make(quality: SignalQuality,
             value=None,
             raw_value: Optional[Union[int, float]] = None,
             error: Optional[str] = None) -> SignalObservation:
        return SignalObservation(
            key=key_factory(signal.name),
            timestamp_us=source.timestamp_us,
            source=source,
            quality=quality,
            value=value,
            raw_value=raw_value,
            error=error,
        )

    # 1) An undecodable frame (bad payload / unknown mux value) blocks
    #    every signal of the message. A truncated bit field still gets
    #    its own, more precise quality.
    if frame_error is not None:
        if not _is_signal_complete(signal, payload_length):
            return make(
                SignalQuality.TRUNCATED,
                error=(
                    f"signal bit field ({signal.length} bits) is not fully "
                    f"contained in the {payload_length}-byte payload"
                ),
            )
        return make(SignalQuality.DECODE_FAILED, error=frame_error)

    # 2) Incomplete bit field in a short payload: the signal is missing
    #    and blocks any older good value.
    if not _is_signal_complete(signal, payload_length):
        return make(
            SignalQuality.TRUNCATED,
            error=(
                f"signal bit field ({signal.length} bits) is not fully "
                f"contained in the {payload_length}-byte payload"
            ),
        )

    # 3) Multiplexer branch not active.
    if signal.multiplexer_signal is not None and not _branch_active(
        signal, mux_values
    ):
        return make(
            SignalQuality.BRANCH_INACTIVE,
            error=(
                f"multiplexer branch "
                f"{signal.multiplexer_signal}={signal.multiplexer_ids} "
                "is not active"
            ),
        )

    # 4) The regular chain produced a value. Read the raw value to test
    #    raw_invalid in its original (unscaled) meaning.
    if signal.name not in decoded:
        return make(
            SignalQuality.DECODE_FAILED,
            error="signal disappeared during decoding",
        )

    raw_value = _read_raw(signal, data)
    if (
        signal.raw_invalid is not None
        and raw_value is not None
        and raw_value == signal.raw_invalid
    ):
        return make(
            SignalQuality.INVALID,
            raw_value=raw_value,
            error=(
                f"raw value {raw_value} equals the signal's raw_invalid "
                f"value {signal.raw_invalid}"
            ),
        )

    return make(SignalQuality.FRESH, value=decoded[signal.name],
                raw_value=raw_value)


def _read_raw(signal: Signal, data: bytes) -> Optional[Union[int, float]]:
    """Read the raw (unscaled, un-enumerated) value of a complete signal."""
    number_of_bytes = max(
        len(data), (start_bit(signal) + signal.length + 7) // 8
    )
    padded = data.ljust(number_of_bytes, b"\xff")
    try:
        formats = _raw_formats(signal, number_of_bytes)
        unpacked = {
            **formats.big_endian.unpack(padded),
            **formats.little_endian.unpack(padded[::-1]),
        }
        return unpacked.get(signal.name)
    except _UNPACK_ERRORS:
        return None


def _raw_formats(signal: Signal, number_of_bytes: int):
    from ..database.utils import create_encode_decode_formats

    cache = getattr(signal, "_quality_raw_formats", None)
    if cache is not None and cache[0] == number_of_bytes:
        return cache[1]
    formats = create_encode_decode_formats([signal], number_of_bytes)
    try:
        signal._quality_raw_formats = (  # type: ignore[attr-defined]
            number_of_bytes, formats
        )
    except AttributeError:
        pass
    return formats


def _decode_container(message, data, source, key_factory, decode_choices,
                      scaling, selected_signals) -> DecodedFrame:
    """Container frames: decode each contained message and flatten the
    selected signals."""
    observations: dict[str, SignalObservation] = {}
    frame_error: Optional[str] = None
    try:
        unpacked = message.unpack_container(data, allow_truncated=True)
    except DecodeError as e:
        unpacked = []
        frame_error = str(e)

    for contained_message, contained_data in unpacked:
        if not isinstance(contained_message, Message):
            continue
        names = {
            sig.name for sig in contained_message.signals
            if selected_signals is None or sig.name in selected_signals
        }
        if not names:
            continue
        sub = decode_frame_with_quality(
            contained_message,
            bytes(contained_data),
            source,
            key_factory,
            decode_choices,
            scaling,
            names,
        )
        if sub.error is not None and frame_error is None:
            frame_error = sub.error
        observations.update(sub.observations)

    return DecodedFrame(
        channel=source.channel,
        frame_id=source.frame_id,
        is_extended_frame=source.is_extended_frame,
        message_name=message.name,
        timestamp_us=source.timestamp_us,
        source=source,
        observations=observations,
        error=frame_error,
    )
