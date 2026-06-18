from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | int | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RuntimeConfig:
    email: str = ""
    password: str = ""
    api_token: str = ""
    allow_legacy_userpass: bool = False
    use_short_poll_updates: bool = False
    sample_limit: int = 1

    @classmethod
    def from_sources(
        cls,
        custom_params: Mapping[str, str] | None,
        env: Mapping[str, str] | None,
    ) -> "RuntimeConfig":
        custom = custom_params or {}
        environ = env or {}

        email = str(custom.get("sensorpush_email") or environ.get("SENSORPUSH_EMAIL") or "").strip()
        password = str(custom.get("sensorpush_password") or environ.get("SENSORPUSH_PASSWORD") or "").strip()
        api_token = str(custom.get("sensorpush_api_token") or environ.get("SENSORPUSH_API_TOKEN") or "").strip()
        allow_legacy_userpass = _as_bool(
            custom.get("allow_legacy_userpass")
            if "allow_legacy_userpass" in custom
            else environ.get("SENSORPUSH_ALLOW_LEGACY_USERPASS"),
            default=False,
        )
        use_short = _as_bool(
            custom.get("use_short_poll_updates")
            if "use_short_poll_updates" in custom
            else environ.get("SENSORPUSH_USE_SHORT_POLL_UPDATES"),
            default=False,
        )
        sample_limit = _as_int(
            custom.get("sample_limit")
            if "sample_limit" in custom
            else environ.get("SENSORPUSH_SAMPLE_LIMIT"),
            default=1,
        )

        if sample_limit < 1:
            sample_limit = 1
        if sample_limit > 100:
            sample_limit = 100

        return cls(
            email=email,
            password=password,
            api_token=api_token,
            allow_legacy_userpass=allow_legacy_userpass,
            use_short_poll_updates=use_short,
            sample_limit=sample_limit,
        )
