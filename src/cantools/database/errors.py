from typing import Any, Optional, Union

from ..errors import Error as _Error


class Error(_Error):
    pass


class ParseError(Error):
    pass


class EncodeError(Error):
    pass


class DecodeError(Error):
    pass


class AmbiguousFrameIdError(Error, KeyError):
    """Raised when one masked frame identity matches several messages.

    It deliberately derives from :class:`KeyError` as well, so callers
    that treat a missing frame id as "unknown" keep working; the
    distinction is that this frame id is *ambiguous*, not unknown.
    """

    def __init__(self,
                 frame_key: int,
                 frame_id_mask: int,
                 is_extended_frame: bool,
                 messages: tuple[Any, ...],
                 requested_frame_id: Optional[int] = None) -> None:
        self.frame_key = frame_key
        self.frame_id_mask = frame_id_mask
        self.is_extended_frame = is_extended_frame
        self.messages = messages
        self.requested_frame_id = (
            frame_key if requested_frame_id is None else requested_frame_id)
        super().__init__(frame_key)

    def __str__(self) -> str:
        kind = 'extended' if self.is_extended_frame else 'standard'
        candidates = ', '.join(
            f"'{message.name}' (0x{message.frame_id:x}, "
            f"{'extended' if message.is_extended_frame else 'standard'})"
            for message in self.messages)
        return (
            f"ambiguous {kind} frame id 0x{self.requested_frame_id:x} with "
            f"mask 0x{self.frame_id_mask:x}: it matches {len(self.messages)} "
            f"different message definitions: {candidates}")

    def __reduce__(self) -> Union[str, tuple[Any, ...]]:
        return (type(self),
                (self.frame_key, self.frame_id_mask,
                 self.is_extended_frame, self.messages,
                 self.requested_frame_id))


class UnsupportedDatabaseFormatError(Error):
    """This exception is raised when
    :func:`~cantools.database.load_file()`,
    :func:`~cantools.database.load()` and
    :func:`~cantools.database.load_string()` are unable to parse given
    database file or string.

    """

    def __init__(self,
                 e_arxml: Optional[Exception],
                 e_dbc: Optional[Exception],
                 e_kcd: Optional[Exception],
                 e_sym: Optional[Exception],
                 e_cdd: Optional[Exception]) -> None:
        message_chunks: list[str] = []

        if e_arxml is not None:
            message_chunks.append(f'ARXML: "{e_arxml}"')

        if e_dbc is not None:
            message_chunks.append(f'DBC: "{e_dbc}"')

        if e_kcd is not None:
            message_chunks.append(f'KCD: "{e_kcd}"')

        if e_sym is not None:
            message_chunks.append(f'SYM: "{e_sym}"')

        if e_cdd is not None:
            message_chunks.append(f'CDD: "{e_cdd}"')

        message = ', '.join(message_chunks)

        super().__init__(message)

        self.e_arxml = e_arxml
        self.e_dbc = e_dbc
        self.e_kcd = e_kcd
        self.e_sym = e_sym
        self.e_cdd = e_cdd

    def __reduce__(self) -> Union[str, tuple[Any, ...]]:
        return type(self), (self.e_arxml, self.e_dbc, self.e_kcd, self.e_sym, self.e_cdd), {}
