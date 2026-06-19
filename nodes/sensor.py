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
        {"driver": "GV3", "value": 0, "uom": 0},
        {"driver": "GV4", "value": 0, "uom": 0},
        {"driver": "GV5", "value": 0, "uom": 0},
        {"driver": "GV6", "value": 0, "uom": 25},
    ]

    commands = {}

    def __init__(self, polyglot: Any, address: str, name: str, primary: str) -> None:
        super().__init__(polyglot, primary, address, name)

    def set_metrics(
        self,
        *,
        connected: bool,
        temperature_f: float | None,
        humidity_pct: float | None,
        battery_v: float | None,
        barometric_pressure: float | None = None,
        dew_point_f: float | None = None,
        vpd: float | None = None,
        sensor_type_index: int | None = None,
    ) -> None:
        self.setDriver("ST", 1 if connected else 0)

        if temperature_f is not None:
            self.setDriver("GV0", round(float(temperature_f), 1))
        if humidity_pct is not None:
            self.setDriver("GV1", round(float(humidity_pct), 1))
        if battery_v is not None:
            self.setDriver("GV2", round(float(battery_v), 3))
        if barometric_pressure is not None:
            self.setDriver("GV3", round(float(barometric_pressure), 3))
        if dew_point_f is not None:
            self.setDriver("GV4", round(float(dew_point_f), 1))
        if vpd is not None:
            self.setDriver("GV5", round(float(vpd), 3))
        if sensor_type_index is not None:
            self.setDriver("GV6", int(sensor_type_index))

        self.reportDrivers()
