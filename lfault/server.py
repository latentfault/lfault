import socket

from .http import read_request_head

CHUNK_SIZE = 4096


def listen(port=8080):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    return listener


def handle_client(client):
    with client, client.makefile("rb") as stream:
        try:
            request = read_request_head(stream)
        except (EOFError, ValueError):
            return

        # The current forwarding step handles one bodyless request.
        with socket.create_connection(request.upstream_address) as upstream:
            upstream.sendall(request.raw)
            # Until we parse response framing, the upstream must close to end this loop.
            while chunk := upstream.recv(CHUNK_SIZE):
                client.sendall(chunk)
