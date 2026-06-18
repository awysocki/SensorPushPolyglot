from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

import requests

LOGGER = logging.getLogger(__name__)


class SensorPushApiError(RuntimeError):
    pass


class SensorPushClient:
    BASE_URL = "https://api.sensorpush.com/api/v1"

    def __init__(
        self,
        account_token: str,
        email: str = "",
        timeout_seconds: int = 20,
    ) -> None:
        self.email = email
        self.account_token = account_token
        self.timeout_seconds = timeout_seconds
        self._access_token: Optional[str] = None
        self._access_expires_at: Optional[datetime] = None

    def _post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        LOGGER.debug(
            "SensorPush API request: path=%s token_present=%s body_keys=%s",
            path,
            bool(token),
            sorted((body or {}).keys()),
        )
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
        LOGGER.debug("SensorPush API response: path=%s keys=%s", path, sorted(data.keys()))
        return data

    def _is_token_valid(self) -> bool:
        if not self._access_token or not self._access_expires_at:
            return False
        return datetime.now(timezone.utc) < self._access_expires_at

    def _exchange_account_token(self) -> None:
        LOGGER.debug("Exchanging SensorPush account token for OAuth access token")

        # Attempt 1: direct bearer exchange using account token.
        direct_ok = False
        try:
            token_response = self._post(
                "/oauth/accesstoken",
                body={},
                token=self.account_token,
            )
            LOGGER.debug(
                "Account-token direct exchange response: has_accesstoken=%s keys=%s",
                "accesstoken" in token_response,
                sorted(token_response.keys()),
            )
            token = token_response.get("accesstoken")
            if token:
                self._access_token = str(token)
                direct_ok = True
        except SensorPushApiError as err:
            # Some deployments require an authorization code first.
            err_text = str(err)
            if "Must provide an authorization code" in err_text:
                LOGGER.info(
                    "Direct account-token exchange requires authorization code; "
                    "trying authorize->accesstoken flow with token as API id"
                )
            else:
                raise

        if direct_ok:
            LOGGER.debug("Access token acquired from account token: length=%s", len(self._access_token or ""))
            self._access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=55)
            return

        # Attempt 2: request authorization code using account token as api-id style credential.
        authorize_response: Dict[str, Any]
        try:
            authorize_response = self._post(
                "/oauth/authorize",
                body={"apiId": self.account_token},
            )
        except SensorPushApiError as err:
            err_text = str(err)
            # Some accounts do not accept dashboard tokens as apiId. If email is
            # provided, retry authorize using email + token as the credential.
            if "invalid user" in err_text.lower() and self.email:
                LOGGER.info(
                    "apiId authorize was rejected; retrying authorize with email + token credential"
                )
                authorize_response = self._post(
                    "/oauth/authorize",
                    body={"email": self.email, "password": self.account_token},
                )
            elif "invalid user" in err_text.lower():
                raise SensorPushApiError(
                    "Account token was rejected as apiId. Set sensorpush_email for fallback "
                    "authorize with email + token, or verify the account token value."
                ) from err
            else:
                raise
        LOGGER.debug(
            "Authorize-with-token response: has_authorization=%s keys=%s",
            "authorization" in authorize_response,
            sorted(authorize_response.keys()),
        )
        authorization = authorize_response.get("authorization")
        if not authorization:
            raise SensorPushApiError("Authorize response missing 'authorization' token")

        token_response = self._post(
            "/oauth/accesstoken",
            body={"authorization": authorization},
        )
        LOGGER.debug(
            "Access-token-from-authorization response: has_accesstoken=%s keys=%s",
            "accesstoken" in token_response,
            sorted(token_response.keys()),
        )
        token = token_response.get("accesstoken")
        if not token:
            raise SensorPushApiError("Access token response missing 'accesstoken'")

        self._access_token = str(token)
        LOGGER.debug("Access token acquired from authorization code: length=%s", len(self._access_token))

        # Docs are inconsistent on expiry horizon; use conservative refresh window.
        self._access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=55)

    def _get_access_token(self) -> str:
        if not self._is_token_valid():
            if not self.account_token:
                raise SensorPushApiError("Account token is required for authentication")
            self._exchange_account_token()
        if not self._access_token:
            raise SensorPushApiError("Access token not available after authentication")
        return self._access_token

    def list_sensors(self) -> Dict[str, Any]:
        token = self._get_access_token()
        LOGGER.debug("Fetching SensorPush sensors list")
        return self._post("/devices/sensors", body={}, token=token)

    def list_gateways(self) -> Dict[str, Any]:
        token = self._get_access_token()
        LOGGER.debug("Fetching SensorPush gateways list")
        return self._post("/devices/gateways", body={}, token=token)

    def get_samples(self, sensor_ids: Iterable[str], limit: int = 1) -> Dict[str, Any]:
        token = self._get_access_token()
        body: Dict[str, Any] = {"limit": limit}
        ids = [s for s in sensor_ids if s]
        if ids:
            body["sensors"] = ids
        LOGGER.debug("Fetching SensorPush samples: sensor_count=%s limit=%s", len(ids), limit)
        return self._post("/samples", body=body, token=token)
