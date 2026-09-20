from .server import listen


with listen() as listener:
    while True:
        client, _ = listener.accept()
        client.close()
