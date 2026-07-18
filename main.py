from __future__ import annotations

import logging
import re
import threading
import time

import udi_interface
from nodes.controller import SensorPushController

LOGGER = udi_interface.LOGGER


class _ConsecutiveDuplicateFilter(logging.Filter):
    def __init__(self, window_seconds: float = 0.5) -> None:
        super().__init__()
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        self._last_key: tuple[str, str, str] | None = None
        self._last_time = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True

        now = time.monotonic()
        key = (record.name, record.levelname, message)
        with self._lock:
            if self._last_key == key and (now - self._last_time) <= self._window_seconds:
                return False
            self._last_key = key
            self._last_time = now
        return True


class _SensitiveDataFilter(logging.Filter):
    _MASK = "***"
    _PATTERNS = (
        re.compile(r'("sensorpush_password"\s*:\s*")(.*?)(")', re.IGNORECASE),
        re.compile(r'("ntfy_token"\s*:\s*")(.*?)(")', re.IGNORECASE),
        re.compile(r'("password"\s*:\s*")(.*?)(")', re.IGNORECASE),
        re.compile(r'("token"\s*:\s*")(.*?)(")', re.IGNORECASE),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True

        masked = rendered
        for pattern in self._PATTERNS:
            masked = pattern.sub(rf'\1{self._MASK}\3', masked)

        if masked != rendered:
            record.msg = masked
            record.args = ()

        return True


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


def _install_sensitive_log_filter() -> None:
    sensitive_filter = _SensitiveDataFilter()
    duplicate_filter = _ConsecutiveDuplicateFilter()
    targets = [
        logging.getLogger(),
        logging.getLogger("udi_interface"),
        logging.getLogger("udi_interface.interface"),
        LOGGER,
    ]

    for logger in targets:
        logger.addFilter(sensitive_filter)
        logger.addFilter(duplicate_filter)
        for handler in logger.handlers:
            handler.addFilter(sensitive_filter)
            handler.addFilter(duplicate_filter)


def main() -> None:
    # Install redaction before startup so inbound config payload logs never expose secrets.
    _install_sensitive_log_filter()

    polyglot = udi_interface.Interface([])
    polyglot.start()
    _dedupe_all_loggers()
    _set_mqtt_logger_silent()

    controller = SensorPushController(polyglot)
    polyglot.subscribe(polyglot.START, controller.start)
    polyglot.subscribe(polyglot.POLL, controller.poll)

    stop_event = getattr(polyglot, "STOP", None)
    if stop_event is not None:
        polyglot.subscribe(stop_event, controller.stop)

    custom_typed_data_event = getattr(polyglot, "CUSTOMTYPEDDATA", None)
    if custom_typed_data_event is not None:
        polyglot.subscribe(custom_typed_data_event, controller.custom_typed_data_changed)

    polyglot.addNode(controller)

    polyglot.ready()
    polyglot.runForever()


if __name__ == "__main__":
    main()