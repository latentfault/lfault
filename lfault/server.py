import logging
import socketserver

logger = logging.getLogger(__name__)


class ProxyRequestHandler(socketserver.BaseRequestHandler):
    buffer_size = 4096
    header_terminator = b"\r\n\r\n"

    def setup(self) -> None:
        host, port = self.client_address
        self.logger = logging.LoggerAdapter(
            logger,
            {"context": f"client {host}:{port}"},
        )
        self.logger.info("connection opened")

    def handle(self) -> None:
        request = self._receive_request().decode("iso-8859-1").rstrip()
        self.logger.info("incoming request:\n%s", request)
        self._send_not_implemented()

    def _receive_request(self) -> bytes:
        request = bytearray()
        while self.header_terminator not in request:
            chunk = self.request.recv(self.buffer_size)
            if not chunk:
                break
            request.extend(chunk)
        return bytes(request)

    def _send_not_implemented(self) -> None:
        self.request.sendall(
            b"HTTP/1.1 501 Not Implemented\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )


class ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.logger = logging.LoggerAdapter(logger, {"context": "server"})
        super().__init__((host, port), ProxyRequestHandler)

    def run(self) -> None:
        host, port = self.server_address
        self.logger.info("listening on %s:%s", host, port)
        self.serve_forever()
