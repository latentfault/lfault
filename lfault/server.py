import socket


def listen(port=8080):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    return listener


def handle_client(client):
    with client:
        chunk = client.recv(4096)
        print(repr(chunk))
        print("Header terminator found:", b"\r\n\r\n" in chunk)
