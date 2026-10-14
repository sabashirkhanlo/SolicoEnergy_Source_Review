from __future__ import annotations
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from .core.db import init_db
from .core.logging_setup import setup_logging
from .gui.main_window import MainWindow
from .core.windows_shortcuts import refresh_shortcuts


def run_gui():
    setup_logging(); init_db(); app=QApplication(sys.argv); app.setApplicationName('Solico Energy Report'); app.setStyle('Fusion')
    icon_path=Path(__file__).resolve().parent/'assets'/'app_icon.png'
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    try:
        refresh_shortcuts(Path(__file__).resolve().parent/'assets'/'app_icon.ico')
    except Exception:
        pass
    w=MainWindow()
    if icon_path.exists():
        w.setWindowIcon(QIcon(str(icon_path)))
    w.show()
    return app.exec()
