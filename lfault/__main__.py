from .server import listen


with listen() as listener:
    while True:
        client, _ = listener.accept()
        with client:
            chunk = client.recv(4096)
            print(repr(chunk))
            print("Header terminator found:", b"\r\n\r\n" in chunk)
