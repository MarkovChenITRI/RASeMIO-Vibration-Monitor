"""Select an active local IPv4 adapter on the robot's subnet."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class AdapterAddress:
    name: str
    address: str
    netmask: str


def matching_adapter(target_ip: str) -> AdapterAddress:
    target = ipaddress.IPv4Address(target_ip)
    stats = psutil.net_if_stats()
    matches: list[tuple[int, AdapterAddress]] = []
    available: list[str] = []
    for name, addresses in psutil.net_if_addrs().items():
        if not stats.get(name) or not stats[name].isup:
            continue
        for item in addresses:
            if item.family != socket.AF_INET or not item.netmask or item.address.startswith("127."):
                continue
            available.append(f"{name}: {item.address}/{item.netmask}")
            try:
                network = ipaddress.IPv4Network((item.address, item.netmask), strict=False)
            except ValueError:
                continue
            if target in network:
                matches.append((network.prefixlen, AdapterAddress(name, item.address, item.netmask)))
    if not matches:
        detail = "；".join(available) if available else "沒有已啟用的 IPv4 網卡"
        raise OSError(f"找不到與 {target_ip} 位於同一子網的網卡（目前：{detail}）")
    return max(matches, key=lambda candidate: candidate[0])[1]
