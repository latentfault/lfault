import logging
import selectors
import socket
import socketserver
from contextlib import suppress
from urllib.parse import urlsplit

from .http1 import (
    BUFFER_SIZE,
    HEAD_TERMINATOR,
    LINE_TERMINATOR,
    BodyKind,
    BufferedSocket,
    final_response_body_framing,
    has_expectation,
    relay_body,
    relay_to_eof,
    request_body_framing,
)

logger = logging.getLogger(__name__)
CONNECT_ESTABLISHED_RESPONSE = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def parse_request_route(request_head: bytes) -> tuple[bytes, tuple[str, int]]:
    request_line = request_head.partition(LINE_TERMINATOR)[0]
    parts = request_line.split(b" ")
    if len(parts) != 3 or not all(parts):
        raise ValueError("malformed request line")

    method, target, _ = parts
    if method == b"CONNECT":
        # The // prefix makes urlsplit interpret authority-form as an authority.
        parsed = urlsplit(b"//" + target)
    else:
        parsed = urlsplit(target)
    port = parsed.port
    hostname = parsed.hostname
    if method == b"CONNECT":
        if (
            not hostname
            or port is None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise ValueError("CONNECT target is not a host and port")
    elif parsed.scheme.lower() != b"http" or not hostname:
        raise ValueError("request target is not an absolute HTTP URL")
    host = hostname.decode("ascii")
    return method, (host, 80 if port is None else port)


class ProxyRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = BufferedSocket(self.request)
        connection_context = f"client {self.client_address!r}"
        try:
            request_head, complete = client.read_until(HEAD_TERMINATOR)
            if not request_head:
                return
            if not complete:
                logger.warning(
                    "%s closed before request head completed",
                    connection_context,
                )
                return
            try:
                method, destination = parse_request_route(request_head)
            except (UnicodeDecodeError, ValueError):
                logger.warning(
                    "could not route request from %s",
                    connection_context,
                )
                return
            connection_context = f"{connection_context}, upstream {destination!r}"
            logger.info(
                "%r request from %r to %r",
                method,
                self.client_address,
                destination,
            )
            if method == b"CONNECT":
                with socket.create_connection(destination) as upstream_socket:
                    self.request.sendall(CONNECT_ESTABLISHED_RESPONSE)
                    self._relay_bidirectionally(
                        client,
                        BufferedSocket(upstream_socket),
                    )
            else:
                self._forward_exchange(client, request_head, method, destination)
        except OSError as error:
            logger.warning(
                "I/O failure while handling %s: %s",
                connection_context,
                error,
            )

    def _forward_exchange(
        self,
        client: BufferedSocket,
        request_head: bytes,
        method: bytes,
        destination: tuple[str, int],
    ) -> None:
        framing = request_body_framing(request_head)
        if framing is BodyKind.OPAQUE:
            logger.warning(
                "rejecting request from %r to %r: body boundary is ambiguous",
                self.client_address,
                destination,
            )
            return
        if has_expectation(request_head):
            logger.warning(
                "rejecting request from %r to %r: Expect is unsupported",
                self.client_address,
                destination,
            )
            return
        with socket.create_connection(destination) as upstream_socket:
            upstream = BufferedSocket(upstream_socket)
            upstream_socket.sendall(request_head)
            if not relay_body(client, upstream_socket, framing):
                logger.warning(
                    "request body from %r to %r was incomplete or malformed",
                    self.client_address,
                    destination,
                )
                return
            self._relay_responses(client, upstream, method, destination)

    @staticmethod
    def _relay_bidirectionally(
        client: BufferedSocket,
        upstream: BufferedSocket,
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
                    destination = key.data
                    chunk = key.fileobj.recv(BUFFER_SIZE)
                    if chunk:
                        destination.sendall(chunk)
                        continue
                    selector.unregister(key.fileobj)
                    # Propagate this EOF while allowing reverse traffic to continue.
                    with suppress(OSError):
                        destination.shutdown(socket.SHUT_WR)

    def _relay_responses(
        self,
        client: BufferedSocket,
        upstream: BufferedSocket,
        method: bytes,
        destination: tuple[str, int],
    ) -> None:
        while True:
            response_head, complete = upstream.read_until(HEAD_TERMINATOR)
            if response_head:
                client.transport.sendall(response_head)
            if not complete:
                logger.warning(
                    "response head from %r for client %r was incomplete",
                    destination,
                    self.client_address,
                )
                return
            parts = response_head.partition(LINE_TERMINATOR)[0].split(b" ", 2)
            status = None
            if len(parts) > 1 and parts[1].isdigit():
                status = int(parts[1])
            if status is not None and 100 <= status < 200 and status != 101:
                continue
            if status is None:
                logger.warning(
                    "could not parse response status from %r for client %r; "
                    "relaying to EOF",
                    destination,
                    self.client_address,
                )
                relay_to_eof(upstream, client.transport)
            elif status == 101:
                self._relay_bidirectionally(client, upstream)
            else:
                framing = final_response_body_framing(response_head, method, status)
                if framing is BodyKind.OPAQUE:
                    relay_to_eof(upstream, client.transport)
                elif not relay_body(upstream, client.transport, framing):
                    logger.warning(
                        "response body from %r for client %r was incomplete "
                        "or malformed",
                        destination,
                        self.client_address,
                    )
            return


class ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
