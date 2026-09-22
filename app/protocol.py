"""Read-only RASeMIO TCP protocol implementation."""

from __future__ import annotations

import binascii
import socket
import struct
from dataclasses import dataclass


PACKET = struct.Struct("<4s4sIII29dI")
READ_ONLY_COMMANDS = frozenset({
    "GPST", "GMST", "GSID", "GSPD", "GACC", "GDEC", "GJNT", "GENC", "GPOS",
})


@dataclass(frozen=True)
class Response:
    command: str
    type: int
    mode: int
    error: int
    data: tuple[float, ...]


def build_request(command: str) -> bytes:
    command = command.upper()
    if command not in READ_ONLY_COMMANDS:
        raise ValueError(f"Command {command!r} is not in the read-only allowlist")
    head = struct.pack("<4s4sIII29d", b"REQM", command.encode("ascii"), 0, 0, 0, *([0.0] * 29))
    return head + struct.pack("<I", binascii.crc32(head) & 0xFFFFFFFF)


def receive_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise ConnectionError("RASeMIO closed the TCP connection")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def parse_response(packet: bytes, expected_command: str | None = None) -> Response:
    if len(packet) != PACKET.size:
        raise ValueError(f"Expected {PACKET.size} bytes, got {len(packet)}")
    if binascii.crc32(packet[:-4]) & 0xFFFFFFFF != struct.unpack_from("<I", packet, 252)[0]:
        raise ValueError("Response CRC32 mismatch")
    fields = PACKET.unpack(packet)
    marker = fields[0].decode("ascii", "replace")
    command = fields[1].decode("ascii", "replace")
    if marker != "RSPM" or command not in READ_ONLY_COMMANDS:
        raise ValueError(f"Unexpected response {marker!r}/{command!r}")
    if expected_command and command != expected_command.upper():
        raise ValueError(f"Expected {expected_command.upper()!r}, received {command!r}")
    return Response(command, fields[2], fields[3], fields[4], tuple(fields[5:34]))


class RASeMIOClient:
    def __init__(self, host: str, port: int, timeout: float, local_ip: str = "") -> None:
        self.host, self.port, self.timeout, self.local_ip = host, port, timeout, local_ip
        self.socket: socket.socket | None = None

    def connect(self) -> None:
        self.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.local_ip.strip():
            sock.bind((self.local_ip.strip(), 0))
        sock.connect((self.host, self.port))
        self.socket = sock

    def query(self, command: str = "GPST") -> Response:
        if self.socket is None:
            raise ConnectionError("Not connected")
        command = command.upper()
        self.socket.sendall(build_request(command))
        response = parse_response(receive_exact(self.socket, PACKET.size), command)
        if response.error:
            raise RuntimeError(f"{command} returned ERRC={response.error}")
        return response

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None


# Backward-compatible name for code that imported the original GPST-only client.
GPSTClient = RASeMIOClient
