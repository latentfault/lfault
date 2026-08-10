import logging
import socketserver

from .request import REQUEST_HEAD_TERMINATOR, RequestHeadParseError, parse_request_head

logger = logging.getLogger(__name__)


class ProxyRequestHandler(socketserver.BaseRequestHandler):
    buffer_size = 4096

    def setup(self) -> None:
        host, port = self.client_address
        self.logger = logging.LoggerAdapter(
            logger,
            {"context": f"client {host}:{port}"},
        )
        self.input_buffer = bytearray()
        self.logger.info("connection opened")

    def handle(self) -> None:
        request_head = self._receive_request_head()
        if request_head is None:
            self.logger.warning("connection closed before request head completed")
            return
        try:
            request = parse_request_head(request_head)
        except RequestHeadParseError as error:
            self.logger.warning("could not parse request head: %s", error)
            return
        request_text = request.raw.decode("iso-8859-1").rstrip()
        self.logger.info("incoming request:\n%s", request_text)
        self._send_not_implemented()

    def _receive_request_head(self) -> bytes | None:
        while (boundary := self.input_buffer.find(REQUEST_HEAD_TERMINATOR)) < 0:
            chunk = self.request.recv(self.buffer_size)
            if not chunk:
                return None
            self.input_buffer.extend(chunk)
        end = boundary + len(REQUEST_HEAD_TERMINATOR)
        request_head = bytes(self.input_buffer[:end])
        del self.input_buffer[:end]
        return request_head

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
