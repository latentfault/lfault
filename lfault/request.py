from dataclasses import dataclass

from .http1 import HEAD_TERMINATOR


class RequestHeadParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HeaderField:
    name: bytes
    value: bytes


@dataclass(frozen=True, slots=True)
class RequestHead:
    raw: bytes
    method: bytes
    target: bytes
    version: bytes
    fields: tuple[HeaderField, ...]


def parse_request_head(raw: bytes) -> RequestHead:
    """Extract a byte-preserving structure without validating HTTP syntax."""
    if not raw.endswith(HEAD_TERMINATOR):
        raise RequestHeadParseError("request head is incomplete")
    head = raw.removesuffix(HEAD_TERMINATOR)
    request_line, *field_lines = head.split(b"\r\n")
    method, target, version = _parse_request_line(request_line)
    fields = tuple(_parse_field_line(line) for line in field_lines)
    return RequestHead(raw, method, target, version, fields)


def _parse_request_line(line: bytes) -> tuple[bytes, bytes, bytes]:
    parts = line.split(b" ")
    if len(parts) != 3 or not all(parts):
        raise RequestHeadParseError("request line cannot be split")
    method, target, version = parts
    return method, target, version


def _parse_field_line(line: bytes) -> HeaderField:
    name, colon, value = line.partition(b":")
    if not name or not colon:
        raise RequestHeadParseError("header field cannot be split")
    return HeaderField(name, value)
