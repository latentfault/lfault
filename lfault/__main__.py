import logging

from .server import ProxyRequestHandler, ThreadingProxyServer

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
with ThreadingProxyServer(("127.0.0.1", 8080), ProxyRequestHandler) as server:
    logging.info("listening on %s:%s", *server.server_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
