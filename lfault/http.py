from io import BufferedReader
from urllib.parse import urlsplit


HEADER_SEPARATOR = b"\r\n\r\n"
LINE_SEPARATOR = b"\r\n"
FIELD_SEPARATOR = b" "


class Request:
    def __init__(self, raw_head: bytes, raw_body: bytes = bytes()) -> None:
        request_line, _, header_lines = raw_head.partition(LINE_SEPARATOR)
        _, target, _ = request_line.split(FIELD_SEPARATOR)
        url = urlsplit(target)

        if url.scheme != b"http" or not url.hostname:
            raise ValueError("request target must be an absolute HTTP URL")

        self.raw_head = raw_head
        self.raw_body = raw_body
        self.upstream_address = (
            url.hostname,
            80 if url.port is None else url.port,
        )
        self.content_length = 0
        for line in header_lines.split(LINE_SEPARATOR):
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                self.content_length = int(value)
        if self.content_length < 0:
            raise ValueError("Content-Length must not be negative")

    @classmethod
    def from_stream(cls, stream: BufferedReader) -> Request:
        data = bytearray()
        while line := stream.readline():
            data.extend(line)
            if data.endswith(HEADER_SEPARATOR):
                break
        else:
            raise EOFError("stream ended before a complete request head was received")

        request = cls(bytes(data))
        request.raw_body = stream.read(request.content_length)
        if len(request.raw_body) != request.content_length:
            raise EOFError("stream ended before the complete request body was received")
        return request
