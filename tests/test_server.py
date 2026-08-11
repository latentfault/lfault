import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from lfault.http1 import BufferedSocket
from lfault.server import (
    CONNECTION_ESTABLISHED,
    IO_TIMEOUT,
    ProxyRequestHandler,
    RequestRoutingError,
    request_destination,
)


class SelectorDouble:
    def __init__(self, *readable: list[object]) -> None:
        self.readable = list(readable)
        self.registered: dict[object, object] = {}

    def __enter__(self) -> "SelectorDouble":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def register(self, fileobj: object, events: int, data: object) -> None:
        self.registered[fileobj] = data

    def unregister(self, fileobj: object) -> None:
        del self.registered[fileobj]

    def select(self, timeout: float | None = None) -> list[tuple[object, int]]:
        readable = self.readable.pop(0)
        return [
            (
                SimpleNamespace(fileobj=fileobj, data=self.registered[fileobj]),
                1,
            )
            for fileobj in readable
        ]

    def get_map(self) -> dict[object, object]:
        return self.registered

    def close(self) -> None:
        self.registered.clear()


class SocketDouble:
    def __init__(self, *incoming: bytes) -> None:
        self.incoming = list(incoming)
        self.sent: list[bytes] = []
        self.shutdowns: list[int] = []
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

    def shutdown(self, how: int) -> None:
        self.shutdowns.append(how)

    def close(self) -> None:
        self.closed = True


def make_handler(client: SocketDouble) -> ProxyRequestHandler:
    handler = ProxyRequestHandler.__new__(ProxyRequestHandler)
    handler.request = client
    handler.logger = Mock()
    return handler


class RequestRoutingTests(unittest.TestCase):
    def test_routes_from_the_absolute_target_without_using_host(self) -> None:
        request_head = (
            b"GET http://upstream.test:8080/path HTTP/1.1\r\n"
            b"Host: spoofed.test\r\n\r\n"
        )
        self.assertEqual(request_destination(request_head), ("upstream.test", 8080))

    def test_uses_the_default_http_port(self) -> None:
        request_head = b"GET http://upstream.test/path HTTP/1.1\r\n\r\n"
        self.assertEqual(request_destination(request_head), ("upstream.test", 80))

    def test_does_not_route_an_origin_form_target_from_host(self) -> None:
        request_head = b"GET /path HTTP/1.1\r\nHost: upstream.test\r\n\r\n"
        with self.assertRaises(RequestRoutingError):
            request_destination(request_head)

    def test_routes_connect_from_its_authority_without_using_host(self) -> None:
        request_head = (
            b"CONNECT upstream.test:443 HTTP/1.1\r\n"
            b"Host: spoofed.test:8443\r\n\r\n"
        )
        self.assertEqual(request_destination(request_head), ("upstream.test", 443))

    def test_requires_a_port_in_a_connect_target(self) -> None:
        request_head = b"CONNECT upstream.test HTTP/1.1\r\n\r\n"
        with self.assertRaises(RequestRoutingError):
            request_destination(request_head)


