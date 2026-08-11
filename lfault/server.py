import logging
import selectors
import socket
import socketserver
from contextlib import closing
from urllib.parse import urlsplit

from .http1 import (
    BUFFER_SIZE,
    HEAD_TERMINATOR,
    BodyKind,
    BufferedSocket,
    has_expectation,
    relay_body,
    relay_to_eof,
    request_body_framing,
    response_body_framing,
)

logger = logging.getLogger(__name__)
CONNECTION_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def request_route(request_head: bytes) -> tuple[bytes, tuple[str, int]]:
    request_line = request_head.partition(b"\r\n")[0]
    parts = request_line.split(b" ")
    if len(parts) != 3 or not all(parts):
        raise ValueError("request line cannot be split")

    method, target, _ = parts
    parsed = urlsplit(b"//" + target) if method == b"CONNECT" else urlsplit(target)
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
        peer = "client"
        try:
            request_head, complete = client.read_until(HEAD_TERMINATOR)
            if not request_head:
                return
            if not complete:
                logger.warning("connection closed before request head completed")
                return
            try:
                method, destination = request_route(request_head)
            except (UnicodeDecodeError, ValueError) as error:
                logger.warning("could not route request: %s", error)
                return
            peer = f"{destination[0]}:{destination[1]}"
            request_text = request_head.decode("iso-8859-1").removesuffix("\r\n\r\n")
            request_line = request_text.partition("\r\n")[0]
            logger.info("%s", request_line)
            logger.debug(
                "request head:\n        %s",
                request_text.replace("\r\n", "\n        "),
            )
            if method == b"CONNECT":
                with closing(socket.create_connection(destination)) as upstream_socket:
                    self.request.sendall(CONNECTION_ESTABLISHED)
                    self._relay_bidirectionally(
                        client,
                        BufferedSocket(upstream_socket),
                    )
            else:
                self._forward_exchange(client, request_head, method, destination)
        except OSError as error:
            logger.warning("%s: %s", peer, error)

    def _forward_exchange(
        self,
        client: BufferedSocket,
        request_head: bytes,
        method: bytes,
        destination: tuple[str, int],
    ) -> None:
        framing = request_body_framing(request_head)
        if framing is BodyKind.OPAQUE:
            logger.warning("request body boundary is ambiguous")
            return
        if has_expectation(request_head):
            logger.warning("Expect is unsupported")
            return
        with closing(socket.create_connection(destination)) as upstream_socket:
            upstream = BufferedSocket(upstream_socket)
            upstream_socket.sendall(request_head)
            if not relay_body(client, upstream_socket, framing):
                return
            self._relay_responses(client, upstream, method)

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
                if source.buffer:
                    destination.sendall(source.read(len(source.buffer)))
                selector.register(source.transport, selectors.EVENT_READ, destination)
            while selector.get_map():
                for key, _ in selector.select():
                    destination = key.data
                    chunk = key.fileobj.recv(BUFFER_SIZE)
                    if chunk:
                        destination.sendall(chunk)
                        continue
                    selector.unregister(key.fileobj)
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

    def _relay_responses(
        self,
        client: BufferedSocket,
        upstream: BufferedSocket,
        method: bytes,
    ) -> None:
        while True:
            response_head, complete = upstream.read_until(HEAD_TERMINATOR)
            if response_head:
                client.transport.sendall(response_head)
            if not complete:
                return
            parts = response_head.partition(b"\r\n")[0].split(b" ", 2)
            status = None
            if len(parts) > 1 and parts[1].isdigit():
                status = int(parts[1])
            if status is not None and 100 <= status < 200 and status != 101:
                continue
            if status is None:
                relay_to_eof(upstream, client.transport)
            elif status == 101:
                self._relay_bidirectionally(client, upstream)
            else:
                framing = response_body_framing(response_head, method, status)
                if framing is BodyKind.OPAQUE:
                    relay_to_eof(upstream, client.transport)
                else:
                    relay_body(upstream, client.transport, framing)
            return


class ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
