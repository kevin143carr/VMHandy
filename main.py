from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
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


def _app_icon_path() -> Path | None:
    base_dir = Path(__file__).resolve().parent
    candidates = [
        base_dir / "assets" / "vmhandy.icns",
        base_dir / "vmhandy.icns",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    _ensure_supported_python()
    from ui import VmHandyWindow

    app = QApplication(sys.argv)
    icon_path = _app_icon_path()
    if icon_path is not None:
        icon = QIcon(str(icon_path))
        app.setWindowIcon(icon)
    window = VmHandyWindow()
    if icon_path is not None:
        window.setWindowIcon(app.windowIcon())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
