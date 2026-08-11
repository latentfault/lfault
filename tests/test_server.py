import unittest
from unittest.mock import Mock, patch

from lfault.server import (
    CONNECT_ESTABLISHED_RESPONSE,
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

    def __enter__(self) -> "SocketDouble":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def make_handler(client: SocketDouble) -> ProxyRequestHandler:
    # BaseRequestHandler.__init__ runs handle immediately; tests configure first.
    handler = ProxyRequestHandler.__new__(ProxyRequestHandler)
    handler.request = client
    handler.client_address = ("client.test", 12345)
    handler.server = Mock()
    return handler


class ForwardingTests(unittest.TestCase):
    no_content_response = b"HTTP/1.1 204 No Content\r\n\r\n"

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
            with self.subTest(case=name):
                client = SocketDouble(*incoming)
                with (
                    self.assertLogs("lfault.server", level="WARNING"),
                    patch("lfault.server.socket.create_connection") as connect,
                ):
                    make_handler(client).handle()
                connect.assert_not_called()

    def test_relays_a_chunked_request_with_extensions_and_trailers(self) -> None:
        request_head = (
            b"POST http://upstream.test/ HTTP/1.1\r\n"
            b"Transfer-Encoding: gzip, chunked\r\n\r\n"
        )
        request_body = b"4;note=value\r\nbody\r\n0\r\nX-Trailer: value\r\n\r\n"
        client = SocketDouble(request_head + request_body)
        upstream = SocketDouble(self.no_content_response)

        with patch("lfault.server.socket.create_connection", return_value=upstream):
            make_handler(client).handle()

        self.assertEqual(b"".join(upstream.sent), request_head + request_body)

    def test_logs_request_metadata_without_sensitive_values(self) -> None:
        request_head = (
            b"GET http://user:target-secret@upstream.test/path?token=query-secret "
            b"HTTP/1.1\r\n"
            b"Proxy-Authorization: Basic header-secret\r\n"
            b"Cookie: session=cookie-secret\r\n\r\n"
        )
        client = SocketDouble(request_head)
        upstream = SocketDouble(self.no_content_response)

        with (
            self.assertLogs("lfault.server", level="INFO") as captured,
            patch(
                "lfault.server.socket.create_connection",
                return_value=upstream,
            ),
        ):
            make_handler(client).handle()

        log_output = "\n".join(captured.output)
        self.assertIn("b'GET'", log_output)
        self.assertIn("client.test", log_output)
        self.assertIn("upstream.test", log_output)
        self.assertNotIn("target-secret", log_output)
        self.assertNotIn("query-secret", log_output)
        self.assertNotIn("header-secret", log_output)
        self.assertNotIn("cookie-secret", log_output)
        self.assertEqual(upstream.sent, [request_head])

    def test_does_not_log_an_invalid_request_target(self) -> None:
        client = SocketDouble(
            b"GET http://upstream.test:route-secret/ HTTP/1.1\r\n\r\n"
        )

        with (
            self.assertLogs("lfault.server", level="WARNING") as captured,
            patch("lfault.server.socket.create_connection") as connect,
        ):
            make_handler(client).handle()

        connect.assert_not_called()
        self.assertNotIn("route-secret", "\n".join(captured.output))


class ConnectTunnelTests(unittest.TestCase):
    request_head = (
        b"CONNECT upstream.test:443 HTTP/1.1\r\n"
        b"Host: spoofed.test:8443\r\n\r\n"
    )

    def test_established_tunnel_is_acknowledged_and_preserves_buffer(self) -> None:
        client_hello = b"\x16\x03\x01client hello"
        client = SocketDouble(self.request_head + client_hello)
        handler = make_handler(client)
        upstream = SocketDouble()

        with (
            patch.object(handler, "_relay_bidirectionally") as relay,
            patch(
                "lfault.server.socket.create_connection",
                return_value=upstream,
            ) as connect,
        ):
            handler.handle()

        connect.assert_called_once_with(("upstream.test", 443))
        self.assertEqual(client.sent, [CONNECT_ESTABLISHED_RESPONSE])
        client_stream, upstream_stream = relay.call_args.args
        self.assertEqual(client_stream.read(len(client_hello)), client_hello)
        self.assertIs(upstream_stream.transport, upstream)
        self.assertTrue(upstream.closed)

    def test_failed_tunnel_is_not_acknowledged(self) -> None:
        client = SocketDouble(self.request_head)
        with (
            self.assertLogs("lfault.server", level="WARNING"),
            patch(
                "lfault.server.socket.create_connection",
                side_effect=OSError("connection failed"),
            ),
        ):
            make_handler(client).handle()

        self.assertEqual(client.sent, [])
