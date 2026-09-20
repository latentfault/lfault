from .server import listen


with listen() as listener:
    client, _ = listener.accept()
    client.close()
