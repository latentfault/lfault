import socket
import threading
import unittest
from contextlib import ExitStack, suppress
from unittest.mock import Mock

from lfault import streams


class BufferedSocketTests(unittest.TestCase):
    def test_reads_a_split_boundary_and_retains_following_bytes(self) -> None:
        transport = Mock()
        transport.recv.side_effect = (b"head\r\n\r", b"\nfollowing")
        stream = streams.BufferedSocket(transport)

        self.assertEqual(stream.read_until(b"\r\n\r\n"), (b"head\r\n\r\n", True))
        self.assertEqual(stream.read(len(b"following")), b"following")

    def test_read_exactly_combines_buffered_and_new_bytes(self) -> None:
        transport = Mock()
        transport.recv.side_effect = (b"head|ab", b"cd")
        stream = streams.BufferedSocket(transport)

        self.assertEqual(stream.read_until(b"|"), (b"head|", True))
        self.assertEqual(stream.read_exactly(4), (b"abcd", True))

    def test_returns_partial_data_at_eof(self) -> None:
        for operation, argument in (("read_until", b"|"), ("read_exactly", 5)):
            with self.subTest(operation=operation):
                transport = Mock()
                transport.recv.side_effect = (b"abc", b"")
                stream = streams.BufferedSocket(transport)

                self.assertEqual(getattr(stream, operation)(argument), (b"abc", False))
                self.assertEqual(stream.drain_buffer(), b"")


class RelayTests(unittest.TestCase):
    def test_exact_relay_leaves_following_bytes_unread(self) -> None:
        transport = Mock()
        transport.recv.return_value = b"head|bodyfollowing"
        source = streams.BufferedSocket(transport)
        source.read_until(b"|")
        destination = Mock()

        self.assertTrue(streams.relay_exactly(source, destination, 4))
        destination.sendall.assert_called_once_with(b"body")
        self.assertEqual(source.read(9), b"following")

    def test_exact_relay_reports_eof_after_forwarding_partial_data(self) -> None:
        transport = Mock()
        transport.recv.side_effect = (b"ab", b"")
        source = streams.BufferedSocket(transport)
        destination = Mock()

        self.assertFalse(streams.relay_exactly(source, destination, 4))
        destination.sendall.assert_called_once_with(b"ab")

    def test_eof_relay_sends_buffered_bytes_first(self) -> None:
        transport = Mock()
        transport.recv.side_effect = (b"head|buffered", b"new", b"")
        source = streams.BufferedSocket(transport)
        source.read_until(b"|")
        destination = Mock()

        streams.relay_to_eof(source, destination)

        self.assertEqual(
            [call.args[0] for call in destination.sendall.call_args_list],
            [b"buffered", b"new"],
        )


def receive_exactly(transport: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = transport.recv(size - len(data))
        if not chunk:
            raise AssertionError("unexpected EOF")
        data.extend(chunk)
    return bytes(data)


class BidirectionalRelayTests(unittest.TestCase):
    def test_flushes_buffers_and_keeps_reverse_traffic_after_eof(self) -> None:
        with ExitStack() as stack:
            left_peer, left_socket = socket.socketpair()
            for transport in (left_peer, left_socket):
                stack.enter_context(transport)
                transport.settimeout(2)
            right_peer, right_socket = socket.socketpair()
            for transport in (right_peer, right_socket):
                stack.enter_context(transport)
                transport.settimeout(2)

            left = streams.BufferedSocket(left_socket)
            right = streams.BufferedSocket(right_socket)
            left_peer.sendall(b"head|left buffer")
            right_peer.sendall(b"head|right buffer")
            self.assertEqual(left.read_until(b"|"), (b"head|", True))
            self.assertEqual(right.read_until(b"|"), (b"head|", True))

            errors = []

            def relay() -> None:
                try:
                    streams.relay_bidirectionally(left, right)
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=relay, daemon=True)
            worker.start()
            try:
                self.assertEqual(receive_exactly(right_peer, 11), b"left buffer")
                self.assertEqual(receive_exactly(left_peer, 12), b"right buffer")

                left_peer.sendall(b"forward")
                self.assertEqual(receive_exactly(right_peer, 7), b"forward")
                left_peer.shutdown(socket.SHUT_WR)
                self.assertEqual(right_peer.recv(1), b"")

                right_peer.sendall(b"reverse after EOF")
                self.assertEqual(receive_exactly(left_peer, 17), b"reverse after EOF")
                right_peer.shutdown(socket.SHUT_WR)
                self.assertEqual(left_peer.recv(1), b"")
            finally:
                for peer in (left_peer, right_peer):
                    with suppress(OSError):
                        peer.shutdown(socket.SHUT_RDWR)
                worker.join(timeout=3)

            self.assertFalse(worker.is_alive(), "relay did not finish after both EOFs")
            self.assertEqual(errors, [])
            self.assertNotEqual(left_socket.fileno(), -1)
            self.assertNotEqual(right_socket.fileno(), -1)
