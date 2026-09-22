from threading import Thread

from .server import handle_client, listen


with listen() as listener:
    while True:
        client, _ = listener.accept()
        Thread(target=handle_client, args=(client,)).start()
