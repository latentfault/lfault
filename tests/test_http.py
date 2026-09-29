import unittest

from lfault.http import parse_request_head


class ParseRequestHeadTest(unittest.TestCase):
    def test_parses_request_head(self):
        data = (
            b"POST /submit HTTP/1.1\r\n"
            b"Host: lfault.test\r\n"
            b"Content-Length: 6\r\n"
            b"\r\n"
            b"lfault"
        )

        method, target, version, headers, remainder = parse_request_head(data)

        self.assertEqual(method, b"POST")
        self.assertEqual(target, b"/submit")
        self.assertEqual(version, b"HTTP/1.1")
        self.assertEqual(headers, [b"Host: lfault.test", b"Content-Length: 6"])
        self.assertEqual(remainder, b"lfault")
