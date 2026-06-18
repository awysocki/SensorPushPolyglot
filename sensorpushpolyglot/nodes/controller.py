from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

import udi_interface
from udi_interface import Node

from sensorpushpolyglot.config import RuntimeConfig
from sensorpushpolyglot.nodes.sensor import SensorPushSensorNode
from sensorpushpolyglot.sensorpush_api import SensorPushApiError, SensorPushClient

LOGGER = udi_interface.LOGGER


class SensorPushController(Node):
    id = "controller"
    SENSOR_ADDR_PREFIX = "sp_"

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
        self._typed_params_data: Dict[str, Any] = {}
        self._reload_config()

    @classmethod
    def _sensor_address(cls, sensor_id: str) -> str:
        digest = hashlib.md5(sensor_id.encode("utf-8")).hexdigest()[:10]
        return f"{cls.SENSOR_ADDR_PREFIX}{digest}"

    def _get_existing_nodes(self) -> Dict[str, Any]:
        nodes = getattr(self.poly, "nodes", None)
        if isinstance(nodes, dict):
            return nodes
        return {}

    def _get_node(self, address: str) -> Any | None:
        getter = getattr(self.poly, "getNode", None)
        if callable(getter):
            try:
                node = getter(address)
                if node is not None:
                    return node
            except Exception:
                pass
        return self._get_existing_nodes().get(address)

    def _delete_node(self, address: str) -> None:
        for method_name in ("delNode", "deleteNode"):
            deleter = getattr(self.poly, method_name, None)
            if callable(deleter):
                deleter(address)
                return
        raise RuntimeError("No node deletion method available on polyglot interface")

    def _coerce_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _sync_sensor_nodes(self, sensors: Dict[str, Any], sample_map: Dict[str, Any]) -> None:
        active_addresses: set[str] = set()

        for sensor_id, sensor_data in sensors.items():
            address = self._sensor_address(str(sensor_id))
            active_addresses.add(address)

            sensor_name = str(sensor_id)
            if isinstance(sensor_data, dict):
                sensor_name = str(sensor_data.get("name") or sensor_id)

            node = self._get_node(address)
            if not isinstance(node, SensorPushSensorNode):
                node = SensorPushSensorNode(self.poly, address=address, name=sensor_name, primary=self.address)
                self.poly.addNode(node)
                LOGGER.info("Created child sensor node: %s (%s)", sensor_name, address)

            latest_sample: Dict[str, Any] = {}
            samples = sample_map.get(sensor_id)
            if isinstance(samples, list) and samples:
                first = samples[0]
                if isinstance(first, dict):
                    latest_sample = first

            battery_v = None
            if isinstance(sensor_data, dict):
                battery_v = self._coerce_float(sensor_data.get("battery_voltage"))

            node.set_metrics(
                connected=True,
                temperature_f=self._coerce_float(latest_sample.get("temperature")),
                humidity_pct=self._coerce_float(latest_sample.get("humidity")),
                battery_v=battery_v,
            )

        existing_sensor_addresses = {
            address
            for address, _ in self._get_existing_nodes().items()
            if isinstance(address, str) and address.startswith(self.SENSOR_ADDR_PREFIX)
        }
        stale_addresses = sorted(existing_sensor_addresses - active_addresses)

        for address in stale_addresses:
            try:
                self._delete_node(address)
                LOGGER.info("Deleted stale child sensor node: %s", address)
            except Exception:
                LOGGER.exception("Failed deleting stale child sensor node: %s", address)

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

    def custom_typed_data_changed(self, params: Dict[str, Any] | None = None) -> None:
        if isinstance(params, dict):
            self._typed_params_data = dict(params)
        self._reload_config()
        LOGGER.info("Custom typed params updated from PG3 Admin form")

    def _get_custom_params(self) -> Dict[str, str]:
        config = getattr(self.poly, "polyConfig", None) or {}
        params = {}

        raw_custom = config.get("customParams", {})
        if isinstance(raw_custom, dict):
            params.update(raw_custom)

        for key in ("customtypedparams", "customTypedParams", "customTypedData", "customtypeddata"):
            typed = config.get(key, {})
            if isinstance(typed, dict):
                params.update(typed)

        if self._typed_params_data:
            params.update(self._typed_params_data)

        if not isinstance(params, dict):
            return {}
        normalized: Dict[str, str] = {}
        for k, v in params.items():
            if isinstance(v, list):
                normalized[str(k)] = str(v[0]) if v else ""
            else:
                normalized[str(k)] = str(v)
        return normalized

    def _reload_config(self) -> None:
        custom_params = self._get_custom_params()
        self._runtime_config = RuntimeConfig.from_sources(custom_params, os.environ)

        has_legacy_up = bool(self._runtime_config.email and self._runtime_config.password)

        if self._runtime_config.api_token:
            self._client = SensorPushClient(
                email=self._runtime_config.email,
                password=self._runtime_config.password,
                api_token=self._runtime_config.api_token,
            )
            LOGGER.info("Auth mode: API token (recommended/default)")
        elif self._runtime_config.allow_legacy_userpass and has_legacy_up:
            self._client = SensorPushClient(
                email=self._runtime_config.email,
                password=self._runtime_config.password,
                api_token="",
            )
            LOGGER.warning("Auth mode: legacy email/password fallback (explicitly enabled)")
        else:
            self._client = None
            LOGGER.warning(
                "SensorPush API token not configured. Set sensorpush_api_token. "
                "Legacy user/password fallback is disabled unless allow_legacy_userpass=true."
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
            if not isinstance(sample_map, dict):
                sample_map = {}

            self._sync_sensor_nodes(sensors=sensors, sample_map=sample_map)

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
