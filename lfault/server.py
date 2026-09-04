import logging
import selectors
import socket
import socketserver
from contextlib import suppress
from typing import cast
from urllib.parse import urlsplit

from . import http1

logger = logging.getLogger(__name__)
CONNECT_METHOD = b"CONNECT"
CONNECT_ESTABLISHED_RESPONSE = b"HTTP/1.1 200 Connection Established\r\n\r\n"

Address = tuple[str, int]
RoutedRequest = tuple[bytes, bytes, Address]


def parse_request_route(request_head: bytes) -> tuple[bytes, Address]:
    request_line = request_head.partition(http1.LINE_TERMINATOR)[0]
    parts = request_line.split(b" ")
    if len(parts) != 3 or not all(parts):
        raise ValueError("malformed request line")

    method, target, _ = parts

    if method == CONNECT_METHOD:
        return method, _parse_connect_target(target)
    return method, _parse_http_target(target)


def _parse_connect_target(target: bytes) -> Address:
    # The // prefix makes urlsplit interpret authority-form as an authority.
    parsed = urlsplit(b"//" + target)
    port = parsed.port
    hostname = parsed.hostname
    target_suffix = parsed.path or parsed.query or parsed.fragment

    if not hostname or port is None:
        raise ValueError("CONNECT target is missing a host or port")
    if target_suffix or parsed.username is not None:
        raise ValueError("CONNECT target contains non-authority components")
    return hostname.decode("ascii"), port


def _parse_http_target(target: bytes) -> Address:
    parsed = urlsplit(target)
    port = parsed.port
    hostname = parsed.hostname

    if parsed.scheme.lower() != b"http" or not hostname:
        raise ValueError("request target is not an absolute HTTP URL")
    return hostname.decode("ascii"), 80 if port is None else port


class ProxyRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = http1.BufferedSocket(self.request)
        context = f"client {self.client_address!r}"
        try:
            request = self._read_request(client, context)
            if request is None:
                return

            request_head, method, destination = request
            context = f"{context}, upstream {destination!r}"
            logger.info("%r request from %s", method, context)

            if method == CONNECT_METHOD:
                self._open_tunnel(client, destination)
            else:
                self._forward_exchange(client, request_head, method, destination)
        except OSError as error:
            logger.warning("I/O failure while handling %s: %s", context, error)

    @staticmethod
    def _read_request(
        client: http1.BufferedSocket, context: str
    ) -> RoutedRequest | None:
        request_head, complete = client.read_until(http1.HEAD_TERMINATOR)
        if not request_head:
            return None
        if not complete:
            logger.warning("%s closed before request head completed", context)
            return None

        try:
            method, destination = parse_request_route(request_head)
        except (UnicodeDecodeError, ValueError):
            logger.warning("could not route request from %s", context)
            return None
        return request_head, method, destination

    def _open_tunnel(
        self, client: http1.BufferedSocket, destination: Address
    ) -> None:
        with socket.create_connection(destination) as upstream_socket:
            self.request.sendall(CONNECT_ESTABLISHED_RESPONSE)

            upstream = http1.BufferedSocket(upstream_socket)
            self._relay_bidirectionally(client, upstream)

    def _forward_exchange(
        self, client: http1.BufferedSocket, request_head: bytes,
        method: bytes, destination: Address
    ) -> None:
        endpoints = self.client_address, destination
        framing = http1.request_body_framing(request_head)
        if framing is http1.BodyKind.OPAQUE:
            logger.warning("%r -> %r: ambiguous request body", *endpoints)
            return
        if http1.has_expectation(request_head):
            logger.warning("%r -> %r: unsupported Expect", *endpoints)
            return

        with socket.create_connection(destination) as upstream_socket:
            upstream = http1.BufferedSocket(upstream_socket)
            upstream_socket.sendall(request_head)

            if not http1.relay_body(client, upstream_socket, framing):
                logger.warning("%r -> %r: incomplete/malformed body", *endpoints)
                return

            self._relay_responses(client, upstream, method, destination)

    def _relay_bidirectionally(
        self, client: http1.BufferedSocket, upstream: http1.BufferedSocket
    ) -> None:
        streams = (
            (client, upstream.transport),
            (upstream, client.transport),
        )
        with selectors.DefaultSelector() as selector:
            for source, destination in streams:
                # Selectors cannot see bytes already removed into a Python buffer.
                if buffered := source.drain_buffer():
                    destination.sendall(buffered)
                selector.register(source.transport, selectors.EVENT_READ, destination)

            while selector.get_map():
                for key, _ in selector.select():
                    self._relay_ready_stream(selector, key)

    @staticmethod
    def _relay_ready_stream(
        selector: selectors.BaseSelector, key: selectors.SelectorKey
    ) -> None:
        source = cast(socket.socket, key.fileobj)
        destination = key.data
        chunk = source.recv(http1.BUFFER_SIZE)
        if chunk:
            destination.sendall(chunk)
            return

        selector.unregister(source)
        # Propagate this EOF while allowing reverse traffic to continue.
        with suppress(OSError):
            destination.shutdown(socket.SHUT_WR)

    def _relay_responses(
        self, client: http1.BufferedSocket, upstream: http1.BufferedSocket,
        method: bytes, destination: Address
    ) -> None:
        endpoints = self.client_address, destination
        while True:
            response_head, complete = upstream.read_until(http1.HEAD_TERMINATOR)
            if response_head:
                client.transport.sendall(response_head)
            if not complete:
                logger.warning("%r <- %r: incomplete response head", *endpoints)
                return

            status = http1.response_status(response_head)
            if status is not None and 100 <= status < 200 and status != 101:
                continue

            self._relay_final_response(
                client, upstream, response_head, method, destination, status
            )
            return

    def _relay_final_response(
        self, client: http1.BufferedSocket, upstream: http1.BufferedSocket,
        response_head: bytes, method: bytes, destination: Address,
        status: int | None
    ) -> None:
        endpoints = self.client_address, destination
        if status is None:
            logger.warning("%r <- %r: bad status; relaying to EOF", *endpoints)
            http1.relay_to_eof(upstream, client.transport)
            return

        if status == 101:
            self._relay_bidirectionally(client, upstream)
            return

        framing = http1.final_response_body_framing(response_head, method, status)
        if framing is http1.BodyKind.OPAQUE:
            http1.relay_to_eof(upstream, client.transport)
        elif not http1.relay_body(upstream, client.transport, framing):
            logger.warning("%r <- %r: incomplete/malformed body", *endpoints)


class ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
