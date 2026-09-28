import socket

from .http import HEADER_SEPARATOR, parse_request_head

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
        try:
            parse_request_head(data)
        except ValueError:
            return
