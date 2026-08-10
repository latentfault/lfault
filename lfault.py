#!/usr/bin/env python3
import socket

HOST = "127.0.0.1"
PORT = 8080
BUFFER_SIZE = 4096

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()

    print(f"[+] lfault listening on {HOST}:{PORT}")

    client, address = server.accept()

    with client:
        print(f"[+] connection from {address[0]}:{address[1]}")

        request = b""

        while b"\r\n\r\n" not in request:
            chunk = client.recv(BUFFER_SIZE)

            if not chunk:
                break

            request += chunk

        print("[+] incoming request:")
        print(request.decode("iso-8859-1").rstrip())

        client.sendall(
            b"HTTP/1.1 501 Not Implemented\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
