import unittest

from lfault.request import HeaderField, RequestHeadParseError, parse_request_head


class RequestHeadParsingTests(unittest.TestCase):
    def test_parses_without_losing_request_head_bytes(self) -> None:
        raw = b"GET / HTTP/1.1\r\nX: first\r\nX:second\r\n\r\n"
        request = parse_request_head(raw)
        self.assertEqual(request.raw, raw)
        self.assertEqual((request.method, request.target, request.version),
                         (b"GET", b"/", b"HTTP/1.1"))
        self.assertEqual(
            request.fields,
            (
                HeaderField(b"X", b" first"),
                HeaderField(b"X", b"second"),
            ),
        )

    def test_tokenizes_without_validating_http_syntax(self) -> None:
        raw = b"GE\tT / garbage\r\nHost : example.test\r\n\r\n"
        request = parse_request_head(raw)
        self.assertEqual(request.raw, raw)
        self.assertEqual(
            (request.method, request.target, request.version),
            (b"GE\tT", b"/", b"garbage"),
        )
        self.assertEqual(
            request.fields,
            (HeaderField(b"Host ", b" example.test"),),
        )

    def test_parses_a_head_without_fields(self) -> None:
        request = parse_request_head(b"GET / HTTP/1.1\r\n\r\n")
        self.assertEqual(request.fields, ())

    def test_preserves_colons_in_field_values(self) -> None:
        request = parse_request_head(b"GET / HTTP/1.1\r\nX:one:two\r\n\r\n")
        self.assertEqual(request.fields, (HeaderField(b"X", b"one:two"),))

    def test_rejects_unparseable_heads(self) -> None:
        for head in (
            b"GET / HTTP/1.1\r\n",
            b"GET /\r\n\r\n",
            b"GET  HTTP/1.1\r\n\r\n",
            b"GET / HTTP/1.1\r\nInvalid\r\n\r\n",
            b"GET / HTTP/1.1\r\n: value\r\n\r\n",
        ):
            with self.subTest(head=head):
                with self.assertRaises(RequestHeadParseError):
                    parse_request_head(head)
