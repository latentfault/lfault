import unittest

from lfault.http import RequestHead


class ParseRequestHeadTest(unittest.TestCase):
    def test_parses_request_head(self) -> None:
        data = (
            b"GET http://lfault.test:8080/ HTTP/1.1\r\n"
            b"Host: lfault.test:8080\r\n"
            b"\r\n"
        )

        request = RequestHead(data)

        self.assertEqual(request.raw, data)
        self.assertEqual(request.upstream_address, (b"lfault.test", 8080))
