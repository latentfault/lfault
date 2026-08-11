import unittest
from unittest.mock import Mock

from lfault.http1 import (
    BodyFraming,
    BodyKind,
    BufferedSocket,
    HTTP1LimitError,
    connection_is_persistent,
    request_body_framing,
    response_body_framing,
)


class BufferedSocketTests(unittest.TestCase):
    def test_reads_a_split_boundary_and_retains_following_bytes(self) -> None:
        transport = Mock()
        transport.recv.side_effect = (b"head\r\n\r", b"\nfollowing")
        stream = BufferedSocket(transport)

        self.assertEqual(
            stream.read_until(b"\r\n\r\n", 64),
            (b"head\r\n\r\n", True),
        )
        self.assertEqual(bytes(stream.buffer), b"following")

    def test_rejects_a_section_larger_than_its_limit(self) -> None:
        transport = Mock()
        transport.recv.side_effect = (b"five!",)
        stream = BufferedSocket(transport)

        with self.assertRaises(HTTP1LimitError):
            stream.read_until(b"\r\n", 4)

    def test_does_not_read_the_transport_again_after_eof(self) -> None:
        transport = Mock()
        transport.recv.return_value = b""
        stream = BufferedSocket(transport)

        self.assertEqual(stream.read(1), b"")
        self.assertEqual(stream.read(1), b"")
        transport.recv.assert_called_once_with(1)


class BodyFramingTests(unittest.TestCase):
    def test_recognizes_request_body_framing(self) -> None:
        cases = (
            (
                b"POST / HTTP/1.1\r\nContent-Length: 4\r\n\r\n",
                BodyFraming(BodyKind.LENGTH, 4),
            ),
            (
                b"POST / HTTP/1.1\r\nTransfer-Encoding: gzip, chunked\r\n\r\n",
                BodyFraming(BodyKind.CHUNKED),
            ),
            (
                b"POST / HTTP/1.1\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Content-Length: 4\r\n\r\n",
                BodyFraming(BodyKind.OPAQUE),
            ),
            (
                b"POST / HTTP/1.1\r\nContent-Length : 4\r\n\r\n",
                BodyFraming(BodyKind.OPAQUE),
            ),
        )
        for head, expected in cases:
            with self.subTest(head=head):
                self.assertEqual(request_body_framing(head), expected)

    def test_recognizes_response_body_framing(self) -> None:
        cases = (
            (
                b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n",
                b"GET",
                200,
                BodyFraming(BodyKind.LENGTH, 4),
            ),
            (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
                b"GET",
                200,
                BodyFraming(BodyKind.CHUNKED),
            ),
            (
                b"HTTP/1.1 200 OK\r\n\r\n",
                b"HEAD",
                200,
                BodyFraming(BodyKind.LENGTH, 0),
            ),
            (
                b"HTTP/1.1 200 OK\r\n\r\n",
                b"GET",
                200,
                BodyFraming(BodyKind.OPAQUE),
            ),
        )
        for head, method, status, expected in cases:
            with self.subTest(head=head, method=method):
                self.assertEqual(
                    response_body_framing(head, method, status),
                    expected,
                )

    def test_connection_persistence_respects_version_and_close(self) -> None:
        self.assertTrue(
            connection_is_persistent(b"GET / HTTP/1.1\r\n\r\n", b"HTTP/1.1")
        )
        self.assertFalse(
            connection_is_persistent(
                b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n",
                b"HTTP/1.1",
            )
        )
        self.assertTrue(
            connection_is_persistent(
                b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n",
                b"HTTP/1.0",
            )
        )


if __name__ == "__main__":
    unittest.main()
