from argparse import ArgumentParser
from threading import Thread

from .server import handle_client, listen


def main() -> None:
    parser = ArgumentParser(description="An HTTP proxy with boundary issues.")
    parser.add_argument(
        "--port", type=int, default=8080,
        help="listening port (default: 8080; 0 selects an available port)",
    )
    args = parser.parse_args()

    with listen(args.port) as listener:
        host, port = listener.getsockname()
        print(f"Listening on {host}:{port}", flush=True)
        while True:
            client, _ = listener.accept()
            Thread(target=handle_client, args=(client,)).start()


if __name__ == "__main__":
    main()
