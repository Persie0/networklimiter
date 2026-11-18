"""Minimal KDE/Qt6 interface: one toggle and one download speed, for ALL PIDs."""
from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QMessageBox, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from .client import ClientError, request
from .core import DEFAULT_KIB, MAX_KIB

STYLE = """
QWidget { background: #0d1422; color: #e8eef8; font-size: 14px; font-family: sans-serif; }
QLabel#heading { font-size: 29px; font-weight: 800; }
QLabel#subtitle, QLabel#explanation { color: #98a8be; }
QLabel#status { background: #17263b; color: #8bdac7; border: 1px solid #2c415c;
                padding: 12px 14px; border-radius: 9px; }
QLabel#section { font-weight: 650; color: #f4f8ff; font-size: 17px; }
QWidget#controlPanel { background: #16243a; border: 1px solid #32465e; border-radius: 14px; }
QWidget#controlPanel QLabel { background: transparent; }
QCheckBox { background: transparent; spacing: 14px; font-size: 17px; font-weight: 650; }
QCheckBox::indicator { width: 42px; height: 24px; }
QCheckBox::indicator:unchecked { image: none; background: #536578;
    border: 2px solid #8592a6; border-radius: 12px; }
QCheckBox::indicator:checked { image: none; background: #15a8b8;
    border: 2px solid #52e1e6; border-radius: 12px; }
QDoubleSpinBox, QComboBox { background: #0e1b2d; color: #ffffff;
    border: 1px solid #475d76; border-radius: 9px; padding: 12px; font-size: 19px; }
QComboBox { min-width: 95px; font-size: 15px; }
QComboBox QAbstractItemView { background: #18263b; color: #ffffff;
    selection-background-color: #1c7693; }
"""


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#139dbb"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QPen(QColor("white"), 5, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawLine(13, 39, 25, 29)
    p.drawLine(25, 29, 36, 35)
    p.drawLine(36, 35, 51, 19)
    p.end()
    return QIcon(pixmap)


