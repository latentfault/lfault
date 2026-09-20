import socket


def listen(port=8080):
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen()
    return listener
