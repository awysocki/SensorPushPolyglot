from __future__ import annotations

from typing import Any

from udi_interface import Node


class SensorPushSensorNode(Node):
    id = "sensor"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "GV0", "value": 0, "uom": 17},
        {"driver": "GV1", "value": 0, "uom": 22},
        {"driver": "GV2", "value": 0, "uom": 72},
    ]

    commands = {}

    def __init__(self, polyglot: Any, address: str, name: str, primary: str) -> None:
        super().__init__(polyglot, primary, address, name)

    def set_metrics(self, *, connected: bool, temperature_f: float | None, humidity_pct: float | None, battery_v: float | None) -> None:
        self.setDriver("ST", 1 if connected else 0)

        if temperature_f is not None:
            self.setDriver("GV0", round(float(temperature_f), 1))
        if humidity_pct is not None:
            self.setDriver("GV1", round(float(humidity_pct), 1))
        if battery_v is not None:
            self.setDriver("GV2", round(float(battery_v), 3))

        self.reportDrivers()
