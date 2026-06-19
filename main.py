from __future__ import annotations

import logging

import udi_interface
from nodes.controller import SensorPushController

LOGGER = udi_interface.LOGGER


def _dedupe_logger_handlers(logger: logging.Logger) -> None:
    seen: set[tuple[str, str]] = set()
    for handler in list(logger.handlers):
        destination = ""
        if hasattr(handler, "baseFilename"):
            destination = str(getattr(handler, "baseFilename", ""))
        elif hasattr(handler, "stream"):
            destination = repr(getattr(handler, "stream", ""))

        key = (handler.__class__.__name__, destination)
        if key in seen:
            logger.removeHandler(handler)
        else:
            seen.add(key)

    # If this logger has its own handlers, avoid duplicate emission via parents.
    if logger.handlers:
        logger.propagate = False


def _dedupe_all_loggers() -> None:
    _dedupe_logger_handlers(logging.getLogger())
    _dedupe_logger_handlers(logging.getLogger("udi_interface"))
    _dedupe_logger_handlers(LOGGER)

    # udi_interface defines multiple child loggers; de-dupe each one explicitly.
    manager = logging.root.manager
    for name, obj in manager.loggerDict.items():
        if name.startswith("udi_interface") and isinstance(obj, logging.Logger):
            _dedupe_logger_handlers(obj)


def _register_admin_params(polyglot: udi_interface.Interface) -> None:
    typed_params = udi_interface.Custom(polyglot, "customtypedparams")
    typed_params.load(
        [
            {
                "name": "sensorpush_email",
                "title": "SensorPush Email (Optional Fallback)",
                "desc": "Optional. Used only if token-as-apiId is rejected by SensorPush; then authorize retries with email + token credential.",
                "isRequired": False,
            },
            {
                "name": "sensorpush_account_token",
                "title": "SensorPush Account Token",
                "desc": "Required long-lived account token from SensorPush dashboard.",
                "isRequired": True,
            },
            {
                "name": "use_short_poll_updates",
                "title": "Use Short Poll Updates",
                "desc": "Default is false (No/0): set true for 1-minute test updates; false for 5-minute production updates.",
                "default": "0",
                "isRequired": False,
            },
            {
                "name": "sample_limit",
                "title": "Sample Limit",
                "desc": "Number of samples to request per sensor each poll (1-100).",
                "isRequired": False,
            },
        ],
        True,
    )


def main() -> None:
    polyglot = udi_interface.Interface([])
    polyglot.start()
    _dedupe_all_loggers()
    _register_admin_params(polyglot)
    polyglot.setCustomParamsDoc()

    controller = SensorPushController(polyglot)
    polyglot.subscribe(polyglot.START, controller.start)
    polyglot.subscribe(polyglot.POLL, controller.poll)
    polyglot.subscribe(polyglot.CUSTOMPARAMS, controller.custom_params_changed)
    custom_typed_data_event = getattr(polyglot, "CUSTOMTYPEDDATA", None)
    if custom_typed_data_event is not None:
        polyglot.subscribe(custom_typed_data_event, controller.custom_typed_data_changed)
    polyglot.addNode(controller, conn_status=True)

    polyglot.ready()
    polyglot.runForever()


if __name__ == "__main__":
    main()
