from __future__ import annotations

import binascii
import struct
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app.data import SessionRecorder, align_status_to_sensor, infer_sensor_start, load_status_csv
from app.protocol import PACKET, build_request, parse_response


class ProtocolTests(unittest.TestCase):
    def test_request_is_valid_256_byte_packet(self) -> None:
        packet = build_request("GPST")
        self.assertEqual(len(packet), 256)
        self.assertEqual(packet[:8], b"REQMGPST")
        self.assertEqual(struct.unpack_from("<I", packet, 252)[0], binascii.crc32(packet[:-4]) & 0xFFFFFFFF)

    def test_response_mode_is_parsed(self) -> None:
        head = struct.pack("<4s4sIII29d", b"RSPM", b"GPST", 0, 2, 0, *([0.0] * 29))
        response = parse_response(head + struct.pack("<I", binascii.crc32(head) & 0xFFFFFFFF))
        self.assertEqual(response.mode, 2)

    def test_all_monitoring_commands_are_allowed_but_write_commands_are_rejected(self) -> None:
        for command in ("GPST", "GMST", "GSID", "GSPD", "GACC", "GDEC", "GJNT", "GENC", "GPOS"):
            self.assertEqual(build_request(command)[4:8], command.encode("ascii"))
        with self.assertRaises(ValueError):
            build_request("ACCT")

    def test_response_command_must_match_request(self) -> None:
        head = struct.pack("<4s4sIII29d", b"RSPM", b"GMST", 0, 1, 0, *([0.0] * 29))
        packet = head + struct.pack("<I", binascii.crc32(head) & 0xFFFFFFFF)
        self.assertEqual(parse_response(packet, "GMST").mode, 1)
        with self.assertRaises(ValueError):
            parse_response(packet, "GPST")


class RecorderTests(unittest.TestCase):
    def test_old_sensor_csv_start_falls_back_to_recdata_filename(self) -> None:
        result = infer_sensor_start(Path("RecData--20260603153648.csv"), {})
        self.assertIsNotNone(result)
        self.assertEqual(result.strftime("%Y-%m-%d %H:%M:%S"), "2026-06-03 15:36:48")

    def test_sensor_metadata_start_takes_priority_over_filename(self) -> None:
        result = infer_sensor_start(
            Path("RecData--20260603153648.csv"), {"Date/Time": "2026/06/03 15:38:29"}
        )
        self.assertEqual(result.strftime("%Y-%m-%d %H:%M:%S"), "2026-06-03 15:38:29")

    def test_recording_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            recorder = SessionRecorder(Path(folder), {"host": "127.0.0.1"})
            recorder.append({"timestamp": "2026-01-01T00:00:00+08:00", "elapsed_s": 0.1,
                             "pickup_mode": 1, "error": 0, "latency_ms": 1.2, "host": "127.0.0.1", "port": 4000})
            recorder.mark("2026-01-01T00:00:00.100+08:00", 0.1, "test event")
            path = recorder.csv_path; recorder.close()
            result = load_status_csv(path)
            self.assertEqual(int(result.pickup_mode.iloc[0]), 1)
            self.assertIn("test event", recorder.events_path.read_text(encoding="utf-8-sig"))

    def test_align_status_to_sensor_uses_previous_state(self) -> None:
        sensor = pd.DataFrame({"time_s": [0.0, 0.5, 1.0], "X": [0, 0, 0], "Y": [0, 0, 0], "Z": [0, 0, 0]})
        status = pd.DataFrame({"elapsed_s": [0.0, 0.8], "pickup_mode": [0, 1]})
        result = align_status_to_sensor(sensor, status, 0.0)
        self.assertEqual(result.pickup_mode.tolist(), [0, 0, 1])


if __name__ == "__main__":
    unittest.main()
