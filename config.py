from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | int | None, default: int, minimum: int = 1) -> int:
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


def _as_float(value: str | float | int | None, default: float, minimum: float = 0.0) -> float:
    if value is None:
        return default
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


def _as_csv_set(value: str | None) -> set[str]:
    if value is None:
        return set()
    return {item.strip().lower() for item in str(value).split(",") if item.strip()}


@dataclass
class RuntimeConfig:
    email: str = ""
    account_token: str = ""
    use_short_poll_updates: bool = False
    fetch_limit: int = 1
    sensor_offline_hours: float = 24.0
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str = ""
    sensor_offline_notify_recovery: bool = True
    sensor_ntfy_ignore_list: set[str] = field(default_factory=set)

    @classmethod
    def from_sources(
        cls,
        custom_params: Mapping[str, str] | None,
        env: Mapping[str, str] | None,
    ) -> "RuntimeConfig":
        custom = custom_params or {}
        environ = env or {}

        email = str(custom.get("sensorpush_email") or environ.get("SENSORPUSH_EMAIL") or "").strip()
        account_token = str(
            custom.get("sensorpush_password")
            or custom.get("sensorpush_account_token")
            or environ.get("SENSORPUSH_PASSWORD")
            or environ.get("SENSORPUSH_ACCOUNT_TOKEN")
            or ""
        ).strip()

        use_short = _as_bool(
            custom.get("use_short_poll_updates")
            if "use_short_poll_updates" in custom
            else environ.get("USE_SHORT_POLL_UPDATES"),
            default=False,
        )

        fetch_limit = _as_int(
            custom.get("fetch_limit")
            if "fetch_limit" in custom
            else environ.get("FETCH_LIMIT"),
            default=1,
            minimum=1,
        )

        sensor_offline_hours = _as_float(
            custom.get("sensor_offline_hours")
            if "sensor_offline_hours" in custom
            else environ.get("SENSOR_OFFLINE_HOURS"),
            default=24.0,
            minimum=0.0,
        )

        ntfy_topic = str(
            custom.get("ntfy_topic")
            or environ.get("NTFY_TOPIC")
            or ""
        ).strip()
        ntfy_server = str(custom.get("ntfy_server") or environ.get("NTFY_SERVER") or "https://ntfy.sh").strip()
        ntfy_token = str(custom.get("ntfy_token") or environ.get("NTFY_TOKEN") or "").strip()
        sensor_offline_notify_recovery = _as_bool(
            custom.get("sensor_offline_notify_recovery")
            if "sensor_offline_notify_recovery" in custom
            else environ.get("SENSOR_OFFLINE_NOTIFY_RECOVERY"),
            default=True,
        )
        sensor_ntfy_ignore_list = _as_csv_set(
            custom.get("sensor_ntfy_ignore_list")
            if "sensor_ntfy_ignore_list" in custom
            else environ.get("SENSOR_NTFY_IGNORE_LIST")
        )

        return cls(
            email=email,
            account_token=account_token,
            use_short_poll_updates=use_short,
            fetch_limit=fetch_limit,
            sensor_offline_hours=sensor_offline_hours,
            ntfy_topic=ntfy_topic,
            ntfy_server=ntfy_server,
            ntfy_token=ntfy_token,
            sensor_offline_notify_recovery=sensor_offline_notify_recovery,
            sensor_ntfy_ignore_list=sensor_ntfy_ignore_list,
        )
