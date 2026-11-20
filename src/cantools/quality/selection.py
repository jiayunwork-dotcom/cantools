# Helpers for building fully-disambiguated signal selections.

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..database.can.database import Database
from ..database.errors import AmbiguousFrameIdError
from .engine import Selection


@dataclass(frozen=True)
class SignalSelector:
    """User-facing signal selection.

    The same frame id may exist on two channels / standard and extended;
    all four coordinates must therefore be given (or resolved uniquely)
    before a selection is accepted.
    """

    channel: str
    message_name: str
    signal_name: str
    is_extended_frame: Optional[bool] = None

    def resolve(self, database: Database) -> Selection:
        try:
            message = database.get_message_by_name(self.message_name)
        except KeyError:
            raise ValueError(
                f"database has no message named {self.message_name!r}"
            ) from None

        extended = message.is_extended_frame
        if (
            self.is_extended_frame is not None
            and self.is_extended_frame != extended
        ):
            raise ValueError(
                f"message {self.message_name!r} is "
                f"{'extended' if extended else 'standard'} but the selector "
                f"requested {self.is_extended_frame}"
            )

        # Resolving the name is not enough: the frame identity used for
        # lookups must be unambiguous too, otherwise the per-frame lookup
        # in the engine would silently disagree with this selection.
        try:
            database.get_message_by_frame_id(
                message.frame_id,
                is_extended_frame=extended)
        except AmbiguousFrameIdError as e:
            raise ValueError(
                f"message {self.message_name!r} cannot be selected: {e}"
            ) from None
        except KeyError:
            raise ValueError(
                f"message {self.message_name!r} has frame id "
                f"0x{message.frame_id:X} "
                f"({'extended' if extended else 'standard'}) which is not "
                "indexed by the database's frame id mask"
            ) from None

        message.get_signal_by_name(self.signal_name)

        return Selection(
            channel=self.channel,
            frame_id=message.frame_id,
            is_extended_frame=extended,
            message_name=message.name,
            signal_name=self.signal_name,
        )


def select_signals(
    database: Database, selectors: list[SignalSelector]
) -> list[Selection]:
    """Resolve selectors against a database, rejecting ambiguous or
    duplicated (channel, frame type, message, signal) identities."""
    selections: list[Selection] = []
    seen: set[tuple[str, int, bool, str]] = set()
    for selector in selectors:
        selection = selector.resolve(database)
        dedup = (
            selection.channel,
            selection.frame_id,
            selection.is_extended_frame,
            selection.signal_name,
        )
        if dedup in seen:
            raise ValueError(
                f"duplicate selection after resolution: "
                f"{selection.key}"
            )
        seen.add(dedup)
        selections.append(selection)
    return selections
