"""Qt 6 desktop window and Plasma system tray."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSpinBox, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextBrowser, QVBoxLayout, QWidget, QHeaderView,
)

from .client import ClientError, request

AUTOSTART = Path.home() / ".config/autostart/networklimiter.desktop"
DESKTOP = Path("/usr/share/applications/networklimiter.desktop")

STYLES = """
QWidget { background: #0d1422; color: #e5eaf5; font-family: Inter, Noto Sans, sans-serif;
          font-size: 13px; }
QMainWindow, QTabWidget::pane { background: #0d1422; border: 0; }
QLabel#hero { font-size: 27px; font-weight: 800; color: #f6f8ff; }
QLabel#sub { font-size: 12px; color: #8192ad; }
QLabel#status { padding: 11px 15px; background: #182538; border-radius: 9px; color: #7fe5cc; }
QLabel#stat { background: #152137; border: 1px solid #25344c; border-radius: 12px;
              padding: 16px; font-size: 15px; font-weight: 600; }
QTabBar::tab { padding: 12px 20px; color: #8d9eb6; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #66d9ed; border-bottom: 2px solid #39bfdc; }
QPushButton { padding: 10px 15px; background: #21324a; border: 1px solid #344865;
              border-radius: 8px; font-weight: 600; }
QPushButton:hover { background: #2e4260; }
QPushButton#primary { background: #1199be; color: #ffffff; border: 1px solid #26bddb; }
QPushButton#primary:hover { background: #16afcf; }
QPushButton#danger { color: #ffc0c7; border: 1px solid #734051; }
QLineEdit, QSpinBox, QComboBox { padding: 9px; background: #152237;
    border: 1px solid #364d6e; border-radius: 7px; }
QTableWidget { gridline-color: #26364c; background: #111b2b;
    alternate-background-color: #182538; border: 1px solid #28374e;
    border-radius: 9px; selection-background-color: #245b74; }
QHeaderView::section { background: #1b2a3f; color: #aebbd0;
    border: 0; border-bottom: 1px solid #33445e; padding: 10px; }
QTextBrowser { border: 1px solid #28374e; border-radius: 9px; padding: 16px; }
QCheckBox { spacing: 10px; }
QToolTip { color: #fff; background: #1b2a3f; }
"""


def brand_icon():
    image = QPixmap(64, 64)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#109cba"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(3, 3, 58, 58, 15, 15)
    p.setPen(QPen(QColor("#f3fdff"), 5, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    for x1, y1, x2, y2 in [(13, 40, 24, 30), (24, 30, 34, 37), (34, 37, 49, 20)]:
        p.drawLine(x1, y1, x2, y2)
    p.end()
    return QIcon(image)


class RateFields(QWidget):
    def __init__(self, down=100, up=None, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)
        self.download = QSpinBox()
        self.download.setRange(1, 1_000_000)
        self.download.setSuffix(" KiB/s")
        self.download.setValue(down if down is not None else 100)
        self.no_download = QCheckBox("Unlimited download")
        self.no_download.setChecked(down is None)
        self.no_download.toggled.connect(self.download.setDisabled)
        self.download.setEnabled(down is not None)
        self.upload = QSpinBox()
        self.upload.setRange(1, 1_000_000)
        self.upload.setSuffix(" KiB/s")
        self.upload.setValue(up if up is not None else 100)
        self.no_upload = QCheckBox("Unlimited upload")
        self.no_upload.setChecked(up is None)
        self.no_upload.toggled.connect(self.upload.setDisabled)
        self.upload.setEnabled(up is not None)
        layout.addRow("Download maximum", self.download)
        layout.addRow("", self.no_download)
        layout.addRow("Upload maximum", self.upload)
        layout.addRow("", self.no_upload)

    def limits(self):
        return {
            "download_kib": None if self.no_download.isChecked() else self.download.value(),
            "upload_kib": None if self.no_upload.isChecked() else self.upload.value(),
        }


class RuleDialog(QDialog):
    def __init__(self, rule=None, *, admin=False, parent=None, pid_only=False):
        super().__init__(parent)
        self.setWindowTitle("Limit one process" if pid_only else "Application bandwidth rule")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        self.pid_only = pid_only
        initial = rule or {}
        if not pid_only:
            form = QFormLayout()
            self.mode = QComboBox()
            self.mode.addItem("Exact executable path", "exe")
            self.mode.addItem("Process name", "name")
            self.mode.setCurrentIndex(0 if initial.get("mode", "exe") == "exe" else 1)
            self.target = QLineEdit(initial.get("target", ""))
            self.target.setPlaceholderText("/usr/bin/pacman or /opt/brave.com/brave/brave")
            self.scope = QComboBox()
            self.scope.addItem("My processes", "user")
            if admin:
                self.scope.addItem("All users (including root updates)", "system")
            if initial.get("scope") == "system" and admin:
                self.scope.setCurrentIndex(1)
            form.addRow("Match", self.mode)
            form.addRow("Executable / name", self.target)
            form.addRow("Apply to", self.scope)
            layout.addLayout(form)
        self.rates = RateFields(initial.get("download_kib", 100),
                                initial.get("upload_kib"))
        layout.addWidget(self.rates)
        note = QLabel("Each matching network-owning PID gets its own cap. "
                      "Set both directions to unlimited to exempt this application "
                      "from the global default. Browser tabs often share one network-service PID.")
        note.setWordWrap(True)
        note.setObjectName("sub")
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_rule(self):
        result = self.rates.limits()
        if not self.pid_only:
            result.update({
                "mode": self.mode.currentData(),
                "target": self.target.text().strip(),
                "scope": self.scope.currentData(),
            })
        return result


class Window(QMainWindow):
    def __init__(self, tray_start=False):
        super().__init__()
        self.setWindowTitle("NetworkLimiter")
        self.setWindowIcon(brand_icon())
        self.resize(1000, 720)
        self.setMinimumSize(750, 510)
        self.rules = []
        self.procs = []
        self.admin = False
        self.paused = False
        self.quitting = False
        self._global_last = object()
        self._build()
        self._tray()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(4000)
        if not tray_start or not self.tray.isSystemTrayAvailable():
            self.show()

    def _button(self, text, callback, style=None):
        b = QPushButton(text)
        if style:
            b.setObjectName(style)
        b.clicked.connect(callback)
        return b

    def _table(self, headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        t.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setStretchLastSection(True)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        return t

    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(26, 25, 26, 22)
        lay.setSpacing(16)
        title = QLabel("NETWORK LIMITER")
        title.setObjectName("hero")
        lay.addWidget(title)
        subtitle = QLabel("Per-process bandwidth control  •  CachyOS / Arch  •  KDE Plasma")
        subtitle.setObjectName("sub")
        lay.addWidget(subtitle)
        self.banner = QLabel("Connecting to system service…")
        self.banner.setObjectName("status")
        lay.addWidget(self.banner)
        counters = QHBoxLayout()
        self.count_procs = QLabel("— processes")
        self.count_rules = QLabel("— rules")
        self.count_active = QLabel("— limited")
        for label in (self.count_procs, self.count_rules, self.count_active):
            label.setObjectName("stat")
            counters.addWidget(label)
        lay.addLayout(counters)
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        page = QWidget()
        v = QVBoxLayout(page)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Network-active processes"))
        bar.addStretch()
        bar.addWidget(self._button("Refresh", self.refresh))
        bar.addWidget(self._button("Limit selected PID", self.limit_pid, "primary"))
        bar.addWidget(self._button("Create persistent rule", self.from_process))
        v.addLayout(bar)
        self.process_table = self._table(
            ["Process", "PID", "Owner UID", "Download cap", "Upload cap", "Executable"])
        self.process_table.itemDoubleClicked.connect(lambda _: self.limit_pid())
        v.addWidget(self.process_table)
        v.addWidget(self._button("Clear selected PID override", self.clear_pid))
        tabs.addTab(page, "Live processes")

        global_page = QWidget()
        global_layout = QVBoxLayout(global_page)
        global_header = QLabel("DEFAULT CAP FOR EVERY NETWORK PROCESS")
        global_header.setObjectName("hero")
        global_layout.addWidget(global_header)
        global_info = QLabel(
            "If enabled, each detected network-owning PID receives its own speed limit. "
            "This includes system services, Brave and root-owned package updates. "
            "Saved app rules override this default; an app rule with both directions "
            "unlimited exempts that application. Use cautiously: limiting system "
            "processes can affect connectivity.")
        global_info.setWordWrap(True)
        global_info.setObjectName("sub")
        global_layout.addWidget(global_info)
        self.global_enabled = QCheckBox("Enable a default limit for all processes")
        global_layout.addWidget(self.global_enabled)
        self.global_rates = RateFields(100, None)
        global_layout.addWidget(self.global_rates)
        global_layout.addWidget(self._button("Save global default", self.apply_global, "primary"))
        global_layout.addStretch()
        tabs.addTab(global_page, "Global default")

        rule_page = QWidget()
        v = QVBoxLayout(rule_page)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Automatically apply rules to new processes"))
        bar.addStretch()
        bar.addWidget(self._button("Add rule", self.add_rule, "primary"))
        bar.addWidget(self._button("Edit", self.edit_rule))
        bar.addWidget(self._button("Delete", self.delete_rule, "danger"))
        v.addLayout(bar)
        self.rule_table = self._table(
            ["Target", "Scope", "Download cap", "Upload cap", "Match type"])
        self.rule_table.itemDoubleClicked.connect(lambda _: self.edit_rule())
        v.addWidget(self.rule_table)
        tabs.addTab(rule_page, "Persistent rules")

        settings = QWidget()
        v = QVBoxLayout(settings)
        self.autostart = QCheckBox("Start NetworkLimiter in system tray after login")
        self.autostart.setChecked(AUTOSTART.exists())
        self.autostart.toggled.connect(self.set_autostart)
        v.addWidget(self.autostart)
        self.pause_check = QCheckBox("Pause limits for my user")
        self.pause_check.toggled.connect(self.pause_changed)
        v.addWidget(self.pause_check)
        self.system_pause = QCheckBox("Pause system-wide rules (administrator)")
        self.system_pause.toggled.connect(self.system_pause_changed)
        v.addWidget(self.system_pause)
        info = QTextBrowser()
        info.setOpenExternalLinks(True)
        info.setHtml("""
        <h3>How limits are applied</h3>
        <p>Each network-owning Linux process (PID) is limited independently
        using cgroup/eBPF filtering. 100 KiB/s equals approximately 819 kbit/s.
        If the global default is enabled, it limits every matching network-owning PID; "
        "otherwise unmatched processes remain unlimited.</p>
        <h3>Important limitations</h3>
        <p>The backend is a <b>packet policer</b>, not a smooth traffic shaper.
        It drops packets when limits are exceeded. Actual download throughput may
        fluctuate, especially with QUIC/UDP.</p>
        <p><b>Brave tabs are not independently limited.</b> Chromium often makes
        all requests through a shared Network Service process. A per-PID rule
        limits that shared process, not individual tabs.</p>
        <p>System rules require membership of
        <code>networklimiter-admin</code>. Sign out and back in after adding the group.
        Running the service requires <code>pidnl</code>.</p>
        """)
        v.addWidget(info, 1)
        tabs.addTab(settings, "Settings & help")

    def _tray(self):
        self.tray = QSystemTrayIcon(brand_icon(), self)
        self.tray.setToolTip("NetworkLimiter")
        menu = QMenu()
        menu.addAction("Open NetworkLimiter", self.open_window)
        self.tray_pause = QAction("Pause my limits", self)
        self.tray_pause.setCheckable(True)
        self.tray_pause.triggered.connect(self.tray_pause_changed)
        menu.addAction(self.tray_pause)
        menu.addSeparator()
        menu.addAction("Quit tray (daemon stays active)", self.exit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.open_window()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

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

    @staticmethod
    def cap(value):
        return "Unlimited" if value is None else f"{value:,} KiB/s"

    def invoke(self, op, **data):
        try:
            return request(op, timeout=2.5, **data)
        except ClientError as exc:
            QMessageBox.warning(self, "NetworkLimiter", str(exc))
            return None

    def refresh(self):
        try:
            data = request("status", timeout=1.5)
        except ClientError as exc:
            self.banner.setText(f"Service unavailable: {exc}")
            self.banner.setStyleSheet("color:#ffb0bb")
            return
        self.banner.setStyleSheet("color:#7fe5cc")
        self.banner.setText(
            "● Service connected — cgroup/eBPF active"
            if not data["backend_error"] else f"● Backend warning: {data['backend_error']}")
        self.rules = data["rules"]
        self.procs = data["processes"]
        self.admin = data["admin"]
        self.paused = data["paused"]
        current_global = data.get("global_default")
        if current_global != self._global_last:
            self._global_last = current_global
            self.global_enabled.setChecked(current_global is not None)
            self._set_rate_fields(self.global_rates, current_global or
                                  {"download_kib": 100, "upload_kib": None})
        self.global_enabled.setEnabled(self.admin)
        self.global_rates.setEnabled(self.admin)
        self.count_procs.setText(f"{len(self.procs)} network PIDs")
        self.count_rules.setText(f"{len(self.rules)} saved rules")
        self.count_active.setText(
            f"{sum(1 for p in self.procs if p['limited'])} limited now")
        self._populate(self.process_table, [
            [p["name"], p["pid"], p["uid"], self.cap(p["download_kib"]),
             self.cap(p["upload_kib"]), p["exe"]] for p in self.procs])
        self._populate(self.rule_table, [
            [r["target"], r["scope"], self.cap(r["download_kib"]),
             self.cap(r["upload_kib"]), r["mode"]] for r in self.rules])
        self.pause_check.blockSignals(True)
        self.pause_check.setChecked(self.paused)
        self.pause_check.blockSignals(False)
        self.system_pause.blockSignals(True)
        self.system_pause.setEnabled(self.admin)
        self.system_pause.setChecked(data["paused_system"])
        self.system_pause.blockSignals(False)
        self.tray_pause.blockSignals(True)
        self.tray_pause.setChecked(self.paused)
        self.tray_pause.blockSignals(False)

    @staticmethod
    def _set_rate_fields(fields, values):
        down, up = values.get("download_kib"), values.get("upload_kib")
        fields.no_download.setChecked(down is None)
        fields.no_upload.setChecked(up is None)
        if down is not None:
            fields.download.setValue(down)
        if up is not None:
            fields.upload.setValue(up)

    @staticmethod
    def _populate(table, rows):
        current = table.currentRow()
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                item = QTableWidgetItem(str(value or "—"))
                if isinstance(value, int):
                    item.setData(Qt.ItemDataRole.DisplayRole, value)
                table.setItem(i, j, item)
        if 0 <= current < len(rows):
            table.selectRow(current)

    def selected(self, table, values):
        row = table.currentRow()
        if not 0 <= row < len(values):
            QMessageBox.information(self, "Select an item", "Select a row first.")
            return None
        return values[row]

    def add_rule(self):
        self._rule_dialog()

    def from_process(self):
        proc = self.selected(self.process_table, self.procs)
        if proc:
            self._rule_dialog({"mode": "exe" if proc["exe"] else "name",
                               "target": proc["exe"] or proc["name"]})

    def edit_rule(self):
        rule = self.selected(self.rule_table, self.rules)
        if rule:
            self._rule_dialog(rule)

    def _rule_dialog(self, old=None):
        dialog = RuleDialog(old, admin=self.admin, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        rule = dialog.get_rule()
        if old and old.get("id"):
            rule["id"] = old["id"]
            if old["scope"] != rule["scope"]:
                QMessageBox.warning(self, "Change scope", "Delete and recreate a rule to change scope.")
                return
        if self.invoke("upsert_rule", rule=rule) is not None:
            self.refresh()

    def delete_rule(self):
        rule = self.selected(self.rule_table, self.rules)
        if rule and QMessageBox.question(
            self, "Delete rule", f"Remove limit for {rule['target']}?"
        ) == QMessageBox.StandardButton.Yes:
            if self.invoke("delete_rule", id=rule["id"]) is not None:
                self.refresh()

    def limit_pid(self):
        proc = self.selected(self.process_table, self.procs)
        if not proc:
            return
        dialog = RuleDialog({"download_kib": proc["download_kib"] or 100,
                             "upload_kib": proc["upload_kib"]},
                            parent=self, pid_only=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.invoke("set_pid", pid=proc["pid"], **dialog.get_rule()) is not None:
                self.refresh()

    def clear_pid(self):
        proc = self.selected(self.process_table, self.procs)
        if proc and self.invoke("clear_pid", pid=proc["pid"]) is not None:
            self.refresh()

    def pause_changed(self, checked):
        if self.invoke("pause", enabled=checked, scope="user") is not None:
            self.refresh()

    def tray_pause_changed(self, checked):
        self.pause_changed(checked)

    def apply_global(self):
        if not self.admin:
            QMessageBox.warning(self, "Administrator required",
                                "Join networklimiter-admin and sign out/in first.")
            return
        enabled = self.global_enabled.isChecked()
        data = self.global_rates.limits() if enabled else {}
        if enabled and data["download_kib"] is None and data["upload_kib"] is None:
            QMessageBox.warning(self, "No limit", "Choose at least one global limit.")
            return
        if enabled and self._global_last is None:
            answer = QMessageBox.question(
                self, "Limit system processes too?",
                "This default applies to ALL network processes, including system services. "
                "It may disrupt connectivity. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self.invoke("set_global", enabled=enabled, **data) is not None:
            self.refresh()

    def system_pause_changed(self, checked):
        if self.invoke("pause", enabled=checked, scope="system") is not None:
            self.refresh()

    def set_autostart(self, checked):
        try:
            if checked:
                AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
                if DESKTOP.exists():
                    shutil.copyfile(DESKTOP, AUTOSTART)
                else:
                    AUTOSTART.write_text(
                        "[Desktop Entry]\nType=Application\nName=NetworkLimiter\n"
                        "Exec=networklimiter --tray\nIcon=network-wired\n"
                        "Terminal=false\nX-GNOME-Autostart-enabled=true\n")
            else:
                AUTOSTART.unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Autostart", str(exc))


def main():
    parser = argparse.ArgumentParser(description="NetworkLimiter Qt tray")
    parser.add_argument("--tray", action="store_true",
                        help="Start in system tray without opening a window")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(brand_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(STYLES)
    window = Window(tray_start=args.tray)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
