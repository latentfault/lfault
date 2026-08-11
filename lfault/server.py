import logging
import selectors
import socket
import socketserver
from contextlib import closing
from urllib.parse import urlsplit

from .http1 import (
    BUFFER_SIZE,
    HEAD_TERMINATOR,
    MAX_HEAD_SIZE,
    BodyFraming,
    BodyKind,
    BufferedSocket,
    HTTP1LimitError,
    connection_is_persistent,
    expects_continue,
    relay_body,
    relay_to_eof,
    request_body_framing,
    response_body_framing,
    response_status,
)
from .request import RequestHeadParseError, parse_request_head

logger = logging.getLogger(__name__)
CONNECTION_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
IO_TIMEOUT = 30


class RequestRoutingError(ValueError):
    pass


class _ResponseRelayed(Exception):
    pass


def request_destination(request_head: bytes) -> tuple[str, int]:
    request_line, _, _ = request_head.partition(b"\r\n")
    parts = request_line.split(b" ")
    if len(parts) != 3 or not all(parts):
        raise RequestRoutingError("request line cannot be split")

    method, target, _ = parts
    try:
        parsed = urlsplit(b"//" + target) if method == b"CONNECT" else urlsplit(target)
        port = parsed.port
    except ValueError as error:
        raise RequestRoutingError("request target has an invalid authority") from error
    hostname = parsed.hostname
    if method == b"CONNECT":
        if (
            not parsed.netloc
            or not hostname
            or port is None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise RequestRoutingError("CONNECT target is not a host and port")
    elif parsed.scheme.lower() != b"http" or not parsed.netloc or not hostname:
        raise RequestRoutingError("request target is not an absolute HTTP URL")
    try:
        host = hostname.decode("ascii")
    except UnicodeDecodeError as error:
        raise RequestRoutingError("request target host is not ASCII") from error
    return host, 80 if port is None else port


def _connect(destination: tuple[str, int]) -> socket.socket:
    return socket.create_connection(destination, timeout=IO_TIMEOUT)


class ProxyRequestHandler(socketserver.BaseRequestHandler):
    buffer_size = BUFFER_SIZE

    def setup(self) -> None:
        host, port = self.client_address
        self.request.settimeout(IO_TIMEOUT)
        self.logger = logging.LoggerAdapter(
            logger,
            {"context": f"client {host}:{port}"},
        )
        self.logger.info("connection opened")

    def handle(self) -> None:
        client = BufferedSocket(self.request)
        try:
            while self._handle_request(client):
                pass
        except HTTP1LimitError as error:
            self.logger.warning("HTTP/1 limit exceeded: %s", error)
        except OSError as error:
            self.logger.warning("connection failed: %s", error)

    def _handle_request(self, client: BufferedSocket) -> bool:
        request_head, complete = client.read_until(HEAD_TERMINATOR, MAX_HEAD_SIZE)
        if not request_head:
            return False
        try:
            destination = request_destination(request_head)
        except RequestRoutingError as error:
            request_line = request_head.partition(b"\r\n")[0]
            self.logger.warning("could not route request %r: %s", request_line, error)
            return False
        request_line = request_head.partition(b"\r\n")[0]
        method, _, version = request_line.split(b" ")
        if method == b"CONNECT" and not complete:
            self.logger.warning("connection closed before CONNECT head completed")
            return False
        try:
            parse_request_head(request_head)
        except RequestHeadParseError as error:
            self.logger.warning("forwarding unparseable request head: %s", error)
        request_text = request_head.decode("iso-8859-1").removesuffix("\r\n\r\n")
        self.logger.info("incoming request:\n%s", request_text)
        if method == b"CONNECT":
            self._tunnel(client, destination)
            return False
        if not complete:
            self._forward_incomplete(request_head, destination)
            return False
        return self._forward_exchange(
            client,
            request_head,
            method,
            version,
            destination,
        )

    def _forward_incomplete(
        self,
        request_head: bytes,
        destination: tuple[str, int],
    ) -> None:
        with closing(_connect(destination)) as upstream:
            upstream.sendall(request_head)
            upstream.shutdown(socket.SHUT_WR)
            relay_to_eof(BufferedSocket(upstream), self.request)

    def _forward_exchange(
        self,
        client: BufferedSocket,
        request_head: bytes,
        method: bytes,
        version: bytes,
        destination: tuple[str, int],
    ) -> bool:
        with closing(_connect(destination)) as upstream_socket:
            upstream = BufferedSocket(upstream_socket)
            upstream_socket.sendall(request_head)
            framing = request_body_framing(request_head)
            if (
                expects_continue(request_head)
                and not (framing.kind is BodyKind.LENGTH and framing.length == 0)
                and not self._await_continue(client, upstream, method)
            ):
                return False
            if framing.kind is BodyKind.OPAQUE:
                self._relay_bidirectionally(client, upstream)
                return False
            body_complete = self._relay_request_body(
                client,
                upstream,
                framing,
                method,
            )
            if body_complete is None:
                return False
            if not body_complete:
                if client.eof:
                    upstream_socket.shutdown(socket.SHUT_WR)
                    self._relay_responses(client, upstream, method)
                else:
                    self._relay_bidirectionally(client, upstream)
                return False
            response_complete = self._relay_responses(client, upstream, method)
            return response_complete and connection_is_persistent(
                request_head,
                version,
                proxy=True,
            )

    def _tunnel(
        self,
        client: BufferedSocket,
        destination: tuple[str, int],
    ) -> None:
        with closing(_connect(destination)) as upstream_socket:
            self.request.sendall(CONNECTION_ESTABLISHED)
            self._relay_bidirectionally(client, BufferedSocket(upstream_socket))

    def _relay_request_body(
        self,
        client: BufferedSocket,
        upstream: BufferedSocket,
        framing: BodyFraming,
        method: bytes,
    ) -> bool | None:
        selector = None

        def wait_for_input() -> None:
            nonlocal selector
            if selector is None:
                selector = selectors.DefaultSelector()
                selector.register(client.transport, selectors.EVENT_READ, "client")
                selector.register(upstream.transport, selectors.EVENT_READ, "upstream")
            while True:
                if upstream.buffer:
                    ready = {"upstream"}
                else:
                    events = selector.select(IO_TIMEOUT)
                    if not events:
                        raise TimeoutError("request body transfer timed out")
                    ready = {key.data for key, _ in events}
                if "upstream" in ready and not self._relay_early_response(
                    client,
                    upstream,
                    method,
                ):
                    raise _ResponseRelayed
                if "client" in ready:
                    return

        previous_callback = client.before_receive
        client.before_receive = wait_for_input
        try:
            return relay_body(client, upstream.transport, framing)
        except _ResponseRelayed:
            return None
        except OSError:
            if upstream.buffer or self._is_readable(upstream.transport):
                self._relay_early_response(client, upstream, method)
                return None
            raise
        finally:
            client.before_receive = previous_callback
            if selector is not None:
                selector.close()

    @staticmethod
    def _is_readable(transport: socket.socket) -> bool:
        with selectors.DefaultSelector() as selector:
            selector.register(transport, selectors.EVENT_READ)
            return bool(selector.select(0))

    def _relay_bidirectionally(
        self,
        client: BufferedSocket,
        upstream: BufferedSocket,
    ) -> None:
        streams = (
            (client, upstream.transport),
            (upstream, client.transport),
        )
        with selectors.DefaultSelector() as selector:
            for source, destination in streams:
                source.flush_to(destination)
                if source.eof:
                    self._shutdown_write(destination)
                else:
                    selector.register(
                        source.transport,
                        selectors.EVENT_READ,
                        (source, destination),
                    )
            while selector.get_map():
                for key, _ in selector.select():
                    source, destination = key.data
                    chunk = source.transport.recv(self.buffer_size)
                    if chunk:
                        destination.sendall(chunk)
                        continue
                    source.eof = True
                    selector.unregister(source.transport)
                    self._shutdown_write(destination)

    @staticmethod
    def _shutdown_write(transport: socket.socket) -> None:
        try:
            transport.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _await_continue(
        self,
        client: BufferedSocket,
        upstream: BufferedSocket,
        method: bytes,
    ) -> bool:
        if client.buffer:
            return True
        with selectors.DefaultSelector() as selector:
            selector.register(client.transport, selectors.EVENT_READ, "client")
            selector.register(upstream.transport, selectors.EVENT_READ, "upstream")
            while True:
                events = selector.select(IO_TIMEOUT)
                if not events:
                    raise TimeoutError("100 Continue wait timed out")
                ready = {key.data for key, _ in events}
                if "upstream" in ready:
                    response = self._relay_response_head(client, upstream)
                    if response is None:
                        return False
                    response_head, status = response
                    if status == 100:
                        return True
                    if status is None or not 100 <= status < 200 or status == 101:
                        self._relay_final_response(
                            client,
                            upstream,
                            response_head,
                            method,
                            status,
                        )
                        return False
                if "client" in ready:
                    return True

    def _relay_responses(
        self,
        client: BufferedSocket,
        upstream: BufferedSocket,
        method: bytes,
    ) -> bool:
        while True:
            response = self._relay_response_head(client, upstream)
            if response is None:
                return False
            response_head, status = response
            if status is not None and 100 <= status < 200 and status != 101:
                continue
            return self._relay_final_response(
                client,
                upstream,
                response_head,
                method,
                status,
            )

    def _relay_early_response(
        self,
        client: BufferedSocket,
        upstream: BufferedSocket,
        method: bytes,
    ) -> bool:
        response = self._relay_response_head(client, upstream)
        if response is None:
            return False
        response_head, status = response
        if status is not None and 100 <= status < 200 and status != 101:
            return True
        self._relay_final_response(client, upstream, response_head, method, status)
        return False

    @staticmethod
    def _relay_response_head(
        client: BufferedSocket,
        upstream: BufferedSocket,
    ) -> tuple[bytes, int | None] | None:
        response_head, complete = upstream.read_until(
            HEAD_TERMINATOR,
            MAX_HEAD_SIZE,
        )
        if response_head:
            client.transport.sendall(response_head)
        if not complete:
            return None
        return response_head, response_status(response_head)

    def _relay_final_response(
        self,
        client: BufferedSocket,
        upstream: BufferedSocket,
        response_head: bytes,
        method: bytes,
        status: int | None,
    ) -> bool:
        if status is None:
            relay_to_eof(upstream, client.transport)
            return False
        if status == 101:
            self._relay_bidirectionally(client, upstream)
            return False
        framing = response_body_framing(response_head, method, status)
        if framing.kind is BodyKind.OPAQUE:
            relay_to_eof(upstream, client.transport)
            return False
        complete = relay_body(upstream, client.transport, framing)
        if not complete:
            relay_to_eof(upstream, client.transport)
            return False
        response_version = response_head.partition(b" ")[0]
        return connection_is_persistent(response_head, response_version)


class ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.logger = logging.LoggerAdapter(logger, {"context": "server"})
        super().__init__((host, port), ProxyRequestHandler)

    def run(self) -> None:
        host, port = self.server_address
        self.logger.info("listening on %s:%s", host, port)
        self.serve_forever()
