from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

MIN_PYTHON = (3, 12)


def _ensure_supported_python() -> None:
    if sys.version_info < MIN_PYTHON:
        major, minor = MIN_PYTHON
        current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        raise SystemExit(
            f"VMHandy requires Python {major}.{minor}+; current interpreter is {current}. "
            f"Run it with python{major}.{minor}."
        )


def main() -> int:
    _ensure_supported_python()
    from ui import VmHandyWindow

    app = QApplication(sys.argv)
    window = VmHandyWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
