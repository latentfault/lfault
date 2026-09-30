from urllib.parse import urlsplit


HEADER_SEPARATOR = b"\r\n\r\n"
LINE_SEPARATOR = b"\r\n"
FIELD_SEPARATOR = b" "


class RequestHead:
    def __init__(self, raw):
        request_line, _, _ = raw.partition(LINE_SEPARATOR)
        _, target, _ = request_line.split(FIELD_SEPARATOR)
        url = urlsplit(target)

        if url.scheme != b"http" or not url.hostname:
            raise ValueError("request target must be an absolute HTTP URL")

        port = url.port
        self.raw = raw
        self.upstream_address = (
            url.hostname,
            80 if port is None else port,
        )


def read_request_head(stream):
    data = bytearray()
    while line := stream.readline():
        data.extend(line)
        if data.endswith(HEADER_SEPARATOR):
            return RequestHead(bytes(data))
    raise EOFError("stream ended before a complete request head was received")
