import unittest
from unittest.mock import patch

from lfault import http1
from lfault.server import ProxyRequestHandler

CREATE_CONNECTION_PATH = "lfault.server.socket.create_connection"


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
    return handler


class ForwardingTests(unittest.TestCase):
    no_content_response = b"HTTP/1.1 204 No Content\r\n\r\n"

    def assert_request_rejected(self, *incoming: bytes) -> None:
        client = SocketDouble(*incoming)
        with (
            self.assertLogs("lfault.server", level="WARNING"),
            patch(CREATE_CONNECTION_PATH) as connect,
        ):
            make_handler(client).handle()

        connect.assert_not_called()

    def test_relays_one_http_exchange_verbatim(self) -> None:
        request_head = (
            b"POST http://upstream.test/path HTTP/1.1\r\n"
            b"Host: spoofed.test\r\n"
            b"X-Test: deliberately-testable\r\n"
            b"Content-Length: 4\r\n"
            b"Malformed field\r\n\r\n"
        )
        informational = b"HTTP/1.1 103 Early Hints\r\n\r\n"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody"
        client = SocketDouble(request_head + b"body")
        upstream = SocketDouble(informational + response)

        with patch(CREATE_CONNECTION_PATH, return_value=upstream) as connect:
            make_handler(client).handle()

        connect.assert_called_once_with(("upstream.test", 80))
        self.assertEqual(upstream.sent, [request_head, b"body"])
        self.assertEqual(b"".join(client.sent), informational + response)

    def test_rejects_expectation(self) -> None:
        request = (
            b"POST http://upstream.test/ HTTP/1.1\r\n"
            b"Content-Length: 4\r\n"
            b"Expect: 100-continue\r\n\r\n"
        )
        self.assert_request_rejected(request)

    def test_rejects_ambiguous_request_framing(self) -> None:
        request = (
            b"POST http://upstream.test/ HTTP/1.1\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Length: 4\r\n\r\n"
        )
        self.assert_request_rejected(request)

    def test_rejects_incomplete_request_head(self) -> None:
        self.assert_request_rejected(
            b"CONNECT upstream.test:443 HTTP/1.1\r\n", b""
        )

    def test_relays_a_chunked_request_with_extensions_and_trailers(self) -> None:
        request_head = (
            b"POST http://upstream.test/ HTTP/1.1\r\n"
            b"Transfer-Encoding: gzip, chunked\r\n\r\n"
        )
        request_body = b"4;note=value\r\nbody\r\n0\r\nX-Trailer: value\r\n\r\n"
        client = SocketDouble(request_head + request_body)
        upstream = SocketDouble(self.no_content_response)

        with patch(CREATE_CONNECTION_PATH, return_value=upstream):
            make_handler(client).handle()

        self.assertEqual(b"".join(upstream.sent), request_head + request_body)

    def test_logs_request_metadata_without_the_raw_target(self) -> None:
        request_target = b"http://upstream.test/private?token=target-secret"
        request_head = (
            b"GET " + request_target + b" HTTP/1.1\r\nHost: upstream.test\r\n\r\n"
        )
        client = SocketDouble(request_head)
        upstream = SocketDouble(self.no_content_response)

        with (
            self.assertLogs("lfault.server", level="INFO") as captured,
            patch(CREATE_CONNECTION_PATH, return_value=upstream),
        ):
            make_handler(client).handle()

        log_output = "\n".join(captured.output)
        self.assertIn("b'GET'", log_output)
        self.assertIn("client.test", log_output)
        self.assertIn("upstream.test", log_output)
        self.assertNotIn(request_target.decode("ascii"), log_output)
        self.assertEqual(upstream.sent, [request_head])

    def test_stops_after_one_exchange(self) -> None:
        request = b"GET http://upstream.test/ HTTP/1.1\r\n\r\n"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        client = SocketDouble(request + b"GET http://next.test/ HTTP/1.1\r\n\r\n")
        upstream = SocketDouble(response + b"following")

        with patch(CREATE_CONNECTION_PATH, return_value=upstream) as connect:
            make_handler(client).handle()

        connect.assert_called_once_with(("upstream.test", 80))
        self.assertEqual(upstream.sent, [request])
        self.assertEqual(b"".join(client.sent), response)
        self.assertTrue(upstream.closed)

    def test_head_response_does_not_read_a_body(self) -> None:
        request = b"HEAD http://upstream.test/ HTTP/1.1\r\n\r\n"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
        client = SocketDouble(request)
        upstream = SocketDouble(response)

        with patch(CREATE_CONNECTION_PATH, return_value=upstream):
            make_handler(client).handle()

        self.assertEqual(client.sent, [response])
        self.assertTrue(upstream.closed)

    def test_relays_unframed_response_to_eof(self) -> None:
        client = SocketDouble(b"GET http://upstream.test/ HTTP/1.1\r\n\r\n")
        response = b"HTTP/1.1 200 OK\r\n\r\nbody"
        upstream = SocketDouble(response, b"more", b"")

        with patch(CREATE_CONNECTION_PATH, return_value=upstream):
            make_handler(client).handle()

        self.assertEqual(b"".join(client.sent), response + b"more")
        self.assertTrue(upstream.closed)

    def test_switching_protocols_preserves_both_stream_buffers(self) -> None:
        request = b"GET http://upstream.test/ HTTP/1.1\r\n\r\n"
        response = b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
        client = SocketDouble(request + b"client data")
        upstream = SocketDouble(response + b"upstream data")

        with (
            patch(CREATE_CONNECTION_PATH, return_value=upstream),
            patch("lfault.server.streams.relay_bidirectionally") as relay,
        ):
            make_handler(client).handle()

        self.assertEqual(upstream.sent, [request])
        self.assertEqual(client.sent, [response])
        client_stream, upstream_stream = relay.call_args.args
        self.assertEqual(client_stream.read(11), b"client data")
        self.assertEqual(upstream_stream.read(13), b"upstream data")
        self.assertTrue(upstream.closed)


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
            patch("lfault.server.streams.relay_bidirectionally") as relay,
            patch(CREATE_CONNECTION_PATH, return_value=upstream) as connect,
        ):
            handler.handle()

        connect.assert_called_once_with(("upstream.test", 443))
        self.assertEqual(client.sent, [http1.CONNECT_ESTABLISHED_RESPONSE])
        client_stream, upstream_stream = relay.call_args.args
        self.assertEqual(client_stream.read(len(client_hello)), client_hello)
        self.assertIs(upstream_stream.transport, upstream)
        self.assertTrue(upstream.closed)

    def test_failed_tunnel_is_not_acknowledged(self) -> None:
        client = SocketDouble(self.request_head)
        with (
            self.assertLogs("lfault.server", level="WARNING"),
            patch(CREATE_CONNECTION_PATH, side_effect=OSError("connection failed")),
        ):
            make_handler(client).handle()

        self.assertEqual(client.sent, [])
