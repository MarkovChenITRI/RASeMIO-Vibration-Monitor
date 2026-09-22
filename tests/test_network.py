import socket
import unittest
from collections import namedtuple
from unittest.mock import patch

from app.network import matching_adapter


Address = namedtuple("Address", "family address netmask broadcast ptp")
Stat = namedtuple("Stat", "isup")


class NetworkSelectionTests(unittest.TestCase):
    @patch("app.network.psutil.net_if_stats")
    @patch("app.network.psutil.net_if_addrs")
    def test_selects_active_matching_adapter_with_most_specific_mask(self, addrs, stats):
        addrs.return_value = {
            "plant": [Address(socket.AF_INET, "192.168.0.5", "255.255.0.0", None, None)],
            "robot": [Address(socket.AF_INET, "192.168.10.20", "255.255.255.0", None, None)],
            "disabled": [Address(socket.AF_INET, "192.168.10.30", "255.255.255.0", None, None)],
        }
        stats.return_value = {"plant": Stat(True), "robot": Stat(True), "disabled": Stat(False)}
        selected = matching_adapter("192.168.10.11")
        self.assertEqual((selected.name, selected.address, selected.netmask),
                         ("robot", "192.168.10.20", "255.255.255.0"))

    @patch("app.network.psutil.net_if_stats", return_value={"lan": Stat(True)})
    @patch("app.network.psutil.net_if_addrs", return_value={
        "lan": [Address(socket.AF_INET, "10.0.0.5", "255.255.255.0", None, None)]})
    def test_reports_when_no_active_adapter_is_on_target_subnet(self, _addrs, _stats):
        with self.assertRaisesRegex(OSError, "同一子網"):
            matching_adapter("192.168.10.11")


if __name__ == "__main__":
    unittest.main()
