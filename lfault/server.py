"""Coordinate one HTTP exchange or tunnel per accepted client connection."""

import logging
import socket
import socketserver

from . import http1, streams

logger = logging.getLogger(__name__)


def _read_request(
    client: streams.BufferedSocket, context: str
) -> http1.RoutedRequest | None:
    request_head, complete = client.read_until(http1.HEAD_TERMINATOR)
    if not request_head:
        return None
    if not complete:
        logger.warning("%s closed before request head completed", context)
        return None

    try:
        return http1.parse_request_route(request_head)
    except ValueError:
        logger.warning("could not route request from %s", context)
        return None


def _open_tunnel(client: streams.BufferedSocket, destination: http1.Address) -> None:
    with socket.create_connection(destination) as upstream_socket:
        client.transport.sendall(http1.CONNECT_ESTABLISHED_RESPONSE)
        streams.relay_bidirectionally(client, streams.BufferedSocket(upstream_socket))


class ProxyRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = streams.BufferedSocket(self.request)
        context = f"client {self.client_address!r}"
        try:
            request = _read_request(client, context)
            if request is None:
                return

            context = f"{context}, upstream {request.destination!r}"
            logger.info("%r request from %s", request.method, context)

            if request.method == http1.CONNECT_METHOD:
                _open_tunnel(client, request.destination)
            else:
                self._forward_exchange(client, request)
        except OSError as error:
            logger.warning("I/O failure while handling %s: %s", context, error)

    def _forward_exchange(
        self, client: streams.BufferedSocket, request: http1.RoutedRequest
    ) -> None:
        endpoints = self.client_address, request.destination
        framing = http1.request_body_framing(request.head)
        if framing is http1.BodyKind.OPAQUE:
            logger.warning("%r -> %r: ambiguous request body", *endpoints)
            return
        if http1.has_expectation(request.head):
            logger.warning("%r -> %r: unsupported Expect", *endpoints)
            return

        with socket.create_connection(request.destination) as upstream_socket:
            upstream = streams.BufferedSocket(upstream_socket)
            upstream_socket.sendall(request.head)

            if not http1.relay_body(client, upstream_socket, framing):
                logger.warning("%r -> %r: incomplete/malformed body", *endpoints)
                return

            self._relay_responses(client, upstream, request)

    def _relay_responses(
        self,
        client: streams.BufferedSocket,
        upstream: streams.BufferedSocket,
        request: http1.RoutedRequest,
    ) -> None:
        endpoints = self.client_address, request.destination
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
                client, upstream, response_head, request, status
            )
            return

    def _relay_final_response(
        self,
        client: streams.BufferedSocket,
        upstream: streams.BufferedSocket,
        response_head: bytes,
        request: http1.RoutedRequest,
        status: int | None,
    ) -> None:
        endpoints = self.client_address, request.destination
        if status is None:
            logger.warning("%r <- %r: bad status; relaying to EOF", *endpoints)
            streams.relay_to_eof(upstream, client.transport)
            return

        if status == 101:
            streams.relay_bidirectionally(client, upstream)
            return

        framing = http1.final_response_body_framing(
            response_head, request.method, status
        )
        if framing is http1.BodyKind.OPAQUE:
            streams.relay_to_eof(upstream, client.transport)
        elif not http1.relay_body(upstream, client.transport, framing):
            logger.warning("%r <- %r: incomplete/malformed body", *endpoints)


class ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
