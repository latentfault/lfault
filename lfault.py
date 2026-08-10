#!/usr/bin/env python3
import socket

HOST = "127.0.0.1"
PORT = 8080
BUFFER_SIZE = 4096
HEADER_TERMINATOR = b"\r\n\r\n"
NOT_IMPLEMENTED_RESPONSE = (
    b"HTTP/1.1 501 Not Implemented\r\n"
    b"Content-Length: 0\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)


def receive_request(client: socket.socket) -> bytes:
    request = bytearray()

    while HEADER_TERMINATOR not in request:
        chunk = client.recv(BUFFER_SIZE)
        if not chunk:
            break

        request.extend(chunk)

    return bytes(request)


def handle_client(client: socket.socket, address: tuple[str, int]) -> None:
    print(f"[+] connection from {address[0]}:{address[1]}")

    request = receive_request(client)
    print("[+] incoming request:")
    print(request.decode("iso-8859-1").rstrip())

    client.sendall(NOT_IMPLEMENTED_RESPONSE)


def serve_once(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen()

        print(f"[+] lfault listening on {host}:{port}")

        client, address = server.accept()
        with client:
            handle_client(client, address)


def main() -> None:
    serve_once(HOST, PORT)


if __name__ == "__main__":
    main()
