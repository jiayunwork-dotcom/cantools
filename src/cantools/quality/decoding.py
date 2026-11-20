# Bridge between the plain Python decode API and the quality-aware
# decoder. Used by ``Message.decode(..., with_quality=True)``.

from __future__ import annotations

from ..database.can.message import Message
from .decoder import decode_frame_with_quality
from .result import QualityDecodeResult, SignalStatus
from .types import SignalKey, SignalQuality, Source


def _virtual_source(message: Message, data: bytes) -> Source:
    """Direct decode calls have no log origin; synthesize a neutral
    source that still carries frame identity and payload length."""
    return Source(
        line=0,
        timestamp_us=0,
        channel="",
        frame_id=message.frame_id,
        is_extended_frame=message.is_extended_frame,
    )


def decode_message_quality(
    message: Message,
    data: bytes,
    *,
    decode_choices: bool = True,
    scaling: bool = True,
    decode_containers: bool = False,
) -> QualityDecodeResult:
    """Quality-aware counterpart of :meth:`Message.decode`.

    Usable signals appear in ``values`` exactly as the plain decode
    would return them; unavailable signals are omitted from ``values``
    but always present in ``statuses`` with a precise reason.
    """
    source = _virtual_source(message, data)

    def key_factory(signal_name: str) -> SignalKey:
        return SignalKey(
            channel="",
            frame_id=message.frame_id,
            is_extended_frame=message.is_extended_frame,
            message_name=message.name,
            signal_name=signal_name,
        )

    if message.is_container and not decode_containers:
        from ..database.errors import DecodeError
        raise DecodeError(
            f'Message "{message.name}" is a container message, but '
            "decoding such messages has not been enabled!"
        )

    decoded = decode_frame_with_quality(
        message=message,
        data=data,
        source=source,
        key_factory=key_factory,
        decode_choices=decode_choices,
        scaling=scaling,
        selected_signals=None,
    )

    values: dict[str, object] = {}
    statuses: dict[str, SignalStatus] = {}
    for name, obs in decoded.observations.items():
        usable = obs.quality == SignalQuality.FRESH
        if usable:
            values[name] = obs.value
        statuses[name] = SignalStatus(
            key=obs.key,
            quality=obs.quality,
            value=obs.value if usable else None,
            raw_value=obs.raw_value,
            error=obs.error,
        )

    return QualityDecodeResult(
        values=values,
        statuses=statuses,
        frame_error=decoded.error,
        source=source,
    )
