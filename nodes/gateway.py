from __future__ import annotations

from typing import Any

from udi_interface import Node


class SensorPushGatewayNode(Node):
    id = "sp_gateway"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
    ]

    commands = {}

    def __init__(self, polyglot: Any, address: str, name: str, primary: str) -> None:
        super().__init__(polyglot, primary, address, name)

    def set_status(self, online: bool, paired: bool) -> None:
        # ST values: 0=Disconnected, 1=Connected, 2=Not Paired.
        state = 2 if not paired else (1 if online else 0)
        self.setDriver("ST", state)
        self.reportDrivers()