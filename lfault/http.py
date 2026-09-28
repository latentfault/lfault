HEADER_SEPARATOR = b"\r\n\r\n"
LINE_SEPARATOR = b"\r\n"
FIELD_SEPARATOR = b" "


def parse_request_head(data):
    head, _, remainder = data.partition(HEADER_SEPARATOR)
    request_line, _, headers = head.partition(LINE_SEPARATOR)
    parts = request_line.split(FIELD_SEPARATOR)
    if len(parts) != 3:
        raise ValueError("request line must contain exactly three fields")
    method, target, version = parts
    return method, target, version, headers, remainder
