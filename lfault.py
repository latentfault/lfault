#!/usr/bin/env python3
import socketserver

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


class ProxyRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        address = self.client_address
        print(f"[+] connection from {address[0]}:{address[1]}")

        request = self._receive_request()
        print("[+] incoming request:")
        print(request.decode("iso-8859-1").rstrip())

        self.request.sendall(NOT_IMPLEMENTED_RESPONSE)

    def _receive_request(self) -> bytes:
        request = bytearray()

        while HEADER_TERMINATOR not in request:
            chunk = self.request.recv(BUFFER_SIZE)
            if not chunk:
                break

            request.extend(chunk)

        return bytes(request)


class ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int) -> None:
        super().__init__((host, port), ProxyRequestHandler)

    def run(self) -> None:
        host, port = self.server_address
        print(f"[+] lfault listening on {host}:{port}")
        self.serve_forever()


def main() -> None:
    try:
        with ThreadingProxyServer(HOST, PORT) as server:
            server.run()
    except KeyboardInterrupt:
        print("\n[+] lfault stopped")


if __name__ == "__main__":
    main()
