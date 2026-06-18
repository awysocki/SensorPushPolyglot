from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

import udi_interface
from udi_interface import Node

from sensorpushpolyglot.config import RuntimeConfig
from sensorpushpolyglot.sensorpush_api import SensorPushApiError, SensorPushClient

LOGGER = udi_interface.LOGGER


class SensorPushController(Node):
    id = "controller"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "GV0", "value": 0, "uom": 56},
        {"driver": "GV1", "value": 0, "uom": 56},
    ]

    commands = {
        "QUERY": "query",
    }

    def __init__(self, polyglot: Any) -> None:
        super().__init__(polyglot, "controller", "controller", "SensorPush Controller")
        self.poly = polyglot
        self._runtime_config = RuntimeConfig()
        self._client: SensorPushClient | None = None
        self._last_poll_utc: datetime | None = None
        self._reload_config()

    def start(self) -> None:
        LOGGER.info(
            "SensorPushController started. update_mode=%s shortPoll=60s longPoll=300s",
            "short" if self._runtime_config.use_short_poll_updates else "long",
        )

    def custom_params_changed(self, params: Dict[str, Any] | None = None) -> None:
        self._reload_config()
        LOGGER.info(
            "Custom params updated. update_mode=%s sample_limit=%s",
            "short" if self._runtime_config.use_short_poll_updates else "long",
            self._runtime_config.sample_limit,
        )

    def _get_custom_params(self) -> Dict[str, str]:
        config = getattr(self.poly, "polyConfig", None) or {}
        params = config.get("customParams", {})
        if not isinstance(params, dict):
            return {}
        return {str(k): str(v) for k, v in params.items()}

    def _reload_config(self) -> None:
        custom_params = self._get_custom_params()
        self._runtime_config = RuntimeConfig.from_sources(custom_params, os.environ)

        if self._runtime_config.email and self._runtime_config.password:
            self._client = SensorPushClient(
                email=self._runtime_config.email,
                password=self._runtime_config.password,
            )
        else:
            self._client = None
            LOGGER.warning(
                "SensorPush credentials not configured. Set sensorpush_email and "
                "sensorpush_password custom params (or environment variables)."
            )

    def _run_poll_cycle(self, reason: str) -> None:
        if not self._client:
            self.setDriver("ST", 0)
            return

        try:
            sensors_payload = self._client.list_sensors()
            sensors = sensors_payload if isinstance(sensors_payload, dict) else {}
            sensor_ids = list(sensors.keys())

            samples_payload = self._client.get_samples(
                sensor_ids=sensor_ids,
                limit=self._runtime_config.sample_limit,
            )
            sample_map = samples_payload.get("sensors", {}) if isinstance(samples_payload, dict) else {}

            total_samples = 0
            if isinstance(sample_map, dict):
                for _, entries in sample_map.items():
                    if isinstance(entries, list):
                        total_samples += len(entries)

            self.setDriver("ST", 1)
            self.setDriver("GV0", len(sensor_ids))
            self.setDriver("GV1", total_samples)
            self.reportDrivers()

            self._last_poll_utc = datetime.now(timezone.utc)
            LOGGER.info(
                "SensorPush %s update complete: sensors=%s samples=%s",
                reason,
                len(sensor_ids),
                total_samples,
            )
        except SensorPushApiError as err:
            self.setDriver("ST", 0)
            LOGGER.error("SensorPush API failure during %s poll: %s", reason, err)
        except Exception:
            self.setDriver("ST", 0)
            LOGGER.exception("Unexpected error during %s poll", reason)

    def shortPoll(self) -> None:
        if self._runtime_config.use_short_poll_updates:
            self._run_poll_cycle("shortPoll")

    def longPoll(self) -> None:
        if not self._runtime_config.use_short_poll_updates:
            self._run_poll_cycle("longPoll")

    def query(self, command: Dict[str, Any] | None = None) -> bool:
        self._run_poll_cycle("query")
        return True
