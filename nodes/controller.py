from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict

import udi_interface
from udi_interface import Node

from config import RuntimeConfig
from nodes.sensor import SensorPushSensorNode
from sensorpush_api import SensorPushApiError, SensorPushClient

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
        self._server_version = self._load_server_version()
        self._runtime_config = RuntimeConfig()
        self._client: SensorPushClient | None = None
        self._last_poll_utc: datetime | None = None
        self._last_config_refresh_utc: datetime | None = None
        self._poll_cycle_seq: int = 0
        self._missing_token_warned: bool = False
        self._initial_discovery_completed: bool = False
        self._typed_params_data: Dict[str, Any] = {}
        self._reload_config()

    def _run_config_refresh_once(self, source: str) -> None:
        now = datetime.now(timezone.utc)
        if self._last_config_refresh_utc and (now - self._last_config_refresh_utc).total_seconds() < 2:
            LOGGER.debug(
                "Skipping duplicate config refresh from %s (version=%s)",
                source,
                self._server_version,
            )
            return
        self._last_config_refresh_utc = now
        self._run_poll_cycle("config_update", discover_nodes=True)

    @staticmethod
    def _load_server_version() -> str:
        try:
            server_path = Path(__file__).resolve().parent.parent / "server.json"
            data = json.loads(server_path.read_text(encoding="utf-8"))
            version = data.get("version")
            if isinstance(version, str) and version.strip():
                return version.strip()
        except Exception:
            pass
        return "unknown"

    @classmethod
    def _sensor_address(cls, sensor_id: str) -> str:
        digest = hashlib.md5(sensor_id.encode("utf-8")).hexdigest()[:10]
        return f"{cls.SENSOR_ADDR_PREFIX}{digest}"

    @staticmethod
    def _mask(value: str, keep_start: int = 3, keep_end: int = 2) -> str:
        text = str(value or "")
        if not text:
            return "<empty>"
        if len(text) <= keep_start + keep_end:
            return "*" * len(text)
        return f"{text[:keep_start]}***{text[-keep_end:]}"

    @staticmethod
    def _describe_sensor(sensor_id: Any, sensor_data: Any) -> str:
        if isinstance(sensor_data, dict):
            name = str(sensor_data.get("name") or sensor_id)
        else:
            name = str(sensor_id)
        return f"{sensor_id}:{name}"

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

    @staticmethod
    def _as_bool(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _json_preview(payload: Any, limit: int = 8000) -> str:
        try:
            text = json.dumps(payload, sort_keys=True, default=str)
        except Exception:
            text = repr(payload)
        if len(text) > limit:
            return f"{text[:limit]}...<truncated>"
        return text

    def _is_moredebug_enabled(self) -> bool:
        params = self._get_custom_params()
        params_ci = {str(k).strip().lower(): v for k, v in params.items()}
        raw = params_ci.get("moredebug") or params_ci.get("sensorpush_moredebug")
        return self._as_bool(raw)

    def _extract_float(self, payload: Any, keys: tuple[str, ...]) -> float | None:
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                number = self._coerce_float(value)
                if number is not None:
                    return number
            for value in payload.values():
                number = self._extract_float(value, keys)
                if number is not None:
                    return number
        elif isinstance(payload, list):
            for value in payload:
                number = self._extract_float(value, keys)
                if number is not None:
                    return number
        return None

    def _sync_sensor_nodes(
        self,
        sensors: Dict[str, Any],
        sample_map: Dict[str, Any],
        discover_nodes: bool,
        reason: str,
    ) -> None:
        active_addresses: set[str] = set()
        attempted_updates: list[str] = []
        skipped_updates: list[str] = []

        for sensor_id, sensor_data in sensors.items():
            address = self._sensor_address(str(sensor_id))
            active_addresses.add(address)

            sensor_name = str(sensor_id)
            if isinstance(sensor_data, dict):
                sensor_name = str(sensor_data.get("name") or sensor_id)

            node = self._get_node(address)
            if not isinstance(node, SensorPushSensorNode):
                if discover_nodes:
                    node = SensorPushSensorNode(self.poly, address=address, name=sensor_name, primary=self.address)
                    self.poly.addNode(node)
                    LOGGER.debug("Created child sensor node: %s (%s)", sensor_name, address)
                else:
                    skipped_updates.append(f"{sensor_name} ({address})")
                    LOGGER.debug(
                        "Skipping new sensor during update-only poll: %s (%s)",
                        sensor_name,
                        address,
                    )
                    continue

            latest_sample: Dict[str, Any] = {}
            samples = sample_map.get(sensor_id)
            if isinstance(samples, list) and samples:
                first = samples[0]
                if isinstance(first, dict):
                    latest_sample = first

            battery_v = None
            sensor_type = None
            if isinstance(sensor_data, dict):
                battery_v = self._coerce_float(sensor_data.get("battery_voltage"))
                sensor_type = str(
                    sensor_data.get("device_type")
                    or sensor_data.get("sensor_type")
                    or sensor_data.get("type")
                    or sensor_data.get("model")
                    or ""
                ).strip() or None

            temp_f = self._coerce_float(latest_sample.get("temperature"))
            humidity_pct = self._coerce_float(latest_sample.get("humidity"))
            metric_sources = [latest_sample, sensor_data]
            barometric_pressure = None
            dew_point_f = None
            vpd = None
            heat_index_f = None
            for source in metric_sources:
                if barometric_pressure is None:
                    barometric_pressure = self._extract_float(
                        source,
                        (
                            "barometric_pressure",
                            "barometricPressure",
                            "barometricpressure",
                            "pressure",
                            "pressure_inhg",
                            "pressureInHg",
                        ),
                    )
                if dew_point_f is None:
                    dew_point_f = self._extract_float(
                        source,
                        (
                            "dew_point",
                            "dewPoint",
                            "dewpoint",
                            "dew_point_f",
                            "dewPointF",
                        ),
                    )
                if vpd is None:
                    vpd = self._extract_float(source, ("vpd", "vapour_pressure_deficit", "vapourPressureDeficit"))
                if heat_index_f is None:
                    heat_index_f = self._extract_float(
                        source,
                        (
                            "heat_index",
                            "heatIndex",
                            "heatindex",
                            "heat_index_f",
                            "heatIndexF",
                        ),
                    )

            node.set_metrics(
                connected=True,
                temperature_f=temp_f,
                humidity_pct=humidity_pct,
                battery_v=battery_v,
                barometric_pressure=barometric_pressure,
                dew_point_f=dew_point_f,
                vpd=vpd,
                heat_index_f=heat_index_f,
            )
            LOGGER.debug(
                "Node %s Updated: Temp=%s°F, Humidity=%s%%, Pressure=%s, DewPoint=%s°F, VPD=%s, HeatIndex=%s°F, Type=%s",
                sensor_name,
                temp_f,
                humidity_pct,
                barometric_pressure,
                dew_point_f,
                vpd,
                heat_index_f,
                sensor_type,
            )
            attempted_updates.append(f"{sensor_name} ({address})")

        LOGGER.debug(
            "SensorPush %s node update targets (%s): %s",
            reason,
            len(attempted_updates),
            ", ".join(attempted_updates) if attempted_updates else "<none>",
        )
        if skipped_updates:
            LOGGER.debug(
                "SensorPush %s node updates skipped (%s): %s",
                reason,
                len(skipped_updates),
                ", ".join(skipped_updates),
            )

        if discover_nodes:
            existing_sensor_addresses = {
                address
                for address, _ in self._get_existing_nodes().items()
                if isinstance(address, str) and address.startswith(self.SENSOR_ADDR_PREFIX)
            }
            stale_addresses = sorted(existing_sensor_addresses - active_addresses)

            for address in stale_addresses:
                try:
                    self._delete_node(address)
                    LOGGER.debug("Deleted stale child sensor node: %s", address)
                except Exception:
                    LOGGER.exception("Failed deleting stale child sensor node: %s", address)

    def start(self) -> None:
        LOGGER.info(
            "SensorPushController started. version=%s update_mode=%s shortPoll=60s longPoll=300s",
            self._server_version,
            "short" if self._runtime_config.use_short_poll_updates else "long",
        )
        self._run_poll_cycle("startup", discover_nodes=True)

    def stop(self) -> None:
        LOGGER.info("SensorPushController stopped. version=%s", self._server_version)

    def custom_params_changed(self, params: Dict[str, Any] | None = None) -> None:
        self._reload_config()
        LOGGER.debug(
            "Custom params updated. update_mode=%s sample_limit=%s",
            "short" if self._runtime_config.use_short_poll_updates else "long",
            self._runtime_config.sample_limit,
        )
        # Update MQTT logger level if verbose_mqtt_logging param changed
        custom_params = self._get_custom_params()
        mqtt_logger = logging.getLogger("udi_interface.interface")
        verbose = str(custom_params.get("verbose_mqtt_logging") or "0").lower() in ("1", "true")
        mqtt_logger.setLevel(logging.INFO if verbose else logging.WARNING)
        # PG3 sends customparams before customtypeddata during startup. Defer poll refresh
        # until typed params are available to avoid a transient "missing token" warning.
        if self._client is None and not self._runtime_config.account_token:
            LOGGER.debug("Deferring config refresh from custom_params_changed until typed params load")
            return
        self._run_config_refresh_once("custom_params_changed")

    def custom_typed_data_changed(self, params: Dict[str, Any] | None = None) -> None:
        if isinstance(params, dict):
            self._typed_params_data = dict(params)
        self._reload_config()
        LOGGER.debug("Custom typed params updated from PG3 Admin form")
        self._run_config_refresh_once("custom_typed_data_changed")

    def poll(self, poll_data: Any) -> None:
        text = str(poll_data)
        if "shortPoll" in text:
            self.shortPoll()
        if "longPoll" in text:
            self.longPoll()

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

        has_account_token = bool(self._runtime_config.account_token)
        auth_decision = "account_token_exchange" if has_account_token else "none"
        LOGGER.debug(
            "Config reload: auth_decision=%s account_token_present=%s email_present=%s short_poll=%s sample_limit=%s",
            auth_decision,
            has_account_token,
            bool(self._runtime_config.email),
            self._runtime_config.use_short_poll_updates,
            self._runtime_config.sample_limit,
        )

        if has_account_token:
            self._client = SensorPushClient(
                email=self._runtime_config.email,
                account_token=self._runtime_config.account_token,
            )
            self._missing_token_warned = False
            LOGGER.debug("Auth mode: account token -> OAuth access token exchange")
        else:
            self._client = None
            LOGGER.debug("SensorPush account token not configured yet")

    def _run_poll_cycle(self, reason: str, discover_nodes: bool) -> None:
        if not self._client:
            if not self._missing_token_warned:
                LOGGER.warning("SensorPush account token not configured. Set sensorpush_account_token.")
                self._missing_token_warned = True
            self.setDriver("ST", 0)
            return

        try:
            self._poll_cycle_seq += 1
            cycle_id = self._poll_cycle_seq
            LOGGER.info(
                "SensorPush updating: id=%s reason=%s discover_nodes=%s version=%s",
                cycle_id,
                reason,
                discover_nodes,
                self._server_version,
            )
            sensors_payload = self._client.list_sensors()
            sensors = sensors_payload if isinstance(sensors_payload, dict) else {}
            sensor_ids = list(sensors.keys())
            sensor_descriptions = [
                self._describe_sensor(sensor_id, sensor_data)
                for sensor_id, sensor_data in sensors.items()
            ]
            LOGGER.debug(
                "SensorPush sensors returned (%s): %s",
                len(sensor_descriptions),
                ", ".join(sensor_descriptions) if sensor_descriptions else "<none>",
            )

            samples_payload = self._client.get_samples(
                sensor_ids=sensor_ids,
                limit=self._runtime_config.sample_limit,
            )
            sample_map = samples_payload.get("sensors", {}) if isinstance(samples_payload, dict) else {}
            if not isinstance(sample_map, dict):
                sample_map = {}

            if self._is_moredebug_enabled():
                LOGGER.info("MOREDEBUG SensorPush /devices/sensors payload: %s", self._json_preview(sensors_payload))
                LOGGER.info("MOREDEBUG SensorPush /samples payload: %s", self._json_preview(samples_payload))

            effective_discover_nodes = discover_nodes
            if not discover_nodes:
                if not self._initial_discovery_completed:
                    existing_sensor_nodes = [
                        addr
                        for addr in self._get_existing_nodes().keys()
                        if isinstance(addr, str) and addr.startswith(self.SENSOR_ADDR_PREFIX)
                    ]
                    if not existing_sensor_nodes and sensor_ids:
                        effective_discover_nodes = True
                        LOGGER.info(
                            "No child sensor nodes found during update-only poll; running one-time discovery (reason=%s)",
                            reason,
                        )

            if effective_discover_nodes:
                self._initial_discovery_completed = True

            self._sync_sensor_nodes(
                sensors=sensors,
                sample_map=sample_map,
                discover_nodes=effective_discover_nodes,
                reason=reason,
            )

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
            LOGGER.debug(
                "SensorPush %s update complete: id=%s sensors=%s samples=%s discover_nodes=%s version=%s",
                reason,
                cycle_id,
                len(sensor_ids),
                total_samples,
                effective_discover_nodes,
                self._server_version,
            )
        except SensorPushApiError as err:
            self.setDriver("ST", 0)
            LOGGER.error("SensorPush API failure during %s poll: %s", reason, err)
        except Exception:
            self.setDriver("ST", 0)
            LOGGER.exception("Unexpected error during %s poll", reason)

    def shortPoll(self) -> None:
        # Short poll always updates child node data.
        # Discovery behavior is controlled by use_short_poll_updates.
        discover_nodes = self._runtime_config.use_short_poll_updates
        LOGGER.debug("shortPoll triggered: executing update cycle discover_nodes=%s (version=%s)", discover_nodes, self._server_version)
        self._run_poll_cycle("shortPoll", discover_nodes=discover_nodes)

    def longPoll(self) -> None:
        # Long poll is authoritative discovery pass: add/remove nodes and update data.
        LOGGER.debug("longPoll triggered: executing discovery+update cycle (version=%s)", self._server_version)
        self._run_poll_cycle("longPoll", discover_nodes=True)

    def query(self, command: Dict[str, Any] | None = None) -> bool:
        self._run_poll_cycle("query", discover_nodes=True)
        return True
