from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

import requests


class SensorPushApiError(RuntimeError):
    pass


class SensorPushClient:
    BASE_URL = "https://api.sensorpush.com/api/v1"

    def __init__(
        self,
        email: str,
        password: str,
        api_token: str = "",
        timeout_seconds: int = 20,
    ) -> None:
        self.email = email
        self.password = password
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds
        self._access_token: Optional[str] = None
        self._access_expires_at: Optional[datetime] = None

    def _post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        response = requests.post(
            f"{self.BASE_URL}{path}",
            headers=headers,
            json=body or {},
            timeout=self.timeout_seconds,
        )

        if response.status_code >= 400:
            raise SensorPushApiError(
                f"SensorPush API error {response.status_code} for {path}: {response.text}"
            )

        data = response.json()
        if not isinstance(data, dict):
            raise SensorPushApiError(f"Unexpected response shape for {path}: {type(data).__name__}")
        return data

    def _is_token_valid(self) -> bool:
        if not self._access_token or not self._access_expires_at:
            return False
        return datetime.now(timezone.utc) < self._access_expires_at

    def _authenticate(self) -> None:
        auth_response = self._post(
            "/oauth/authorize",
            body={"email": self.email, "password": self.password},
        )
        authorization = auth_response.get("authorization")
        if not authorization:
            raise SensorPushApiError("Authorize response missing 'authorization' token")

        token_response = self._post(
            "/oauth/accesstoken",
            body={"authorization": authorization},
        )
        token = token_response.get("accesstoken")
        if not token:
            raise SensorPushApiError("Access token response missing 'accesstoken'")

        self._access_token = str(token)

        # Docs are inconsistent on expiry horizon; use conservative refresh window.
        self._access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=55)

    def _get_access_token(self) -> str:
        if self.api_token:
            return self.api_token
        if not self._is_token_valid():
            if not self.email or not self.password:
                raise SensorPushApiError("No api_token or email/password available for authentication")
            self._authenticate()
        if not self._access_token:
            raise SensorPushApiError("Access token not available after authentication")
        return self._access_token

    def list_sensors(self) -> Dict[str, Any]:
        token = self._get_access_token()
        return self._post("/devices/sensors", body={}, token=token)

    def list_gateways(self) -> Dict[str, Any]:
        token = self._get_access_token()
        return self._post("/devices/gateways", body={}, token=token)

    def get_samples(self, sensor_ids: Iterable[str], limit: int = 1) -> Dict[str, Any]:
        token = self._get_access_token()
        body: Dict[str, Any] = {"limit": limit}
        ids = [s for s in sensor_ids if s]
        if ids:
            body["sensors"] = ids
        return self._post("/samples", body=body, token=token)