class Window(QMainWindow):
    def __init__(self, *, tray_start=False):
        super().__init__()
        self.setWindowTitle("NetworkLimiter")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(460, 430)
        self.resize(570, 490)
        self.quitting = False
        self.admin = False
        self.enabled = False
        self.backend_rate = DEFAULT_KIB
        self.loading = False
        self.changing_units = False
        self._build()
        self._tray()

        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(650)
        self.save_timer.timeout.connect(self.apply_settings)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(3500)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.refresh()
        if not tray_start or not QSystemTrayIcon.isSystemTrayAvailable():
            self.show()

    def _build(self):
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        heading = QLabel("NETWORK LIMITER")
        heading.setObjectName("heading")
        layout.addWidget(heading)

        subtitle = QLabel("One speed limit for every network process")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)

        panel = QWidget()
        panel.setObjectName("controlPanel")
        controls = QVBoxLayout(panel)
        controls.setContentsMargins(23, 25, 23, 25)
        controls.setSpacing(16)

        self.toggle = QCheckBox("Enable speed limit")
        self.toggle.setToolTip("Limits the download speed of every detected network-owning process")
        self.toggle.toggled.connect(self.on_toggle)
        controls.addWidget(self.toggle)

        speed_label = QLabel("Maximum download speed per process")
        speed_label.setObjectName("section")
        controls.addWidget(speed_label)

        speed_row = QHBoxLayout()
        self.speed = QDoubleSpinBox()
        self.speed.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.PlusMinus)
        self.speed.setKeyboardTracking(False)
        self.units = QComboBox()
        self.units.addItem("MiB/s", 1024)
        self.units.addItem("KiB/s", 1)
        self._show_rate(DEFAULT_KIB)
        self.speed.valueChanged.connect(self.on_speed)
        self.units.currentIndexChanged.connect(self.on_units_changed)
        speed_row.addWidget(self.speed, 1)
        speed_row.addWidget(self.units)
        controls.addLayout(speed_row)

        explanation = QLabel(
            "Each process gets this limit independently, not a share of one total. "
            "Upload speed is unrestricted."
        )
        explanation.setObjectName("explanation")
        explanation.setWordWrap(True)
        controls.addWidget(explanation)
        layout.addWidget(panel)

        self.status = QLabel("Connecting to background service…")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        details = QLabel(
            "Changes are saved automatically. The system tray starts when you log in. "
            "Brave tabs may share one network-service process, so tabs do not receive "
            "independent caps."
        )
        details.setObjectName("explanation")
        details.setWordWrap(True)
        layout.addWidget(details)
        layout.addStretch()

    def _tray(self):
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("NetworkLimiter")
        menu = QMenu(self)
        menu.addAction("Open NetworkLimiter", self.open_window)
        self.tray_toggle = QAction("Enable speed limit", self)
        self.tray_toggle.setCheckable(True)
        self.tray_toggle.triggered.connect(self.on_tray_toggle)
        menu.addAction(self.tray_toggle)
        menu.addSeparator()
        menu.addAction("Quit tray (service stays active)", self.exit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.open_window()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

    def _show_rate(self, rate_kib):
        factor = self.units.currentData()
        self.speed.blockSignals(True)
        self.speed.setDecimals(0 if factor == 1 else 2)
        self.speed.setRange(1 if factor == 1 else 0.01, MAX_KIB / factor)
        self.speed.setSingleStep(10 if factor == 1 else 0.1)
        self.speed.setValue(rate_kib / factor)
        self.speed.blockSignals(False)

    def _selected_rate(self):
        factor = self.units.currentData()
        return max(1, min(MAX_KIB, round(self.speed.value() * factor)))

    def on_units_changed(self, _index):
        # Switching display units does not change the stored per-PID limit.
        if self.loading:
            return
        self.changing_units = True
        current_rate = self.backend_rate
        if self.save_timer.isActive():
            # Preserve the pending input rate when changing the unit.
            current_rate = self._pending_rate
        self._show_rate(current_rate)
        self.changing_units = False

    def on_speed(self, _value):
        if self.loading or self.changing_units or not self.admin:
            return
        self._pending_rate = self._selected_rate()
        self.save_timer.start()

    def on_toggle(self, _checked):
        if self.loading:
            return
        self.save_timer.stop()
        self.apply_settings()

    def on_tray_toggle(self, checked):
        self.toggle.setChecked(checked)

    def apply_settings(self):
        if not self.admin:
            self.status.setText(
                "Administrator permission required. Sign out and back in after installation."
            )
            self.refresh()
            return
        enabled = self.toggle.isChecked()
        rate = getattr(self, "_pending_rate", self._selected_rate())
        try:
            request("set_global", enabled=enabled, download_kib=rate, timeout=3)
        except ClientError as exc:
            QMessageBox.warning(self, "NetworkLimiter", str(exc))
            self.refresh()
            return
        self._pending_rate = rate
        self.backend_rate = rate
        self.enabled = enabled
        self.status.setText(
            "Enabled · applying the selected limit to network-active processes…"
            if enabled else "Disabled · releasing process limits…"
        )
        self.refresh()

    def refresh(self):
        try:
            data = request("status", timeout=2)
        except ClientError as exc:
            self.status.setText(f"Service unavailable: {exc}")
            self.status.setStyleSheet("color: #ffadb9")
            self.toggle.setEnabled(False)
            self.speed.setEnabled(False)
            self.units.setEnabled(False)
            self.tray_toggle.setEnabled(False)
            return

        self.admin = data["admin"]
        self.enabled = data["enabled"]
        self.backend_rate = data["download_kib"]

        self.loading = True
        try:
            self.toggle.setChecked(self.enabled)
            if not self.save_timer.isActive() and not self.speed.hasFocus():
                # Never overwrite a number while someone is typing it.
                self._show_rate(self.backend_rate)
                self._pending_rate = self.backend_rate
            self.tray_toggle.setChecked(self.enabled)
        finally:
            self.loading = False

        self.toggle.setEnabled(self.admin)
        self.speed.setEnabled(self.admin)
        self.units.setEnabled(self.admin)
        self.tray_toggle.setEnabled(self.admin)
        self.status.setStyleSheet("")

        if not self.admin:
            self.status.setText(
                "View only. Sign out and back in after installation to enable the toggle."
            )
        elif data.get("backend_error"):
            self.status.setText(f"Backend error: {data['backend_error']}")
            self.status.setStyleSheet("color: #ffb0b9")
        elif self.enabled:
            self.status.setText(
                f"Enabled · {data['limited_count']} of {data['active_count']} "
                "detected network processes limited"
            )
        else:
            self.status.setText("Disabled · no new speed limits are being applied")

        if self.enabled:
            self.tray.setToolTip(f"NetworkLimiter — {self.backend_rate} KiB/s per process")
        else:
            self.tray.setToolTip("NetworkLimiter — Off")

    def open_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.quitting or not QSystemTrayIcon.isSystemTrayAvailable():
            event.accept()
        else:
            self.hide()
            event.ignore()

    def exit_app(self):
        self.quitting = True
        self.tray.hide()
        QApplication.instance().quit()


def main():
    parser = argparse.ArgumentParser(description="NetworkLimiter system tray")
    parser.add_argument("--tray", action="store_true")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setWindowIcon(app_icon())
    window = Window(tray_start=args.tray)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
