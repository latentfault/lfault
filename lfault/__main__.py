import logging

from .server import ThreadingProxyServer


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(context)s] %(message)s",
    )
    with ThreadingProxyServer() as server:
        try:
            server.run()
        except KeyboardInterrupt:
            server.logger.info("stopped")


if __name__ == "__main__":
    main()
