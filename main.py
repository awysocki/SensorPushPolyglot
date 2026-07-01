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


def _set_mqtt_logger_silent() -> None:
    """Keep MQTT driver update messages suppressed by default."""
    logging.getLogger("udi_interface.interface").setLevel(logging.WARNING)


def _register_admin_params(polyglot: udi_interface.Interface) -> None:
    typed_params = udi_interface.Custom(polyglot, "customtypedparams")
    typed_params.load(
        [
            {
                "name": "sensorpush_email",
                "title": "SensorPush Email",
                "desc": "Required SensorPush account email.",
                "isRequired": True,
                "isDelete": True,
            },
            {
                "name": "sensorpush_password",
                "title": "SensorPush Password",
                "desc": "Required SensorPush account password.",
                "isRequired": True,
                "isDelete": True,
            },
            {
                "name": "use_short_poll_updates",
                "title": "Use Short Poll Updates",
                "desc": "Default is false (No/0): set true for 1-minute test updates; false for 5-minute production updates.",
                "default": "0",
                "isRequired": False,
                "isDelete": True,
            },
        ],
        True,
    )


def main() -> None:
    polyglot = udi_interface.Interface([])
    polyglot.start()
    _dedupe_all_loggers()
    _set_mqtt_logger_silent()
    _register_admin_params(polyglot)
    polyglot.setCustomParamsDoc()

    controller = SensorPushController(polyglot)
    polyglot.subscribe(polyglot.START, controller.start)
    polyglot.subscribe(polyglot.POLL, controller.poll)
    stop_event = getattr(polyglot, "STOP", None)
    if stop_event is not None:
        polyglot.subscribe(stop_event, controller.stop)
    polyglot.subscribe(polyglot.CUSTOMPARAMS, controller.custom_params_changed)
    custom_typed_data_event = getattr(polyglot, "CUSTOMTYPEDDATA", None)
    if custom_typed_data_event is not None:
        polyglot.subscribe(custom_typed_data_event, controller.custom_typed_data_changed)
    polyglot.addNode(controller, conn_status=True)

    polyglot.ready()
    polyglot.runForever()


if __name__ == "__main__":
    main()