class ForwardingTests(unittest.TestCase):
    empty_response = b"HTTP/1.1 204 No Content\r\n\r\n"

    def test_relays_a_framed_response_without_half_closing_upstream(self) -> None:
        request = (
            b"GET http://upstream.test/ HTTP/1.1\r\n"
            b"Proxy-Connection: Keep-Alive\r\n\r\n"
        )
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 4\r\n"
            b"Connection: keep-alive\r\n\r\nbody"
        )
        client = SocketDouble(request, b"")
        upstream = SocketDouble(response)

        with patch("lfault.server.socket.create_connection", return_value=upstream):
            make_handler(client).handle()

        self.assertEqual(upstream.sent, [request])
        self.assertEqual(upstream.shutdowns, [])
        self.assertEqual(b"".join(client.sent), response)

    def test_preserves_an_unparseable_head_and_its_framed_body(self) -> None:
        request_head = (
            b"POST http://upstream.test:8080/path HTTP/1.1\r\n"
            b"Content-Length: 4\r\n"
            b"Malformed field\r\n"
            b"Proxy-Connection: close\r\n\r\n"
        )
        client = SocketDouble(request_head + b"body")
        upstream = SocketDouble(self.empty_response)

        with patch(
            "lfault.server.socket.create_connection",
            return_value=upstream,
        ) as connect:
            make_handler(client).handle()

        connect.assert_called_once_with(
            ("upstream.test", 8080),
            timeout=IO_TIMEOUT,
        )
        self.assertEqual(upstream.sent, [request_head, b"body"])

    def test_forwards_proxy_authorization_unchanged(self) -> None:
        request = (
            b"GET http://upstream.test/ HTTP/1.1\r\n"
            b"Proxy-Authorization: Basic deliberately-testable\r\n"
            b"Proxy-Connection: close\r\n\r\n"
        )
        client = SocketDouble(request)
        upstream = SocketDouble(self.empty_response)

        with patch("lfault.server.socket.create_connection", return_value=upstream):
            make_handler(client).handle()

        self.assertEqual(upstream.sent, [request])

    def test_relays_continue_before_reading_the_request_body(self) -> None:
        request_head = (
            b"POST http://upstream.test/ HTTP/1.1\r\n"
            b"Content-Length: 4\r\n"
            b"Expect: 100-continue\r\n"
            b"Proxy-Connection: close\r\n\r\n"
        )
        interim = b"HTTP/1.1 100 Continue\r\n\r\n"
        client = SocketDouble(request_head, b"body")
        upstream = SocketDouble(interim, self.empty_response)
        continue_selector = SelectorDouble([upstream])
        body_selector = SelectorDouble([client])

        with (
            patch("lfault.server.socket.create_connection", return_value=upstream),
            patch(
                "lfault.server.selectors.DefaultSelector",
                side_effect=[continue_selector, body_selector],
            ),
        ):
            make_handler(client).handle()

        self.assertEqual(upstream.sent, [request_head, b"body"])
        self.assertEqual(b"".join(client.sent), interim + self.empty_response)

    def test_accepts_a_body_when_the_client_stops_waiting_for_continue(self) -> None:
        request_head = (
            b"POST http://upstream.test/ HTTP/1.1\r\n"
            b"Content-Length: 4\r\n"
            b"Expect: 100-continue\r\n"
            b"Proxy-Connection: close\r\n\r\n"
        )
        client = SocketDouble(request_head, b"body")
        upstream = SocketDouble(self.empty_response)
        continue_selector = SelectorDouble([client])
        body_selector = SelectorDouble([client])

        with (
            patch("lfault.server.socket.create_connection", return_value=upstream),
            patch(
                "lfault.server.selectors.DefaultSelector",
                side_effect=[continue_selector, body_selector],
            ),
        ):
            make_handler(client).handle()

        self.assertEqual(upstream.sent, [request_head, b"body"])
        self.assertEqual(b"".join(client.sent), self.empty_response)

    def test_relays_a_final_response_before_the_request_body_finishes(self) -> None:
        request_head = (
            b"POST http://upstream.test/ HTTP/1.1\r\n"
            b"Content-Length: 8\r\n\r\n"
        )
        response = (
            b"HTTP/1.1 413 Content Too Large\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
        client = SocketDouble(request_head + b"part", b"rest")
        upstream = SocketDouble(response)
        selector = SelectorDouble([upstream])

        with (
            patch("lfault.server.socket.create_connection", return_value=upstream),
            patch("lfault.server.selectors.DefaultSelector", return_value=selector),
        ):
            make_handler(client).handle()

        self.assertEqual(upstream.sent, [request_head, b"part"])
        self.assertEqual(b"".join(client.sent), response)
        self.assertEqual(client.incoming, [b"rest"])

    def test_routes_each_request_on_a_persistent_client_connection(self) -> None:
        first = b"GET http://first.test/ HTTP/1.1\r\n\r\n"
        second = (
            b"GET http://second.test/ HTTP/1.1\r\n"
            b"Proxy-Connection: close\r\n\r\n"
        )
        client = SocketDouble(first, second)
        first_upstream = SocketDouble(self.empty_response)
        second_upstream = SocketDouble(self.empty_response)

        with patch(
            "lfault.server.socket.create_connection",
            side_effect=[first_upstream, second_upstream],
        ) as connect:
            make_handler(client).handle()

        self.assertEqual(
            connect.call_args_list,
            [
                call(("first.test", 80), timeout=IO_TIMEOUT),
                call(("second.test", 80), timeout=IO_TIMEOUT),
            ],
        )
        self.assertEqual(first_upstream.sent, [first])
        self.assertEqual(second_upstream.sent, [second])

    def test_routes_bytes_after_a_content_length_body_as_another_request(self) -> None:
        first_head = (
            b"POST http://first.test/ HTTP/1.1\r\n"
            b"Content-Length: 4\r\n\r\n"
        )
        second = (
            b"GET http://second.test/ HTTP/1.1\r\n"
            b"Proxy-Connection: close\r\n\r\n"
        )
        client = SocketDouble(first_head + b"body" + second)
        first_upstream = SocketDouble(self.empty_response)
        second_upstream = SocketDouble(self.empty_response)

        with patch(
            "lfault.server.socket.create_connection",
            side_effect=[first_upstream, second_upstream],
        ) as connect:
            make_handler(client).handle()

        self.assertEqual(
            connect.call_args_list,
            [
                call(("first.test", 80), timeout=IO_TIMEOUT),
                call(("second.test", 80), timeout=IO_TIMEOUT),
            ],
        )
        self.assertEqual(first_upstream.sent, [first_head, b"body"])
        self.assertEqual(second_upstream.sent, [second])

    def test_routes_bytes_after_a_chunked_body_as_another_request(self) -> None:
        first_head = (
            b"POST http://first.test/ HTTP/1.1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        first_body = b"4\r\nbody\r\n0\r\nX-Trailer: value\r\n\r\n"
        second = (
            b"GET http://second.test/ HTTP/1.1\r\n"
            b"Proxy-Connection: close\r\n\r\n"
        )
        client = SocketDouble(first_head + first_body + second)
        first_upstream = SocketDouble(self.empty_response)
        second_upstream = SocketDouble(self.empty_response)

        with patch(
            "lfault.server.socket.create_connection",
            side_effect=[first_upstream, second_upstream],
        ) as connect:
            make_handler(client).handle()

        self.assertEqual(
            connect.call_args_list,
            [
                call(("first.test", 80), timeout=IO_TIMEOUT),
                call(("second.test", 80), timeout=IO_TIMEOUT),
            ],
        )
        self.assertEqual(b"".join(first_upstream.sent), first_head + first_body)
        self.assertEqual(second_upstream.sent, [second])


class ConnectTunnelTests(unittest.TestCase):
    def test_establishes_a_tunnel_with_trailing_client_bytes_buffered(self) -> None:
        request_head = b"CONNECT upstream.test:443 HTTP/1.1\r\n\r\n"
        client_hello = b"\x16\x03\x01client hello"
        client = SocketDouble(request_head + client_hello)
        handler = make_handler(client)
        handler._relay_bidirectionally = Mock()
        upstream = SocketDouble()

        with patch(
            "lfault.server.socket.create_connection",
            return_value=upstream,
        ) as connect:
            handler.handle()

        connect.assert_called_once_with(
            ("upstream.test", 443),
            timeout=IO_TIMEOUT,
        )
        self.assertEqual(client.sent, [CONNECTION_ESTABLISHED])
        client_stream, upstream_stream = handler._relay_bidirectionally.call_args.args
        self.assertEqual(bytes(client_stream.buffer), client_hello)
        self.assertIs(upstream_stream.transport, upstream)
        self.assertTrue(upstream.closed)

    def test_keeps_receiving_after_the_upstream_half_closes(self) -> None:
        client = SocketDouble(b"late client data", b"")
        upstream = SocketDouble(b"")
        selector = SelectorDouble([upstream], [client], [client])

        with patch("lfault.server.selectors.DefaultSelector", return_value=selector):
            make_handler(client)._relay_bidirectionally(
                BufferedSocket(client),
                BufferedSocket(upstream),
            )

        self.assertEqual(upstream.sent, [b"late client data"])
        self.assertEqual(client.shutdowns, [socket.SHUT_WR])
        self.assertEqual(upstream.shutdowns, [socket.SHUT_WR])

    def test_does_not_acknowledge_a_failed_upstream_connection(self) -> None:
        request = b"CONNECT upstream.test:443 HTTP/1.1\r\n\r\n"
        client = SocketDouble(request)

        with patch(
            "lfault.server.socket.create_connection",
            side_effect=OSError("connection failed"),
        ):
            make_handler(client).handle()

        self.assertEqual(client.sent, [])

    def test_does_not_establish_an_incomplete_connect_request(self) -> None:
        client = SocketDouble(b"CONNECT upstream.test:443 HTTP/1.1\r\n", b"")

        with patch("lfault.server.socket.create_connection") as connect:
            make_handler(client).handle()

        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
