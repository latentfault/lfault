import socket

from .http import Request

CHUNK_SIZE = 4096


def listen(port: int = 8080) -> socket.socket:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    return listener


def handle_client(client: socket.socket) -> None:
    with client, client.makefile("rb") as stream:
        try:
            request = Request.from_stream(stream)
        except (EOFError, ValueError):
            return

        host, port = request.upstream_address
        with socket.create_connection((host.decode("ascii"), port)) as upstream:
            upstream.sendall(request.raw_head)
            upstream.sendall(request.raw_body)
            # Until we parse response framing, the upstream must close to end this loop.
            while chunk := upstream.recv(CHUNK_SIZE):
                client.sendall(chunk)
