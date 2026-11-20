# Optional quality-aware result attached to the existing decode chain.
#
# The regular ``Message.decode`` / ``Database.decode_message`` return
# values stay byte-for-byte unchanged unless the new keyword-only
# ``with_quality`` flag is enabled.

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from .types import SignalKey, SignalQuality, Source


@dataclass(frozen=True)
class SignalStatus:
    """Per-signal decode status returned when ``with_quality=True``.

    ``value`` is the exact entry the plain decode dictionary would
    contain for usable signals, and ``None`` for unavailable ones. An
    invalid raw value is reported as :data:`SignalQuality.INVALID` with
    its *raw* value in ``raw_value`` -- it is never scaled or turned
    into an enumerated trusted reading.
    """

    key: SignalKey
    quality: SignalQuality
    value: object
    raw_value: Optional[Union[int, float]]
    error: Optional[str]

    @property
    def usable(self) -> bool:
        return self.quality in (SignalQuality.FRESH, SignalQuality.HELD)


@dataclass(frozen=True)
class QualityDecodeResult:
    """The quality-aware counterpart of a plain decode dictionary.

    Supports both mapping-style access (``result[name]`` returns the
    decoded value, matching old behavior for usable signals) and the
    explicit :attr:`statuses` view.
    """

    values: dict[str, object]
    statuses: dict[str, SignalStatus]
    frame_error: Optional[str]
    source: Optional[Source]

    def __getitem__(self, signal_name: str) -> object:
        return self.values[signal_name]

    def get(self, signal_name: str, default=None) -> object:
        return self.values.get(signal_name, default)

    def __contains__(self, signal_name: str) -> bool:
        return signal_name in self.values

    def __iter__(self):
        return iter(self.values)

    def items(self):
        return self.values.items()

    def as_dict(self) -> dict[str, object]:
        """Plain decode-style dictionary (unavailable signals omitted)."""
        return dict(self.values)
