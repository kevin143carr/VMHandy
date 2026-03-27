from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

MIN_PYTHON = (3, 12)
SPLASH_MINIMUM_MS = 4000


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


def _build_splash_pixmap(icon_path: Path | None) -> QPixmap:
    pixmap = QPixmap(620, 300)
    pixmap.fill(QColor("#f5f3ee"))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillRect(0, 0, 620, 300, QColor("#f5f3ee"))
    painter.fillRect(0, 0, 620, 8, QColor("#c66b3d"))

    if icon_path is not None:
        icon = QIcon(str(icon_path))
        icon.paint(painter, 28, 40, 176, 176)

    title_font = QFont()
    title_font.setPointSize(24)
    title_font.setBold(True)
    painter.setFont(title_font)
    painter.setPen(QColor("#1f1a17"))
    painter.drawText(232, 98, "VMHandy")

    subtitle_font = QFont()
    subtitle_font.setPointSize(12)
    painter.setFont(subtitle_font)
    painter.setPen(QColor("#4b423b"))
    painter.drawText(232, 138, "Move virtual machine bundles between")
    painter.drawText(232, 162, "external and local storage with less friction.")

    author_font = QFont()
    author_font.setPointSize(11)
    author_font.setBold(True)
    painter.setFont(author_font)
    painter.setPen(QColor("#7a3b1d"))
    painter.drawText(232, 210, "Author: Kevin Carr")

    footer_font = QFont()
    footer_font.setPointSize(10)
    painter.setFont(footer_font)
    painter.setPen(QColor("#6b625b"))
    painter.drawText(42, 266, "Parallels and VMware Fusion utility for macOS")
    painter.end()
    return pixmap


def _show_startup_splash(app: QApplication, window: QWidget, icon_path: Path | None) -> QDialog:
    available_families = set(QFontDatabase.families())
    title_family = "Avenir Next" if "Avenir Next" in available_families else "Helvetica Neue"
    body_family = "Avenir Next" if "Avenir Next" in available_families else "SF Pro Text"

    splash = QDialog(window, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
    splash.setModal(False)
    splash.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
    splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    splash.setFixedSize(620, 300)
    splash.setStyleSheet(
        """
        QDialog {
            background: #f5f3ee;
            border: 1px solid #d3c5b8;
            border-radius: 18px;
        }
        QLabel#title {
            color: #1f1a17;
            font-family: "%s";
            font-size: 36px;
            font-weight: 800;
            letter-spacing: 0.5px;
        }
        QLabel#body {
            color: #4b423b;
            font-family: "%s";
            font-size: 17px;
            font-weight: 500;
        }
        QLabel#author {
            color: #7a3b1d;
            font-family: "%s";
            font-size: 18px;
            font-weight: 900;
            letter-spacing: 0.4px;
        }
        QLabel#footer {
            color: #6b625b;
            font-family: "%s";
            font-size: 13px;
            font-weight: 500;
        }
        """
        % (title_family, body_family, title_family, body_family)
    )
    if icon_path is not None:
        splash.setWindowIcon(QIcon(str(icon_path)))

    layout = QVBoxLayout(splash)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    accent = QWidget(splash)
    accent.setFixedHeight(8)
    accent.setStyleSheet("background: #c66b3d; border: none; border-top-left-radius: 18px; border-top-right-radius: 18px;")
    layout.addWidget(accent)

    content = QWidget(splash)
    content_layout = QHBoxLayout(content)
    content_layout.setContentsMargins(42, 38, 42, 30)
    content_layout.setSpacing(24)

    icon_label = QLabel(content)
    icon_label.setFixedSize(176, 176)
    icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon_pixmap = _build_splash_pixmap(icon_path).copy(28, 40, 176, 176)
    icon_label.setPixmap(icon_pixmap)
    content_layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

    text_layout = QVBoxLayout()
    text_layout.setSpacing(8)

    title_label = QLabel("VMHandy", content)
    title_label.setObjectName("title")
    text_layout.addWidget(title_label)

    line_one = QLabel("Move virtual machine bundles between", content)
    line_one.setObjectName("body")
    text_layout.addWidget(line_one)

    line_two = QLabel("external and local storage with less friction.", content)
    line_two.setObjectName("body")
    text_layout.addWidget(line_two)

    text_layout.addSpacing(16)

    author_label = QLabel("Author: Kevin Carr", content)
    author_label.setObjectName("author")
    text_layout.addWidget(author_label)

    text_layout.addStretch(1)

    footer_label = QLabel("Parallels and VMware Fusion utility for macOS", content)
    footer_label.setObjectName("footer")
    text_layout.addWidget(footer_label)

    content_layout.addLayout(text_layout, 1)
    layout.addWidget(content)

    window_rect = window.frameGeometry()
    splash_rect = splash.frameGeometry()
    splash_rect.moveCenter(window_rect.center())
    splash.move(splash_rect.topLeft())
    splash.show()
    app.processEvents()
    return splash


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
    app.processEvents()
    splash = _show_startup_splash(app, window, icon_path)

    def finish_startup() -> None:
        splash.close()
        window.raise_()
        window.activateWindow()

    QTimer.singleShot(SPLASH_MINIMUM_MS, finish_startup)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
