"""Non-blocking AAP connection driven by the GLib main loop."""

import errno
import socket

from gi.repository import GLib

import aap

ERROR = GLib.IOCondition.HUP | GLib.IOCondition.ERR


class Session:
    def __init__(self, address, on_packet, on_closed):
        self.address = address
        self.on_packet = on_packet
        self.on_closed = on_closed
        self.sock = None
        self.watch = None
        self.ready = False

    def open(self):
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        self.sock.setblocking(False)
        err = self.sock.connect_ex((self.address, aap.PSM))
        if err not in (0, errno.EINPROGRESS, errno.EAGAIN):
            self.close()
            return
        self._watch(GLib.IOCondition.OUT, self._connected)

    def _watch(self, cond, callback):
        self.watch = GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT, cond | ERROR, callback)

    def _connected(self, _fd, cond):
        self.watch = None
        if cond & ERROR or self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
            self.close()
            return False
        self.send(aap.HANDSHAKE)
        if self.sock:
            self._watch(GLib.IOCondition.IN, self._readable)
        return False

    def _readable(self, _fd, cond):
        while self.sock:
            try:
                packet = self.sock.recv(4096)
            except BlockingIOError:
                break
            except OSError:
                packet = b''
            if not packet:
                self.watch = None
                self.close()
                return False
            self._handle(packet)
        if not self.sock or cond & ERROR:
            self.watch = None
            self.close()
            return False
        return True

    def _handle(self, packet):
        parsed = aap.parse(packet)
        if not parsed:
            return
        kind, payload = parsed
        if kind == 'handshake':
            self.send(aap.SET_FEATURES)
        elif kind == 'features':
            self.send(aap.REQUEST_NOTIFICATIONS)
            self.send(aap.REQUEST_PROXIMITY_KEYS)
            self.ready = True
            self.on_packet('ready', None)
        else:
            self.on_packet(kind, payload)

    def send(self, packet):
        if self.sock:
            try:
                self.sock.send(packet)
            except OSError:
                self.close()

    def close(self):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = None
        if self.sock:
            self.sock.close()
            self.sock = None
            self.ready = False
            self.on_closed()
