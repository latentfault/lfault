import unittest
from unittest.mock import Mock

from lfault.server import ProxyRequestHandler


class RequestHeadReceivingTests(unittest.TestCase):
    def make_handler(self, *chunks: bytes) -> ProxyRequestHandler:
        handler = ProxyRequestHandler.__new__(ProxyRequestHandler)
        handler.request = Mock()
        handler.request.recv.side_effect = chunks
        handler.input_buffer = bytearray()
        return handler

    def test_reads_a_terminator_split_across_chunks(self) -> None:
        handler = self.make_handler(b"GET / HTTP/1.1\r\nHost: example\r\n\r", b"\n")
        request_head = handler._receive_request_head()
        self.assertEqual(
            request_head,
            b"GET / HTTP/1.1\r\nHost: example\r\n\r\n",
        )

    def test_retains_body_bytes_received_with_the_head(self) -> None:
        handler = self.make_handler(b"POST / HTTP/1.1\r\n\r\nbody")
        request_head = handler._receive_request_head()
        self.assertEqual(request_head, b"POST / HTTP/1.1\r\n\r\n")
        self.assertEqual(handler.input_buffer, b"body")

    def test_leaves_coalesced_request_head_bytes_buffered(self) -> None:
        first = b"GET /first HTTP/1.1\r\n\r\n"
        second = b"GET /second HTTP/1.1\r\n\r\n"
        handler = self.make_handler(first + second)
        self.assertEqual(handler._receive_request_head(), first)
        self.assertEqual(handler.input_buffer, second)
        handler.request.recv.assert_called_once()

    def test_reports_an_incomplete_request_head(self) -> None:
        handler = self.make_handler(b"GET / HTTP/1.1\r\n", b"")
        self.assertIsNone(handler._receive_request_head())
        self.assertEqual(handler.input_buffer, b"GET / HTTP/1.1\r\n")


if __name__ == "__main__":
    unittest.main()
