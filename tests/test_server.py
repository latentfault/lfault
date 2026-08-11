import unittest
from unittest.mock import Mock, patch

from lfault.server import (
    CONNECTION_ESTABLISHED,
    ProxyRequestHandler,
)


class SocketDouble:
    def __init__(self, *incoming: bytes) -> None:
        self.incoming = list(incoming)
        self.sent: list[bytes] = []
        self.closed = False

    def recv(self, size: int) -> bytes:
        if not self.incoming:
            raise AssertionError("unexpected receive")
        chunk = self.incoming.pop(0)
        if len(chunk) > size:
            self.incoming.insert(0, chunk[size:])
            return chunk[:size]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def close(self) -> None:
        self.closed = True


def make_handler(client: SocketDouble) -> ProxyRequestHandler:
    handler = ProxyRequestHandler.__new__(ProxyRequestHandler)
    handler.request = client
    return handler


class ForwardingTests(unittest.TestCase):
    empty_response = b"HTTP/1.1 204 No Content\r\n\r\n"

    def test_relays_one_http_exchange_verbatim(self) -> None:
        request_head = (
            b"POST http://upstream.test/path HTTP/1.1\r\n"
            b"Host: spoofed.test\r\n"
            b"Proxy-Authorization: Basic deliberately-testable\r\n"
            b"Content-Length: 4\r\n"
            b"Malformed field\r\n\r\n"
        )
        informational = b"HTTP/1.1 103 Early Hints\r\n\r\n"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody"
        client = SocketDouble(request_head + b"body")
        upstream = SocketDouble(informational + response)

        with patch(
            "lfault.server.socket.create_connection",
            return_value=upstream,
        ) as connect:
            make_handler(client).handle()

        connect.assert_called_once_with(("upstream.test", 80))
        self.assertEqual(upstream.sent, [request_head, b"body"])
        self.assertEqual(b"".join(client.sent), informational + response)

    def test_rejects_requests_it_cannot_relay(self) -> None:
        cases = (
            (
                "expectation",
                (
                    b"POST http://upstream.test/ HTTP/1.1\r\n"
                    b"Content-Length: 4\r\n"
                    b"Expect: 100-continue\r\n\r\n",
                ),
            ),
            (
                "ambiguous framing",
                (
                    b"POST http://upstream.test/ HTTP/1.1\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"Content-Length: 4\r\n\r\n",
                ),
            ),
            (
                "incomplete head",
                (b"CONNECT upstream.test:443 HTTP/1.1\r\n", b""),
            ),
        )
        for name, incoming in cases:
            with self.subTest(name):
                client = SocketDouble(*incoming)
                with patch("lfault.server.socket.create_connection") as connect:
                    make_handler(client).handle()
                connect.assert_not_called()

    def test_relays_a_chunked_request_with_extensions_and_trailers(self) -> None:
        request_head = (
            b"POST http://upstream.test/ HTTP/1.1\r\n"
            b"Transfer-Encoding: gzip, chunked\r\n\r\n"
        )
        request_body = b"4;note=value\r\nbody\r\n0\r\nX-Trailer: value\r\n\r\n"
        client = SocketDouble(request_head + request_body)
        upstream = SocketDouble(self.empty_response)

        with patch("lfault.server.socket.create_connection", return_value=upstream):
            make_handler(client).handle()

        self.assertEqual(b"".join(upstream.sent), request_head + request_body)


class ConnectTunnelTests(unittest.TestCase):
    def test_acknowledges_only_an_established_tunnel_and_preserves_buffer(self) -> None:
        request = (
            b"CONNECT upstream.test:443 HTTP/1.1\r\n"
            b"Host: spoofed.test:8443\r\n\r\n"
        )
        client_hello = b"\x16\x03\x01client hello"
        client = SocketDouble(request + client_hello)
        handler = make_handler(client)
        handler._relay_bidirectionally = Mock()
        upstream = SocketDouble()

        with patch(
            "lfault.server.socket.create_connection",
            return_value=upstream,
        ) as connect:
            handler.handle()

        connect.assert_called_once_with(("upstream.test", 443))
        self.assertEqual(client.sent, [CONNECTION_ESTABLISHED])
        client_stream, upstream_stream = handler._relay_bidirectionally.call_args.args
        self.assertEqual(bytes(client_stream.buffer), client_hello)
        self.assertIs(upstream_stream.transport, upstream)
        self.assertTrue(upstream.closed)

        failed_client = SocketDouble(request)
        with patch(
            "lfault.server.socket.create_connection",
            side_effect=OSError("connection failed"),
        ):
            make_handler(failed_client).handle()

        self.assertEqual(failed_client.sent, [])
