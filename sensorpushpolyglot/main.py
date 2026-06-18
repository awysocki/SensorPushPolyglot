from __future__ import annotations

import udi_interface

from sensorpushpolyglot.nodes.controller import SensorPushController

LOGGER = udi_interface.LOGGER


def main() -> None:
    polyglot = udi_interface.Interface([])
    polyglot.start()

    controller = SensorPushController(polyglot)
    polyglot.subscribe(polyglot.START, controller.start)
    polyglot.subscribe(polyglot.CUSTOMPARAMS, controller.custom_params_changed)
    polyglot.addNode(controller, conn_status=True)

    polyglot.ready()
    polyglot.runForever()


if __name__ == "__main__":
    main()
