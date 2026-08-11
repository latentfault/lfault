import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

BUFFER_SIZE = 4096
MAX_HEAD_SIZE = 64 * 1024
MAX_LINE_SIZE = 8 * 1024
LINE_TERMINATOR = b"\r\n"
HEAD_TERMINATOR = b"\r\n\r\n"


class HTTP1LimitError(ValueError):
    pass


class BufferedSocket:
    def __init__(self, transport: socket.socket) -> None:
        self.transport = transport
        self.buffer = bytearray()
        self.eof = False
        self.before_receive: Callable[[], None] | None = None

    def read(self, size: int) -> bytes:
        if self.buffer:
            count = min(size, len(self.buffer))
            data = bytes(self.buffer[:count])
            del self.buffer[:count]
            return data
        if self.eof:
            return b""
        return self._receive(size)

    def read_exactly(self, size: int) -> tuple[bytes, bool]:
        data = bytearray()
        while len(data) < size:
            chunk = self.read(size - len(data))
            if not chunk:
                return bytes(data), False
            data.extend(chunk)
        return bytes(data), True

    def read_until(self, marker: bytes, limit: int) -> tuple[bytes, bool]:
        while (boundary := self.buffer.find(marker)) < 0:
            if len(self.buffer) > limit:
                raise HTTP1LimitError(f"HTTP/1 section exceeds {limit} bytes")
            if self.eof:
                data = bytes(self.buffer)
                self.buffer.clear()
                return data, False
            chunk = self._receive(BUFFER_SIZE)
            if not chunk:
                data = bytes(self.buffer)
                self.buffer.clear()
                return data, False
            self.buffer.extend(chunk)
        end = boundary + len(marker)
        if end > limit:
            raise HTTP1LimitError(f"HTTP/1 section exceeds {limit} bytes")
        data = bytes(self.buffer[:end])
        del self.buffer[:end]
        return data, True

    def flush_to(self, destination: socket.socket) -> None:
        if self.buffer:
            destination.sendall(bytes(self.buffer))
            self.buffer.clear()

    def _receive(self, size: int) -> bytes:
        if self.before_receive is not None:
            self.before_receive()
        data = self.transport.recv(size)
        if not data:
            self.eof = True
        return data


class BodyKind(Enum):
    LENGTH = "length"
    CHUNKED = "chunked"
    OPAQUE = "opaque"


@dataclass(frozen=True, slots=True)
class BodyFraming:
    kind: BodyKind
    length: int = 0


def request_body_framing(request_head: bytes) -> BodyFraming:
    if _has_ambiguous_framing_field(request_head):
        return BodyFraming(BodyKind.OPAQUE)
    transfer_encoding = _header_values(request_head, b"transfer-encoding")
    content_length = _header_values(request_head, b"content-length")
    if transfer_encoding:
        if content_length:
            return BodyFraming(BodyKind.OPAQUE)
        if _final_transfer_coding(transfer_encoding) == b"chunked":
            return BodyFraming(BodyKind.CHUNKED)
        return BodyFraming(BodyKind.OPAQUE)
    if content_length:
        length = _content_length(content_length)
        if length is None:
            return BodyFraming(BodyKind.OPAQUE)
        return BodyFraming(BodyKind.LENGTH, length)
    return BodyFraming(BodyKind.LENGTH, 0)


def response_body_framing(
    response_head: bytes,
    request_method: bytes,
    status: int,
) -> BodyFraming:
    if request_method == b"HEAD" or 100 <= status < 200 or status in (204, 304):
        return BodyFraming(BodyKind.LENGTH, 0)
    if _has_ambiguous_framing_field(response_head):
        return BodyFraming(BodyKind.OPAQUE)
    transfer_encoding = _header_values(response_head, b"transfer-encoding")
    if transfer_encoding:
        if _final_transfer_coding(transfer_encoding) == b"chunked":
            return BodyFraming(BodyKind.CHUNKED)
        return BodyFraming(BodyKind.OPAQUE)
    content_length = _header_values(response_head, b"content-length")
    if content_length:
        length = _content_length(content_length)
        if length is not None:
            return BodyFraming(BodyKind.LENGTH, length)
    return BodyFraming(BodyKind.OPAQUE)


def response_status(response_head: bytes) -> int | None:
    status_line = response_head.partition(LINE_TERMINATOR)[0]
    parts = status_line.split(b" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def expects_continue(request_head: bytes) -> bool:
    return any(
        token.strip().lower() == b"100-continue"
        for value in _header_values(request_head, b"expect")
        for token in value.split(b",")
    )


def connection_is_persistent(
    message_head: bytes,
    version: bytes,
    *,
    proxy: bool = False,
) -> bool:
    names = (b"connection", b"proxy-connection") if proxy else (b"connection",)
    tokens = {
        token.strip().lower()
        for name in names
        for value in _header_values(message_head, name)
        for token in value.split(b",")
    }
    if b"close" in tokens:
        return False
    return version == b"HTTP/1.1" or b"keep-alive" in tokens


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
    framing: BodyFraming,
) -> bool:
    if framing.kind is BodyKind.LENGTH:
        return _relay_exactly(source, destination, framing.length)
    if framing.kind is BodyKind.CHUNKED:
        return _relay_chunked(source, destination)
    raise ValueError("an opaque body has no known boundary")


def _relay_chunked(source: BufferedSocket, destination: socket.socket) -> bool:
    while True:
        size_line, complete = source.read_until(LINE_TERMINATOR, MAX_LINE_SIZE)
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
                trailer_line, complete = source.read_until(
                    LINE_TERMINATOR,
                    MAX_LINE_SIZE,
                )
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
    source.flush_to(destination)
    while chunk := source.read(BUFFER_SIZE):
        destination.sendall(chunk)


def _header_values(message_head: bytes, name: bytes) -> list[bytes]:
    head = message_head.removesuffix(HEAD_TERMINATOR)
    values = []
    for line in head.split(LINE_TERMINATOR)[1:]:
        field_name, colon, value = line.partition(b":")
        if colon and field_name.lower() == name:
            values.append(value.strip())
    return values


def _has_ambiguous_framing_field(message_head: bytes) -> bool:
    head = message_head.removesuffix(HEAD_TERMINATOR)
    framing_names = (b"content-length", b"transfer-encoding")
    for line in head.split(LINE_TERMINATOR)[1:]:
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
