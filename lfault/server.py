import socket

HEADER_SEPARATOR = b"\r\n\r\n"
LINE_SEPARATOR = b"\r\n"
FIELD_SEPARATOR = b" "
CHUNK_SIZE = 4096


def listen(port=8080):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    return listener


def handle_client(client):
    with client:
        data = bytes()
        while HEADER_SEPARATOR not in data:
            if not (chunk := client.recv(CHUNK_SIZE)):
                return
            data += chunk
        head, _, remainder = data.partition(HEADER_SEPARATOR)
        request_line, _, headers = head.partition(LINE_SEPARATOR)
        parts = request_line.split(FIELD_SEPARATOR)
        if len(parts) != 3:
            return
        method, target, version = parts
        print("Method:", repr(method))
        print("Target:", repr(target))
        print("Version:", repr(version))
        print("Headers:", repr(headers))
        print("Already received after headers:", repr(remainder))
