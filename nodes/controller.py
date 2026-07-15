from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

import requests
import udi_interface
from udi_interface import Node

from config import RuntimeConfig
from nodes.gateway import SensorPushGatewayNode
from nodes.sensor import SensorPushSensorNode
from sensorpush_api import SensorPushApiError, SensorPushClient

LOGGER = udi_interface.LOGGER


class SensorPushController(Node):
    id = "controller"
    SENSOR_TYPE_INDEX = {
        "HT1": 1,
        "HT.W": 2,
        "HTP.XW": 3,
    }
    PARAM_NOTICE_KEY = "sensorpush_required_params"
    TYPED_PARAM_SCHEMA = [
        {"name": "sensorpush_email", "title": "SensorPush Email", "desc": "Required SensorPush account email.", "isRequired": True},
        {"name": "sensorpush_password", "title": "SensorPush Password", "desc": "Required SensorPush account password.", "isRequired": True},
        {"name": "use_short_poll_updates", "title": "Use Short Poll Updates", "desc": "Default 0: set 1 for 1-minute test updates.", "isRequired": False, "defaultValue": "0"},
        {"name": "fetch_limit", "title": "Fetch Limit", "desc": "How many recent samples to request per sensor (default 1).", "isRequired": False, "defaultValue": "1"},
        {"name": "sensor_offline_hours", "title": "Sensor Offline Hours", "desc": "Alert when no fresh sample is seen for this many hours (default 1).", "isRequired": False, "defaultValue": "1"},
        {"name": "sensor_offline_notify_recovery", "title": "Notify On Recovery", "desc": "Send ntfy when an offline sensor starts reporting again (default 1).", "isRequired": False, "defaultValue": "1"},
        {"name": "sensor_ntfy_ignore_list", "title": "Sensor ntfy Ignore List", "desc": "Comma-separated sensor names to ignore for ntfy online/offline alerts.", "isRequired": False, "defaultValue": ""},
        {"name": "ntfy_topic", "title": "ntfy Topic", "desc": "Optional: set to enable push notifications via ntfy.", "isRequired": False, "defaultValue": ""},
        {"name": "ntfy_server", "title": "ntfy Server URL", "desc": "Optional: ntfy server URL.", "isRequired": False, "defaultValue": "https://ntfy.sh"},
        {"name": "ntfy_token", "title": "ntfy Access Token", "desc": "Optional bearer token for private ntfy topics.", "isRequired": False, "defaultValue": ""},
    ]

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "GV0", "value": 0, "uom": 56},
        {"driver": "GV1", "value": 0, "uom": 56},
        {"driver": "GV2", "value": 0, "uom": 56},
    ]

    commands = {
        "QUERY": "query",
    }

    @staticmethod
    def _resolve_profile_num(polyglot: Any) -> int:
        config = getattr(polyglot, "polyConfig", None) or {}
        raw_profile = config.get("profileNum")
        try:
            if raw_profile is not None:
                parsed = int(raw_profile)
                if parsed > 0:
                    return parsed
        except (TypeError, ValueError):
            pass

        pg3init = os.environ.get("PG3INIT", "")
        if pg3init:
            try:
                decoded = base64.b64decode(pg3init).decode("utf-8", errors="ignore")
                parsed_json = json.loads(decoded)
                raw_profile = parsed_json.get("profileNum")
                parsed = int(raw_profile)
                if parsed > 0:
                    return parsed
            except Exception:
                pass

        return 0

    def __init__(self, polyglot: Any) -> None:
        profile_num_value = self._resolve_profile_num(polyglot)
        profile_num = str(profile_num_value)
        instance_token = hashlib.md5(profile_num.encode("utf-8")).hexdigest()[:2]
        controller_address = f"ctrl_{instance_token}"
        controller_name = f"SensorPush({profile_num})" if profile_num_value > 0 else "SensorPush"

        super().__init__(polyglot, controller_address, controller_address, controller_name)
        self.poly = polyglot
        self._instance_token = instance_token
        self._sensor_addr_prefix = f"s{instance_token}_"
        self._gateway_addr_prefix = f"g{instance_token}_"
        self._runtime_config = RuntimeConfig()
        self._client: SensorPushClient | None = None
        self._server_version = self._resolve_server_version()
        self._last_poll_utc: datetime | None = None
        self._typed_params_data: Dict[str, Any] = {}
        self._sensor_last_seen_utc: Dict[str, datetime] = {}
        self._sensor_offline_alerted: set[str] = set()
        self._gateway_offline_alerted: set[str] = set()
        self._gateway_defs_refreshed = False
        self._startup_notified = False

        # Publish typed parameter schema so PG3 consistently renders the admin fields.
        self.typed_params = udi_interface.Custom(self.poly, "customtypedparams")
        self.typed_params.load(self.TYPED_PARAM_SCHEMA, True)

        LOGGER.info(
            "Address namespace initialized: profileNum=%s controller=%s sensor_prefix=%s gateway_prefix=%s",
            profile_num,
            controller_address,
            self._sensor_addr_prefix,
            self._gateway_addr_prefix,
        )

        self._reload_config()

    def _set_notice(self, message: str) -> None:
        adder = getattr(self.poly, "addNotice", None)
        if callable(adder):
            try:
                adder(message, self.PARAM_NOTICE_KEY)
                return
            except Exception:
                pass
            try:
                adder({self.PARAM_NOTICE_KEY: message})
            except Exception:
                LOGGER.debug("Unable to publish notice", exc_info=True)

    def _clear_notice(self) -> None:
        remover = getattr(self.poly, "removeNotice", None)
        if callable(remover):
            try:
                remover(self.PARAM_NOTICE_KEY)
            except Exception:
                LOGGER.debug("Unable to clear notice", exc_info=True)

    def _coerce_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _coerce_bool(self, value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if value is None:
            return None

        text = str(value).strip().lower()
        if not text:
            return None

        truthy = {"1", "true", "yes", "y", "on", "online", "connected", "up", "available"}
        falsy = {"0", "false", "no", "n", "off", "offline", "disconnected", "down", "unavailable"}

        if text in truthy:
            return True
        if text in falsy:
            return False
        return None

    def _first_float(self, container: Any, keys: Iterable[str]) -> float | None:
        if not isinstance(container, dict):
            return None
        for key in keys:
            if key in container:
                value = self._coerce_float(container.get(key))
                if value is not None:
                    return value
        return None

    def _extract_gateway_online(self, gateway_data: Any) -> bool:
        if not isinstance(gateway_data, dict):
            return False

        for key in (
            "online",
            "isOnline",
            "is_online",
            "connected",
            "isConnected",
            "is_connected",
            "reachable",
            "isReachable",
            "is_reachable",
            "active",
            "isActive",
            "is_active",
        ):
            if key in gateway_data:
                parsed = self._coerce_bool(gateway_data.get(key))
                if parsed is not None:
                    return parsed

        for key in ("status", "connectionStatus", "state"):
            if key in gateway_data:
                parsed = self._coerce_bool(gateway_data.get(key))
                if parsed is not None:
                    return parsed

        return True

    @classmethod
    def _sensor_type_index(cls, sensor_type: str | None) -> int:
        normalized = str(sensor_type or "").strip().upper()
        return cls.SENSOR_TYPE_INDEX.get(normalized, 0)

    def _ensure_required_params(self) -> bool:
        params = self._get_custom_params()
        email = str(params.get("sensorpush_email") or "").strip()
        account_token = str(
            params.get("sensorpush_password")
            or params.get("sensorpush_account_token")
            or ""
        ).strip()

        if not email or not account_token:
            message = "Set required custom params: sensorpush_email, sensorpush_password."
            self._set_notice(message)
            return False

        self._clear_notice()
        return True

    def _sensor_address(self, sensor_id: str) -> str:
        digest = hashlib.md5(f"{self._instance_token}:{sensor_id}".encode("utf-8")).hexdigest()[:10]
        return f"{self._sensor_addr_prefix}{digest}"

    def _gateway_address(self, gateway_id: str) -> str:
        digest = hashlib.md5(f"{self._instance_token}:{gateway_id}".encode("utf-8")).hexdigest()[:10]
        return f"{self._gateway_addr_prefix}{digest}"

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

    def _describe_sensor(self, sensor_id: Any, sensor_data: Any) -> str:
        if isinstance(sensor_data, dict):
            name = str(sensor_data.get("name") or sensor_id)
        else:
            name = str(sensor_id)
        return f"{sensor_id}:{name}"

    def _calc_dew_point_f(self, temperature_f: float | None, humidity_pct: float | None) -> float | None:
        if temperature_f is None or humidity_pct is None:
            return None
        if humidity_pct <= 0 or humidity_pct > 100:
            return None
        temp_c = (temperature_f - 32.0) * (5.0 / 9.0)
        a = 17.62
        b = 243.12
        gamma = math.log(humidity_pct / 100.0) + ((a * temp_c) / (b + temp_c))
        dew_c = (b * gamma) / (a - gamma)
        return (dew_c * 9.0 / 5.0) + 32.0

    def _calc_vpd_kpa(self, temperature_f: float | None, humidity_pct: float | None) -> float | None:
        if temperature_f is None or humidity_pct is None:
            return None
        if humidity_pct < 0 or humidity_pct > 100:
            return None
        temp_c = (temperature_f - 32.0) * (5.0 / 9.0)
        svp = 0.6108 * math.exp((17.27 * temp_c) / (temp_c + 237.3))
        return svp * (1.0 - (humidity_pct / 100.0))

    def _calc_heat_index_f(self, temperature_f: float | None, humidity_pct: float | None) -> float | None:
        if temperature_f is None or humidity_pct is None:
            return None
        if humidity_pct < 0 or humidity_pct > 100:
            return None

        t = temperature_f
        rh = humidity_pct

        if t < 80.0 or rh < 40.0:
            return t

        heat_index = (
            -42.379
            + 2.04901523 * t
            + 10.14333127 * rh
            - 0.22475541 * t * rh
            - 0.00683783 * t * t
            - 0.05481717 * rh * rh
            + 0.00122874 * t * t * rh
            + 0.00085282 * t * rh * rh
            - 0.00000199 * t * t * rh * rh
        )

        if rh < 13 and 80 <= t <= 112:
            heat_index -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
        elif rh > 85 and 80 <= t <= 87:
            heat_index += ((rh - 85) / 10) * ((87 - t) / 5)

        return heat_index

    @staticmethod
    def _parse_timestamp_utc(value: Any) -> datetime | None:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            epoch = float(value)
            if epoch > 1_000_000_000_000:
                epoch = epoch / 1000.0
            try:
                return datetime.fromtimestamp(epoch, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None

        text = str(value).strip()
        if not text:
            return None

        try:
            numeric = float(text)
            if numeric > 1_000_000_000_000:
                numeric = numeric / 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError):
            pass

        try:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    def _extract_sample_time_utc(self, sample: Dict[str, Any]) -> datetime | None:
        timestamp_keys = (
            "observed",
            "timestamp",
            "time",
            "sampleTime",
            "recorded",
            "date",
            "created",
            "updatedAt",
            "lastObserved",
        )
        for key in timestamp_keys:
            if key in sample:
                parsed = self._parse_timestamp_utc(sample.get(key))
                if parsed is not None:
                    return parsed
        return None

    def _is_sensor_offline(self, last_seen_utc: datetime | None, now_utc: datetime) -> bool:
        if last_seen_utc is None:
            return False
        if self._runtime_config.sensor_offline_hours <= 0:
            return False
        age_seconds = (now_utc - last_seen_utc).total_seconds()
        return age_seconds >= (self._runtime_config.sensor_offline_hours * 3600.0)

    def _notify_ntfy(self, title: str, message: str, tags: str) -> None:
        topic = self._runtime_config.ntfy_topic.strip()
        if not topic:
            return

        base_url = self._runtime_config.ntfy_server.strip() or "https://ntfy.sh"
        publish_url = f"{base_url.rstrip('/')}/{topic}"
        headers = {
            "Title": title,
            "Tags": tags,
        }
        if self._runtime_config.ntfy_token:
            headers["Authorization"] = f"Bearer {self._runtime_config.ntfy_token}"

        try:
            response = requests.post(
                publish_url,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=10,
            )
            if response.status_code >= 400:
                LOGGER.warning(
                    "ntfy publish failed: status=%s body=%s",
                    response.status_code,
                    response.text,
                )
        except Exception:
            LOGGER.exception("Failed to publish ntfy notification")

    def _handle_offline_state(
        self,
        sensor_id: str,
        sensor_name: str,
        offline: bool,
        last_seen_utc: datetime,
        now_utc: datetime,
    ) -> None:
        age_hours = (now_utc - last_seen_utc).total_seconds() / 3600.0
        alert_key = sensor_id
        ignored_targets = self._runtime_config.sensor_ntfy_ignore_list or set()
        ignore_ntfy = (
            sensor_name.strip().lower() in ignored_targets
            or sensor_id.strip().lower() in ignored_targets
        )

        if offline and alert_key not in self._sensor_offline_alerted:
            self._sensor_offline_alerted.add(alert_key)
            LOGGER.warning(
                "Sensor appears offline: %s (%s) last_seen_utc=%s age_hours=%.2f threshold_hours=%.2f",
                sensor_name,
                sensor_id,
                last_seen_utc.isoformat(),
                age_hours,
                self._runtime_config.sensor_offline_hours,
            )
            if not ignore_ntfy:
                self._notify_ntfy(
                    title="Sensor offline",
                    message=(
                        f"Sensor '{sensor_name}' ({sensor_id}) is offline after {age_hours:.1f}h "
                        f"(threshold {self._runtime_config.sensor_offline_hours:.1f}h)."
                    ),
                    tags="warning,sensorpush",
                )
            return

        if not offline and alert_key in self._sensor_offline_alerted:
            self._sensor_offline_alerted.remove(alert_key)
            LOGGER.info("Sensor recovered from offline state: %s (%s)", sensor_name, sensor_id)
            if self._runtime_config.sensor_offline_notify_recovery and not ignore_ntfy:
                self._notify_ntfy(
                    title="Sensor online",
                    message=f"Sensor '{sensor_name}' ({sensor_id}) is reporting again.",
                    tags="white_check_mark,sensorpush",
                )

    def _handle_gateway_state(self, gateway_id: str, gateway_name: str, online: bool) -> None:
        alert_key = gateway_id

        if not online and alert_key not in self._gateway_offline_alerted:
            self._gateway_offline_alerted.add(alert_key)
            LOGGER.warning("Gateway appears offline: %s (%s)", gateway_name, gateway_id)
            self._notify_ntfy(
                title="Gateway offline",
                message=f"Gateway '{gateway_name}' ({gateway_id}) is offline.",
                tags="warning,sensorpush",
            )
            return

        if online and alert_key in self._gateway_offline_alerted:
            self._gateway_offline_alerted.remove(alert_key)
            LOGGER.info("Gateway recovered from offline state: %s (%s)", gateway_name, gateway_id)
            if self._runtime_config.sensor_offline_notify_recovery:
                self._notify_ntfy(
                    title="Gateway online",
                    message=f"Gateway '{gateway_name}' ({gateway_id}) is back online.",
                    tags="white_check_mark,sensorpush",
                )

    def _sync_sensor_nodes(self, sensors: Dict[str, Any], sample_map: Dict[str, Any], poll_utc: datetime) -> int:
        active_addresses: set[str] = set()
        offline_count = 0

        for sensor_id, sensor_data in sensors.items():
            sensor_id_text = str(sensor_id)
            address = self._sensor_address(sensor_id_text)
            active_addresses.add(address)

            sensor_name = sensor_id_text
            if isinstance(sensor_data, dict):
                sensor_name = str(sensor_data.get("name") or sensor_id_text)

            node = self._get_node(address)
            if not isinstance(node, SensorPushSensorNode):
                node = SensorPushSensorNode(self.poly, address=address, name=sensor_name, primary=self.address)
                self.poly.addNode(node)
                LOGGER.info("Created child sensor node: %s (%s)", sensor_name, address)

            latest_sample: Dict[str, Any] = {}
            samples = sample_map.get(sensor_id)
            if isinstance(samples, list) and samples and isinstance(samples[0], dict):
                latest_sample = samples[0]

            last_seen_utc = self._sensor_last_seen_utc.get(sensor_id_text)
            parsed_sample_time = self._extract_sample_time_utc(latest_sample) if latest_sample else None
            if parsed_sample_time is not None:
                last_seen_utc = parsed_sample_time
            elif latest_sample or last_seen_utc is None:
                last_seen_utc = poll_utc

            self._sensor_last_seen_utc[sensor_id_text] = last_seen_utc
            is_offline = self._is_sensor_offline(last_seen_utc, poll_utc)
            if is_offline:
                offline_count += 1

            self._handle_offline_state(
                sensor_id=sensor_id_text,
                sensor_name=sensor_name,
                offline=is_offline,
                last_seen_utc=last_seen_utc,
                now_utc=poll_utc,
            )

            battery_v = self._first_float(sensor_data, ("battery_voltage", "batteryVoltage", "voltage"))
            temperature_f = self._first_float(latest_sample, ("temperature", "temperature_f", "temp"))
            humidity_pct = self._first_float(latest_sample, ("humidity", "humidity_pct", "rh"))
            dew_point_f = self._first_float(latest_sample, ("dewpoint", "dew_point", "dewPoint", "dewpoint_f"))
            if dew_point_f is None:
                dew_point_f = self._calc_dew_point_f(temperature_f, humidity_pct)

            vpd_kpa = self._first_float(latest_sample, ("vpd", "vpd_kpa", "vapor_pressure_deficit"))
            if vpd_kpa is None:
                vpd_kpa = self._calc_vpd_kpa(temperature_f, humidity_pct)

            signal_dbm = self._first_float(sensor_data, ("rssi", "signal", "signal_dbm", "signalStrength"))
            sensor_type = None
            if isinstance(sensor_data, dict):
                sensor_type = str(
                    sensor_data.get("device_type")
                    or sensor_data.get("sensor_type")
                    or sensor_data.get("type")
                    or sensor_data.get("model")
                    or ""
                ).strip() or None
            sensor_type_index = self._sensor_type_index(sensor_type)

            barometric = self._first_float(
                latest_sample,
                ("barometric", "barometric_pressure", "pressure", "pressure_inhg", "pressure_hpa"),
            )
            if barometric is None:
                barometric = self._first_float(
                    sensor_data,
                    ("barometric", "barometric_pressure", "pressure", "pressure_inhg", "pressure_hpa"),
                )

            heat_index_f = self._first_float(latest_sample, ("heatindex", "heat_index", "heatIndex", "heat_index_f"))
            if heat_index_f is None:
                heat_index_f = self._calc_heat_index_f(temperature_f, humidity_pct)

            node.set_metrics(
                connected=not is_offline,
                temperature_f=temperature_f,
                humidity_pct=humidity_pct,
                battery_v=battery_v,
                dew_point_f=dew_point_f,
                vpd_kpa=vpd_kpa,
                signal_dbm=signal_dbm,
                barometric=barometric,
                heat_index_f=heat_index_f,
                sensor_type_index=sensor_type_index,
            )

        existing_sensor_addresses = {
            address
            for address, _ in self._get_existing_nodes().items()
            if isinstance(address, str) and address.startswith(self._sensor_addr_prefix)
        }
        offline_addresses = sorted(existing_sensor_addresses - active_addresses)
        for address in offline_addresses:
            try:
                self._delete_node(address)
                LOGGER.info("Deleted offline child sensor node: %s", address)
            except Exception:
                LOGGER.exception("Failed deleting offline child sensor node: %s", address)

        active_sensor_ids = {str(sensor_id) for sensor_id in sensors.keys()}
        for offline_sensor_id in list(self._sensor_last_seen_utc.keys()):
            if offline_sensor_id not in active_sensor_ids:
                self._sensor_last_seen_utc.pop(offline_sensor_id, None)
                self._sensor_offline_alerted.discard(offline_sensor_id)

        return offline_count

    def _sync_gateway_nodes(self, gateways: Dict[str, Any]) -> None:
        if not self._gateway_defs_refreshed:
            existing_gateway_addresses = {
                address
                for address, _ in self._get_existing_nodes().items()
                if isinstance(address, str) and address.startswith(self._gateway_addr_prefix)
            }
            for address in sorted(existing_gateway_addresses):
                try:
                    self._delete_node(address)
                    LOGGER.info("Deleted gateway node for node definition refresh: %s", address)
                except Exception:
                    LOGGER.exception("Failed deleting gateway node for node definition refresh: %s", address)
            self._gateway_defs_refreshed = True

        active_addresses: set[str] = set()
        for gateway_id, gateway_data in gateways.items():
            gateway_id_text = str(gateway_id)
            address = self._gateway_address(gateway_id_text)
            active_addresses.add(address)

            gateway_name = gateway_id_text
            if isinstance(gateway_data, dict):
                gateway_name = str(
                    gateway_data.get("name")
                    or gateway_data.get("networkName")
                    or gateway_data.get("label")
                    or gateway_id_text
                )

            node = self._get_node(address)
            if not isinstance(node, SensorPushGatewayNode):
                node = SensorPushGatewayNode(self.poly, address=address, name=gateway_name, primary=self.address)
                self.poly.addNode(node)
                LOGGER.info("Created child gateway node: %s (%s)", gateway_name, address)

            online = self._extract_gateway_online(gateway_data)
            node.set_online(online)
            self._handle_gateway_state(gateway_id_text, gateway_name, online)

        existing_gateway_addresses = {
            address
            for address, _ in self._get_existing_nodes().items()
            if isinstance(address, str) and address.startswith(self._gateway_addr_prefix)
        }
        offline_addresses = sorted(existing_gateway_addresses - active_addresses)
        for address in offline_addresses:
            try:
                self._delete_node(address)
                LOGGER.info("Deleted offline child gateway node: %s", address)
            except Exception:
                LOGGER.exception("Failed deleting offline child gateway node: %s", address)

    def _get_custom_params(self) -> Dict[str, str]:
        config = getattr(self.poly, "polyConfig", None) or {}
        params: Dict[str, Any] = {}

        for key in ("customTypedData", "customtypeddata"):
            typed = config.get(key, {})
            if isinstance(typed, dict):
                params.update(typed)
            elif isinstance(typed, list):
                for item in typed:
                    if isinstance(item, dict) and "name" in item:
                        params[str(item["name"])] = item.get("value", "")

        if self._typed_params_data:
            params.update(self._typed_params_data)

        normalized: Dict[str, str] = {}
        for k, v in params.items():
            if isinstance(v, list):
                normalized[str(k)] = str(v[0]) if v else ""
            else:
                normalized[str(k)] = str(v)
        return normalized

    def _merge_typed_event_params(self, params: Dict[str, Any]) -> Dict[str, str]:
        merged = self._get_custom_params()

        payload: Dict[str, Any] = {}

        # Some PG3 paths pass the values directly on the event payload.
        direct_keys = (
            "sensorpush_email",
            "sensorpush_password",
            "sensorpush_account_token",
            "use_short_poll_updates",
            "fetch_limit",
            "sensor_offline_hours",
            "sensor_offline_notify_recovery",
            "sensor_ntfy_ignore_list",
            "ntfy_topic",
            "ntfy_server",
            "ntfy_token",
        )
        for key in direct_keys:
            if key in params:
                payload[key] = params.get(key)

        for key in ("customtypeddata", "customTypedData"):
            raw = params.get(key)
            if isinstance(raw, dict):
                payload.update(raw)
            elif isinstance(raw, str):
                try:
                    decoded = json.loads(raw)
                    if isinstance(decoded, dict):
                        payload.update(decoded)
                except Exception:
                    pass
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and "name" in item:
                        payload[str(item["name"])] = item.get("value", "")

        # Some PG3 events provide only {"value": "{...}"} without key metadata.
        if not payload and "value" in params:
            raw_value_any = params.get("value")
            if isinstance(raw_value_any, dict):
                payload.update(raw_value_any)
            elif isinstance(raw_value_any, str):
                try:
                    decoded = json.loads(raw_value_any)
                    if isinstance(decoded, dict):
                        payload.update(decoded)
                except Exception:
                    pass

        if not payload and params.get("key") in ("customtypeddata", "customTypedData"):
            raw_value = params.get("value")
            if isinstance(raw_value, dict):
                payload.update(raw_value)
            elif isinstance(raw_value, str):
                try:
                    decoded = json.loads(raw_value)
                    if isinstance(decoded, dict):
                        payload.update(decoded)
                except Exception:
                    pass
            elif isinstance(raw_value, list):
                for item in raw_value:
                    if isinstance(item, dict) and "name" in item:
                        payload[str(item["name"])] = item.get("value", "")

        for key, value in payload.items():
            merged[str(key)] = str(value)

        LOGGER.debug(
            "Merged typed params keys=%s email_present=%s token_present=%s",
            sorted(merged.keys()),
            bool(str(merged.get("sensorpush_email") or "").strip()),
            bool(str(merged.get("sensorpush_password") or merged.get("sensorpush_account_token") or "").strip()),
        )
        return merged

    def _resolve_server_version(self) -> str:
        config = getattr(self.poly, "polyConfig", None) or {}
        for key in ("version", "nodeVersion", "serverVersion"):
            raw_value = config.get(key)
            if raw_value is not None and str(raw_value).strip():
                return str(raw_value).strip()

        try:
            server_json_path = Path(__file__).resolve().parent.parent / "server.json"
            server_json = json.loads(server_json_path.read_text(encoding="utf-8"))
            raw_version = server_json.get("version")
            if raw_version is not None and str(raw_version).strip():
                return str(raw_version).strip()
        except Exception:
            LOGGER.debug("Unable to resolve server version from server.json", exc_info=True)

        return "unknown"

    def _startup_ntfy_message(self) -> str:
        return f"SensorPush node server started and is running. Version: {self._server_version}."

    def _reload_config(self, custom_params: Dict[str, str] | None = None) -> None:
        if custom_params is None:
            custom_params = self._get_custom_params()
        self._runtime_config = RuntimeConfig.from_sources(custom_params, os.environ)

        if self._runtime_config.email and self._runtime_config.account_token:
            self._client = SensorPushClient(
                email=self._runtime_config.email,
                account_token=self._runtime_config.account_token,
            )
            LOGGER.info("Auth mode: account token -> OAuth access token exchange")
        else:
            self._client = None
            LOGGER.debug("SensorPush account token not configured yet; waiting for custom typed params.")

    def _register_connection_status_node(self) -> None:
        setter = getattr(self.poly, "setController", None)
        if not callable(setter):
            return

        # PG3/udi_interface signatures differ across versions; try common forms.
        for args in ((self.address, "ST"), (self.address,)):
            try:
                setter(*args)
                LOGGER.info(
                    "Registered connection status mapping: controller=%s driver=%s",
                    self.address,
                    "ST",
                )
                return
            except TypeError:
                continue
            except Exception:
                LOGGER.debug("setController call failed for args=%s", args, exc_info=True)
                return

    def start(self, command: Dict[str, Any] | None = None) -> None:
        LOGGER.info(
            "SensorPushController started. update_mode=%s shortPoll=60s longPoll=300s",
            "short" if self._runtime_config.use_short_poll_updates else "long",
        )
        self._register_connection_status_node()
        if not self._ensure_required_params():
            return

        startup_topic = self._runtime_config.ntfy_topic.strip()
        if startup_topic:
            self._notify_ntfy(
                title="SensorPush started",
                message=self._startup_ntfy_message(),
                tags="rocket,sensorpush",
            )
            self._startup_notified = True
        self._run_poll_cycle("startup")

    def custom_typed_data_changed(self, params: Dict[str, Any] | None = None) -> None:
        previous_topic = self._runtime_config.ntfy_topic.strip()
        merged_custom_params = None
        if isinstance(params, dict):
            merged_custom_params = self._merge_typed_event_params(params)
            self._typed_params_data = dict(merged_custom_params)

        self._reload_config(merged_custom_params)
        if not self._ensure_required_params():
            LOGGER.info("Custom typed params updated, waiting for required values")
            return

        LOGGER.info(
            "Custom typed params updated. update_mode=%s fetch_limit=%s",
            "short" if self._runtime_config.use_short_poll_updates else "long",
            self._runtime_config.fetch_limit,
        )

        current_topic = self._runtime_config.ntfy_topic.strip()
        if current_topic and not self._startup_notified:
            self._notify_ntfy(
                title="SensorPush started",
                message=self._startup_ntfy_message(),
                tags="rocket,sensorpush",
            )
            self._startup_notified = True

        if current_topic and previous_topic and previous_topic != current_topic:
            self._notify_ntfy(
                title="SensorPush configuration updated",
                message=f"ntfy topic updated to '{current_topic}' for SensorPush.",
                tags="gear,sensorpush",
            )

        self._run_poll_cycle("config_update")

    def _run_poll_cycle(self, reason: str) -> None:
        if not self._client:
            LOGGER.debug("Skipping poll cycle '%s' until credentials are available", reason)
            return

        try:
            sensors_payload = self._client.list_sensors()
            sensors = sensors_payload if isinstance(sensors_payload, dict) else {}
            sensor_ids = list(sensors.keys())
            LOGGER.info(
                "SensorPush sensors returned (%s): %s",
                len(sensor_ids),
                ", ".join(self._describe_sensor(sensor_id, sensors.get(sensor_id)) for sensor_id in sensor_ids)
                if sensor_ids
                else "<none>",
            )

            gateways_payload = self._client.list_gateways()
            gateways = gateways_payload if isinstance(gateways_payload, dict) else {}
            self._sync_gateway_nodes(gateways)

            samples_payload = self._client.get_samples(
                sensor_ids=sensor_ids,
                limit=self._runtime_config.fetch_limit,
            )
            sample_map = samples_payload.get("sensors", {}) if isinstance(samples_payload, dict) else {}
            if not isinstance(sample_map, dict):
                sample_map = {}

            poll_utc = datetime.now(timezone.utc)
            offline_count = self._sync_sensor_nodes(
                sensors=sensors,
                sample_map=sample_map,
                poll_utc=poll_utc,
            )

            total_samples = 0
            for entries in sample_map.values():
                if isinstance(entries, list):
                    total_samples += len(entries)

            self.setDriver("ST", 1)
            self.setDriver("GV0", len(sensor_ids))
            self.setDriver("GV1", total_samples)
            self.setDriver("GV2", offline_count)
            self.reportDrivers()
            self._last_poll_utc = poll_utc

            LOGGER.info(
                "SensorPush %s update complete: sensors=%s gateways=%s samples=%s offline=%s",
                reason,
                len(sensor_ids),
                len(gateways),
                total_samples,
                offline_count,
            )
        except SensorPushApiError as err:
            self.setDriver("ST", 0)
            LOGGER.error("SensorPush API failure during %s poll: %s", reason, err)
        except Exception:
            self.setDriver("ST", 0)
            LOGGER.exception("Unexpected error during %s poll", reason)

    def poll(self, command: Dict[str, Any] | None = None) -> None:
        if self._runtime_config.use_short_poll_updates:
            self.shortPoll(command)
        else:
            self.longPoll(command)

    def shortPoll(self, command: Dict[str, Any] | None = None) -> None:
        if self._runtime_config.use_short_poll_updates:
            self._run_poll_cycle("shortPoll")

    def longPoll(self, command: Dict[str, Any] | None = None) -> None:
        if not self._runtime_config.use_short_poll_updates:
            self._run_poll_cycle("longPoll")

    def query(self, command: Dict[str, Any] | None = None) -> bool:
        self._run_poll_cycle("query")
        return True

    def stop(self, command: Dict[str, Any] | None = None) -> None:
        """Required method for main.py shutdown."""
        LOGGER.info("SensorPushController stopping")
        self.setDriver("ST", 0)