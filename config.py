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
    api_token: str = ""  # derived: set to password when no email is provided
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

        # If no email is provided, the password field holds the account token.
        if not email and password:
            api_token = password
            password = ""
        else:
            api_token = ""

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
            use_short_poll_updates=use_short,
            sample_limit=sample_limit,
        )
