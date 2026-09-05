import unittest
from unittest.mock import Mock

from lfault import http1, streams


class RequestRouteTests(unittest.TestCase):
    def test_routes_from_the_target_and_preserves_the_head(self) -> None:
        cases = (
            (b"GET", b"http://upstream.test/path?q=1", ("upstream.test", 80)),
            (b"POST", b"http://upstream.test:8080/", ("upstream.test", 8080)),
            (b"GET", b"http://[::1]:8080/", ("::1", 8080)),
            (b"CONNECT", b"upstream.test:443", ("upstream.test", 443)),
            (b"CONNECT", b"[::1]:443", ("::1", 443)),
        )
        for method, target, destination in cases:
            with self.subTest(method=method, target=target):
                head = (
                    method + b" " + target + b" HTTP/1.1\r\n"
                    b"Host: ignored.test\r\nMalformed field\r\n\r\n"
                )
                request = http1.parse_request_route(head)
                self.assertEqual(request.head, head)
                self.assertEqual(request.method, method)
                self.assertEqual(request.destination, destination)

    def test_rejects_unroutable_request_lines(self) -> None:
        for line in (
            b"GET /relative HTTP/1.1",
            b"GET https://upstream.test/ HTTP/1.1",
            b"GET  http://upstream.test/ HTTP/1.1",
            b"GET http://upstream.test/",
            b"GET http://\xff/ HTTP/1.1",
            b"CONNECT upstream.test HTTP/1.1",
            b"CONNECT upstream.test:443/path HTTP/1.1",
            b"CONNECT user@upstream.test:443 HTTP/1.1",
            b"CONNECT upstream.test:invalid HTTP/1.1",
            b"CONNECT [::1]:99999 HTTP/1.1",
        ):
            with self.subTest(line=line), self.assertRaises(ValueError):
                http1.parse_request_route(line + b"\r\n\r\n")


class ResponseStatusTests(unittest.TestCase):
    def test_parses_response_status(self) -> None:
        self.assertEqual(http1.response_status(b"HTTP/1.1 200 OK\r\n\r\n"), 200)
        self.assertIsNone(http1.response_status(b"not a response\r\n\r\n"))


class BodyFramingTests(unittest.TestCase):
    def test_request_with_whitespace_before_framing_colon_is_opaque(self) -> None:
        self.assertEqual(
            http1.request_body_framing(
                b"POST / HTTP/1.1\r\nContent-Length : 4\r\n\r\n"
            ),
            http1.BodyKind.OPAQUE,
        )

    def test_response_with_final_chunked_coding_is_chunked(self) -> None:
        self.assertEqual(
            http1.final_response_body_framing(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
                b"GET",
                200,
            ),
            http1.BodyKind.CHUNKED,
        )

    def test_head_response_has_no_body(self) -> None:
        self.assertEqual(
            http1.final_response_body_framing(
                b"HTTP/1.1 200 OK\r\n\r\n",
                b"HEAD",
                200,
            ),
            0,
        )

    def test_unframed_response_is_opaque(self) -> None:
        self.assertEqual(
            http1.final_response_body_framing(
                b"HTTP/1.1 200 OK\r\n\r\n",
                b"GET",
                200,
            ),
            http1.BodyKind.OPAQUE,
        )


class ChunkedRelayTests(unittest.TestCase):
    def test_preserves_chunk_bytes_and_stops_before_following_data(self) -> None:
        body = b"4;note=value\r\nbody\r\n0\r\nX-Trailer: value\r\n\r\n"
        transport = Mock()
        transport.recv.return_value = body + b"next"
        source = streams.BufferedSocket(transport)
        destination = Mock()

        self.assertTrue(http1.relay_body(source, destination, http1.BodyKind.CHUNKED))
        sent = b"".join(call.args[0] for call in destination.sendall.call_args_list)
        self.assertEqual(sent, body)
        self.assertEqual(source.read(4), b"next")

    def test_reports_failure_after_forwarding_invalid_or_partial_bytes(self) -> None:
        for body in (
            b"not-hex\r\n",
            b"4\r\nbo",
            b"4\r\nbodyXX",
            b"0\r\nX-Trailer: value\r\n",
        ):
            with self.subTest(body=body):
                transport = Mock()
                transport.recv.side_effect = (body, b"")
                source = streams.BufferedSocket(transport)
                destination = Mock()

                self.assertFalse(
                    http1.relay_body(source, destination, http1.BodyKind.CHUNKED)
                )
                sent = b"".join(
                    call.args[0] for call in destination.sendall.call_args_list
                )
                self.assertEqual(sent, body)
