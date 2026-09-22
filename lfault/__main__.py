from .server import listen


def handle_client(client):
    with client:
        chunk = client.recv(4096)
        print(repr(chunk))
        print("Header terminator found:", b"\r\n\r\n" in chunk)


with listen() as listener:
    while True:
        client, _ = listener.accept()
        handle_client(client)
