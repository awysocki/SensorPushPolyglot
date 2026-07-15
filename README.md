# SensorPush for eISY / PG3

PG3 node server for polling SensorPush Gateway Cloud API.

## Current Scope

- Poll SensorPush cloud API endpoints.
- Default production behavior: update on PG3 long poll (5 minutes).
- Optional test behavior: update on PG3 short poll (1 minute).
- Auto-manage per-sensor child nodes (add new sensors, remove missing sensors).
- Auto-manage per-gateway child nodes (add new gateways, remove missing gateways).
- BLE support may be added later.
- Load up multiple PG# servers for different accounts, name includes slot used in PG3

## Per-Sensor Node Lifecycle

Each SensorPush sensor is represented as a PG3 child node under the controller.

- New sensor appears in SensorPush: a new child node is created automatically.
- Existing sensor remains: child node values are updated.
- Sensor removed from SensorPush: corresponding child node is deleted automatically.

Child node metrics currently include:

- Temperature (F)
- Humidity (%)
- Battery voltage (V)
- Dew point (F)
- VPD (kPa)
- Signal (dBm, when provided by API)

## Per-Gateway Node Lifecycle

Each SensorPush gateway is represented as a dedicated PG3 child node under the controller.

- New gateway appears in SensorPush: a new gateway child node is created automatically.
- Existing gateway remains: gateway online/offline status is updated.
- Gateway removed from SensorPush: corresponding gateway child node is deleted automatically.

Gateway node metrics currently include:

- Online/Offline status

If `ntfy_topic` is set, the controller sends an ntfy alert when a gateway is first detected offline, and optionally sends a recovery message when it comes back online using the existing `sensor_offline_notify_recovery` setting.

Controller metrics include:

- Sensor Count
- Sample Count
- Offline Sensor Count

## Polling Strategy

SensorPush API documentation notes requests should not exceed once per minute.

This project supports two update modes:

- `long` (default): updates during PG3 `longPoll`.
- `short`: updates during PG3 `shortPoll`.

PG3 defaults used in `server.json`:

- `shortPoll`: 60 seconds
- `longPoll`: 300 seconds

## Configuration

Set credentials and behavior using PG3 custom parameters (or environment variables).

### Custom Parameters

- `sensorpush_email` (required)
- `sensorpush_password` (required)
- `use_short_poll_updates` (`true`/`false`, default: `false`)
- `fetch_limit` (default: `1`)
- `sensor_offline_hours` (default: `1`)
- `sensor_offline_notify_recovery` (`true`/`false`, default: `true`)
- `sensor_ntfy_ignore_list` (comma-separated sensor names/IDs to suppress online/offline ntfy alerts)
- `ntfy_topic` (default: ``)
- `ntfy_server` (optional, default: `https://ntfy.sh`)
- `ntfy_token` (optional bearer token for private ntfy topics)


The typed PG3 admin fields are defined in [server.json](server.json) and loaded at startup by [main.py](main.py).

Authentication mode:

- Email + password: set `sensorpush_email` and `sensorpush_password`.
- The node server exchanges credentials for OAuth access token before API calls.

### Environment Variables

- `SENSORPUSH_EMAIL`
- `SENSORPUSH_PASSWORD`
- `USE_SHORT_POLL_UPDATES`
- `FETCH_LIMIT`
- `SENSOR_OFFLINE_HOURS`
- `SENSOR_OFFLINE_NOTIFY_RECOVERY`
- `SENSOR_NTFY_IGNORE_LIST`
- `NTFY_TOPIC`
- `NTFY_SERVER`
- `NTFY_TOKEN`


Custom parameters take precedence over environment variables.
Custom parameters stay lower-case snake_case. Environment variables use the same words in upper-case.

Note about `fetch_limit`:

- The node server passes this value to the SensorPush `/samples` API.
- The current node logic only uses the newest returned sample for each sensor’s live values.
- `GV1` on the controller reflects the total number of returned samples, so higher values mainly affect that total and the raw sample payload, not the live sensor readings.

## Sensor Offline Alerts

This node server can alert when a sensor appears to be offline.

- A sensor is considered offline when its latest sample time is older than `sensor_offline_hours`.
- If the sample payload has no timestamp field, receipt of a sample during a poll is treated as fresh.
- `ST` for that sensor is set to `Disconnected` while offline.
- `GV2` on the controller reports total offline sensors.
- ntfy notifications are sent once when offline is detected, and optionally once on recovery.

Gateway offline alerts use the same ntfy topic and recovery setting, but are based on the gateway online/offline status returned by the SensorPush cloud API rather than sample age.

Example custom params for a 24-hour offline alert to ntfy:

- `sensor_offline_hours=24`
- `sensor_offline_notify_recovery=true`
- `ntfy_topic=my-home-sensors-0000`
- `ntfy_server=https://ntfy.sh`

## Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Repo

Planned GitHub remote:

- https://github.com/awysocki/SensorPush.git

## Next Steps

- Add robust token refresh handling using refresh token endpoint.
- Add BLE mode integration.
