import unittest

from lfault.http import Request


class ParseRequestTest(unittest.TestCase):
    def test_parses_request(self) -> None:
        head = (
            b"GET http://lfault.test:8080/ HTTP/1.1\r\n"
            b"Host: lfault.test:8080\r\n"
            b"Content-Length: 6\r\n"
            b"\r\n"
        )
        body = b"lfault"

        request = Request(head, body)

        self.assertEqual(request.raw_head, head)
        self.assertEqual(request.raw_body, body)
        self.assertEqual(request.upstream_address, (b"lfault.test", 8080))
        self.assertEqual(request.content_length, 6)
