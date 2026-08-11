import socket
from enum import Enum

BUFFER_SIZE = 4096
LINE_TERMINATOR = b"\r\n"
HEAD_TERMINATOR = b"\r\n\r\n"


class BufferedSocket:
    def __init__(self, transport: socket.socket) -> None:
        self.transport = transport
        self.buffer = bytearray()

    def read(self, size: int) -> bytes:
        if not self.buffer:
            return self.transport.recv(size)
        count = min(size, len(self.buffer))
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def read_exactly(self, size: int) -> tuple[bytes, bool]:
        data = bytearray()
        while len(data) < size:
            chunk = self.read(size - len(data))
            if not chunk:
                return bytes(data), False
            data.extend(chunk)
        return bytes(data), True

    def read_until(self, marker: bytes) -> tuple[bytes, bool]:
        while (boundary := self.buffer.find(marker)) < 0:
            chunk = self.transport.recv(BUFFER_SIZE)
            if not chunk:
                data = bytes(self.buffer)
                self.buffer.clear()
                return data, False
            self.buffer.extend(chunk)
        end = boundary + len(marker)
        data = bytes(self.buffer[:end])
        del self.buffer[:end]
        return data, True


class BodyKind(Enum):
    CHUNKED = "chunked"
    OPAQUE = "opaque"


def request_body_framing(request_head: bytes) -> int | BodyKind:
    if _has_ambiguous_framing_field(request_head):
        return BodyKind.OPAQUE
    transfer_encoding = _header_values(request_head, b"transfer-encoding")
    content_length = _header_values(request_head, b"content-length")
    if transfer_encoding:
        if content_length:
            return BodyKind.OPAQUE
        if _final_transfer_coding(transfer_encoding) == b"chunked":
            return BodyKind.CHUNKED
        return BodyKind.OPAQUE
    if content_length:
        length = _content_length(content_length)
        if length is None:
            return BodyKind.OPAQUE
        return length
    return 0


def response_body_framing(
    response_head: bytes,
    request_method: bytes,
    status: int,
) -> int | BodyKind:
    if request_method == b"HEAD" or status in (204, 304):
        return 0
    if _has_ambiguous_framing_field(response_head):
        return BodyKind.OPAQUE
    transfer_encoding = _header_values(response_head, b"transfer-encoding")
    if transfer_encoding:
        if _final_transfer_coding(transfer_encoding) == b"chunked":
            return BodyKind.CHUNKED
        return BodyKind.OPAQUE
    content_length = _header_values(response_head, b"content-length")
    if content_length:
        length = _content_length(content_length)
        if length is not None:
            return length
    return BodyKind.OPAQUE


def has_expectation(request_head: bytes) -> bool:
    return bool(_header_values(request_head, b"expect"))


def _relay_exactly(
    source: BufferedSocket,
    destination: socket.socket,
    length: int,
) -> bool:
    remaining = length
    while remaining:
        chunk = source.read(min(BUFFER_SIZE, remaining))
        if not chunk:
            return False
        destination.sendall(chunk)
        remaining -= len(chunk)
    return True


def relay_body(
    source: BufferedSocket,
    destination: socket.socket,
    framing: int | BodyKind,
) -> bool:
    if isinstance(framing, int):
        return _relay_exactly(source, destination, framing)
    if framing is BodyKind.CHUNKED:
        return _relay_chunked(source, destination)
    raise ValueError("an opaque body has no known boundary")


def _relay_chunked(source: BufferedSocket, destination: socket.socket) -> bool:
    while True:
        size_line, complete = source.read_until(LINE_TERMINATOR)
        if size_line:
            destination.sendall(size_line)
        if not complete:
            return False
        size_token = size_line.removesuffix(LINE_TERMINATOR).split(b";", 1)[0]
        if not size_token or any(
            byte not in b"0123456789abcdefABCDEF" for byte in size_token
        ):
            return False
        size = int(size_token, 16)
        if size == 0:
            while True:
                trailer_line, complete = source.read_until(LINE_TERMINATOR)
                if trailer_line:
                    destination.sendall(trailer_line)
                if not complete:
                    return False
                if trailer_line == LINE_TERMINATOR:
                    return True
        if not _relay_exactly(source, destination, size):
            return False
        ending, complete = source.read_exactly(len(LINE_TERMINATOR))
        if ending:
            destination.sendall(ending)
        if not complete or ending != LINE_TERMINATOR:
            return False


def relay_to_eof(source: BufferedSocket, destination: socket.socket) -> None:
    while chunk := source.read(BUFFER_SIZE):
        destination.sendall(chunk)


def _header_values(message_head: bytes, name: bytes) -> list[bytes]:
    values = []
    for line in message_head.split(LINE_TERMINATOR)[1:]:
        field_name, colon, value = line.partition(b":")
        if colon and field_name.lower() == name:
            values.append(value.strip())
    return values


def _has_ambiguous_framing_field(message_head: bytes) -> bool:
    framing_names = (b"content-length", b"transfer-encoding")
    for line in message_head.split(LINE_TERMINATOR)[1:]:
        field_name, colon, _ = line.partition(b":")
        normalized = field_name.strip().lower()
        if colon and normalized in framing_names and field_name.lower() != normalized:
            return True
    return False


def _final_transfer_coding(values: list[bytes]) -> bytes | None:
    codings = [
        token.split(b";", 1)[0].strip().lower()
        for value in values
        for token in value.split(b",")
    ]
    return codings[-1] if codings and all(codings) else None


def _content_length(values: list[bytes]) -> int | None:
    tokens = [token.strip() for value in values for token in value.split(b",")]
    if not tokens or any(not token.isdigit() for token in tokens):
        return None
    lengths = [int(token) for token in tokens]
    return lengths[0] if all(length == lengths[0] for length in lengths) else None
