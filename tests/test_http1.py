import unittest
from unittest.mock import Mock

from lfault import http1


class BufferedSocketTests(unittest.TestCase):
    def test_reads_a_split_boundary_and_retains_following_bytes(self) -> None:
        transport = Mock()
        transport.recv.side_effect = (b"head\r\n\r", b"\nfollowing")
        stream = http1.BufferedSocket(transport)

        self.assertEqual(stream.read_until(b"\r\n\r\n"), (b"head\r\n\r\n", True))
        self.assertEqual(stream.read(len(b"following")), b"following")


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
