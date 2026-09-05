"""Buffered input and byte relays; callers own socket lifetimes."""

import selectors
import socket
from contextlib import suppress
from typing import cast

BUFFER_SIZE = 4096


class BufferedSocket:
    """Own unread bytes on a borrowed socket; read through this wrapper."""

    def __init__(self, transport: socket.socket) -> None:
        self.transport = transport
        self._buffer = bytearray()

    def read(self, size: int) -> bytes:
        if not self._buffer:
            return self.transport.recv(size)
        count = min(size, len(self._buffer))
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def read_exactly(self, size: int) -> tuple[bytes, bool]:
        """Return size bytes, or partial data and False if EOF arrives first."""
        data = bytearray()
        while len(data) < size:
            chunk = self.read(size - len(data))
            if not chunk:
                return bytes(data), False
            data.extend(chunk)
        return bytes(data), True

    def read_until(self, marker: bytes) -> tuple[bytes, bool]:
        """Read through marker, or return partial data and False at EOF."""
        while (boundary := self._buffer.find(marker)) < 0:
            chunk = self.transport.recv(BUFFER_SIZE)
            if not chunk:
                data = self.drain_buffer()
                return data, False
            self._buffer.extend(chunk)

        end = boundary + len(marker)
        data = bytes(self._buffer[:end])
        del self._buffer[:end]
        return data, True

    def drain_buffer(self) -> bytes:
        """Consume pending bytes without reading the underlying socket."""
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


def relay_exactly(
    source: BufferedSocket, destination: socket.socket, length: int
) -> bool:
    """Copy length bytes, returning False after partial forwarding at EOF."""
    remaining = length
    while remaining:
        chunk = source.read(min(BUFFER_SIZE, remaining))
        if not chunk:
            return False
        destination.sendall(chunk)
        remaining -= len(chunk)
    return True


def relay_to_eof(source: BufferedSocket, destination: socket.socket) -> None:
    while chunk := source.read(BUFFER_SIZE):
        destination.sendall(chunk)


def relay_bidirectionally(left: BufferedSocket, right: BufferedSocket) -> None:
    """Relay both streams through EOF, leaving socket cleanup to the caller."""
    directions = (
        (left, right.transport),
        (right, left.transport),
    )
    with selectors.DefaultSelector() as selector:
        for source, destination in directions:
            # Flush read-ahead before handing input over to direct socket reads.
            # Selectors cannot see bytes already removed into a Python buffer.
            if buffered := source.drain_buffer():
                destination.sendall(buffered)
            selector.register(source.transport, selectors.EVENT_READ, destination)

        while selector.get_map():
            for key, _ in selector.select():
                _relay_ready_stream(selector, key)


def _relay_ready_stream(
    selector: selectors.BaseSelector, key: selectors.SelectorKey
) -> None:
    source = cast(socket.socket, key.fileobj)
    destination = cast(socket.socket, key.data)
    chunk = source.recv(BUFFER_SIZE)
    if chunk:
        destination.sendall(chunk)
        return

    selector.unregister(source)
    # Propagate this EOF while allowing reverse traffic to continue.
    with suppress(OSError):
        destination.shutdown(socket.SHUT_WR)
