import unittest
from unittest.mock import Mock

from lfault.http1 import (
    BodyKind,
    BufferedSocket,
    request_body_framing,
    response_body_framing,
)


class BufferedSocketTests(unittest.TestCase):
    def test_reads_a_split_boundary_and_retains_following_bytes(self) -> None:
        transport = Mock()
        transport.recv.side_effect = (b"head\r\n\r", b"\nfollowing")
        stream = BufferedSocket(transport)

        self.assertEqual(stream.read_until(b"\r\n\r\n"), (b"head\r\n\r\n", True))
        self.assertEqual(bytes(stream.buffer), b"following")


class BodyFramingTests(unittest.TestCase):
    def test_classifies_framing_not_exercised_end_to_end(self) -> None:
        self.assertEqual(
            request_body_framing(
                b"POST / HTTP/1.1\r\nContent-Length : 4\r\n\r\n"
            ),
            BodyKind.OPAQUE,
        )
        self.assertEqual(
            response_body_framing(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
                b"GET",
                200,
            ),
            BodyKind.CHUNKED,
        )
        self.assertEqual(
            response_body_framing(b"HTTP/1.1 200 OK\r\n\r\n", b"HEAD", 200),
            0,
        )
        self.assertEqual(
            response_body_framing(b"HTTP/1.1 200 OK\r\n\r\n", b"GET", 200),
            BodyKind.OPAQUE,
        )
