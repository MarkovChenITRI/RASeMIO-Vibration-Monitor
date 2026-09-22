"""Read-only RASeMIO TCP probe for wafer robot status and digital inputs.

The 256-byte little-endian packet format follows docs/RASeMIO程式教學說明V2.pptx
and docs/RASeMIO_CommTestQt.zip.  This program only sends documented query
commands; it never initializes, moves, stops, or writes robot I/O.
"""

from __future__ import annotations

import argparse
import binascii
import json
import socket
import struct
import time
from dataclasses import asdict, dataclass
from datetime import datetime


PACKET = struct.Struct("<4s4sIII29dI")
READ_ONLY_COMMANDS = frozenset(
    {"GVER", "GION", "GINT", "GSID", "GPST", "GSTA", "GMST", "ERRC"}
)


@dataclass
class Message:
    marker: str
    command: str
    type: int
    mode: int
    error: int
    data: list[float]
    crc32: str


def build_request(command: str, type_: int = 0, mode: int = 0) -> bytes:
    if command not in READ_ONLY_COMMANDS:
        raise ValueError(f"Command {command!r} is not in the read-only allowlist")
    head = struct.pack("<4s4sIII29d", b"REQM", command.encode("ascii"), type_, mode, 0, *([0.0] * 29))
    crc = binascii.crc32(head) & 0xFFFFFFFF
    return head + struct.pack("<I", crc)


def receive_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError(f"Connection closed after {size - remaining}/{size} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def parse_response(packet: bytes, expected_command: str) -> Message:
    if len(packet) != PACKET.size:
        raise ValueError(f"Expected {PACKET.size} bytes, received {len(packet)}")
    expected_crc = binascii.crc32(packet[:-4]) & 0xFFFFFFFF
    fields = PACKET.unpack(packet)
    marker = fields[0].decode("ascii", errors="replace")
    command = fields[1].decode("ascii", errors="replace")
    received_crc = fields[-1]
    if received_crc != expected_crc:
        raise ValueError(f"CRC mismatch: received 0x{received_crc:08x}, expected 0x{expected_crc:08x}")
    if marker != "RSPM" or command != expected_command:
        raise ValueError(f"Unexpected response marker/command: {marker!r}/{command!r}")
    return Message(marker, command, fields[2], fields[3], fields[4], list(fields[5:34]), f"0x{received_crc:08x}")


class Client:
    def __init__(self, host: str, port: int, timeout: float, local_ip: str | None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.local_ip = local_ip
        self.sock: socket.socket | None = None

    def __enter__(self) -> "Client":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.local_ip:
            sock.bind((self.local_ip, 0))
        sock.connect((self.host, self.port))
        self.sock = sock
        return self

    def __exit__(self, *_: object) -> None:
        if self.sock:
            self.sock.close()

    def query(self, command: str, type_: int = 0, mode: int = 0) -> Message:
        assert self.sock is not None
        self.sock.sendall(build_request(command, type_, mode))
        return parse_response(receive_exact(self.sock, PACKET.size), command)


def read_all_inputs(client: Client, input_count: int) -> list[float]:
    values: list[float] = []
    for start in range(0, input_count, 29):
        count = min(29, input_count - start)
        response = client.query("GINT", start, count)
        if response.error:
            raise RuntimeError(f"GINT({start}, {count}) returned ERRC={response.error}")
        values.extend(response.data[:count])
    return values


def probe(host: str, args: argparse.Namespace) -> None:
    print(f"Connecting {args.local_ip or 'auto'} -> {host}:{args.port}")
    with Client(host, args.port, args.timeout, args.local_ip) as client:
        version = client.query("GVER")
        io_count = client.query("GION")
        selected_fork = client.query("GSID")
        pickup_status = client.query("GPST")
        print(f"  version={version.data[0]:g}.{version.data[1]:g}.{version.data[2]:g}, ERRC={version.error}")
        print(
            f"  inputs={io_count.type}, outputs={io_count.mode}, "
            f"selected_fork={selected_fork.mode}, pickup_status={pickup_status.mode}, "
            f"ERRC={io_count.error}/{pickup_status.error}"
        )
        if io_count.error or io_count.type <= 0:
            return

        previous: list[float] | None = None
        deadline = time.monotonic() + args.seconds
        while True:
            pickup_status = client.query("GPST")
            values = read_all_inputs(client, io_count.type)
            changed = [] if previous is None else [i for i, (a, b) in enumerate(zip(previous, values)) if a != b]
            record = {
                "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "host": host,
                "selected_fork": selected_fork.mode,
                "pickup_status": pickup_status.mode,
                "inputs": {str(i): value for i, value in enumerate(values)},
                "changed": changed,
            }
            print(json.dumps(record, ensure_ascii=False))
            previous = values
            if time.monotonic() >= deadline:
                break
            time.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="*", default=["192.168.10.11"])
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument(
        "--local-ip",
        default="",
        help="Optional local IPv4 address to bind; default lets Windows choose the route",
    )
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--seconds", type=float, default=5.0, help="Input observation duration per reachable host")
    parser.add_argument("--interval", type=float, default=0.2)
    args = parser.parse_args()
    args.local_ip = args.local_ip or None

    for host in args.hosts:
        try:
            probe(host, args)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
