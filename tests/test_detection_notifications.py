import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QMetaType, QObject, QSettings, pyqtClassInfo, pyqtSlot
from PyQt6.QtDBus import QDBusArgument, QDBusConnection, QDBusMessage
from PyQt6.QtWidgets import QApplication

from app import (
    NOTIFICATION_DEFAULT_ACTION, NOTIFICATION_DEFAULT_LABEL, NOTIFICATIONS_PATH, NOTIFICATIONS_SERVICE,
    DesktopNotifier, MainWindow,
)


@pyqtClassInfo("D-Bus Interface", NOTIFICATIONS_SERVICE)
class NotificationServer(QObject):
    def __init__(self, bus: QDBusConnection) -> None:
        super().__init__()
        self.bus = bus
        self.calls: list[tuple[str, list[str], str, str]] = []

    @pyqtSlot(str, "uint", str, str, str, "QStringList", "QVariantMap", int, QDBusMessage, result="uint")
    def Notify(self, app_name: str, replaces_id: int, app_icon: str, summary: str, body: str, actions: list[str],
               hints: dict, timeout: int, message: QDBusMessage) -> int:
        self.calls.append((message.signature(), actions, summary, body))
        return len(self.calls)

    def emit(self, name: str, *arguments: object) -> None:
        signal = QDBusMessage.createSignal(NOTIFICATIONS_PATH, NOTIFICATIONS_SERVICE, name)
        signal.setArguments(list(arguments))
        self.bus.send(signal)


BUS_CONFIGURATION = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={directory}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def notification_id(value: int) -> QDBusArgument:
    return QDBusArgument(value, QMetaType.Type.UInt.value)


class DesktopNotifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_bus_test_")
        configuration = Path(self.directory.name) / "bus.conf"
        configuration.write_text(BUS_CONFIGURATION.format(directory=self.directory.name), encoding="utf-8")
        self.daemon = subprocess.Popen(
            ["dbus-daemon", f"--config-file={configuration}", "--nofork", "--nosyslog", "--print-address=1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        address = self.daemon.stdout.readline().strip()
        self.server_bus = QDBusConnection.connectToBus(address, "camera-viewer-test-server")
        self.client_bus = QDBusConnection.connectToBus(address, "camera-viewer-test-client")
        self.server = NotificationServer(self.server_bus)
        self.assertTrue(self.server_bus.registerObject(
            NOTIFICATIONS_PATH, self.server, QDBusConnection.RegisterOption.ExportAllSlots))
        self.assertTrue(self.server_bus.registerService(NOTIFICATIONS_SERVICE))
        self.notifier = DesktopNotifier(bus=self.client_bus)

    def tearDown(self) -> None:
        self.notifier.deleteLater()
        QDBusConnection.disconnectFromBus("camera-viewer-test-client")
        QDBusConnection.disconnectFromBus("camera-viewer-test-server")
        self.daemon.terminate()
        self.daemon.wait(timeout=5)
        self.daemon.stdout.close()
        self.directory.cleanup()

    def wait_until(self, condition: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.01)
        self.assertTrue(condition())

    def test_click_on_notification_runs_its_own_action_once(self) -> None:
        first, second, fallback = Mock(), Mock(), Mock()
        self.notifier.notify("Local camera detection", "Garden · person", 15000, first, fallback)
        self.notifier.notify("Local camera detection", "Entrance · animal", 15000, second, fallback)
        self.wait_until(lambda: len(self.notifier.actions) == 2)
        self.assertEqual(self.server.calls[0], (
            "susssasa{sv}i", [NOTIFICATION_DEFAULT_ACTION, NOTIFICATION_DEFAULT_LABEL],
            "Local camera detection", "Garden · person",
        ))
        self.server.emit("ActionInvoked", notification_id(2), NOTIFICATION_DEFAULT_ACTION)
        self.wait_until(lambda: second.called)
        self.server.emit("ActionInvoked", notification_id(2), NOTIFICATION_DEFAULT_ACTION)
        self.server.emit("ActionInvoked", notification_id(1), NOTIFICATION_DEFAULT_ACTION)
        self.wait_until(lambda: first.called)
        second.assert_called_once_with()
        first.assert_called_once_with()
        fallback.assert_not_called()

    def test_closed_or_foreign_notifications_do_not_run_actions(self) -> None:
        activate = Mock()
        self.notifier.notify("Camera detection", "Garden", 15000, activate, Mock())
        self.wait_until(lambda: list(self.notifier.actions) == [1])
        self.server.emit("ActionInvoked", notification_id(8), NOTIFICATION_DEFAULT_ACTION)
        self.server.emit("ActionInvoked", notification_id(1), "dismiss")
        self.server.emit("NotificationClosed", notification_id(1), notification_id(2))
        self.wait_until(lambda: not self.notifier.actions)
        self.server.emit("ActionInvoked", notification_id(1), NOTIFICATION_DEFAULT_ACTION)
        for _ in range(20):
            self.application.processEvents()
            time.sleep(.01)
        activate.assert_not_called()

    def test_failed_desktop_notification_uses_the_tray_message(self) -> None:
        self.server_bus.unregisterService(NOTIFICATIONS_SERVICE)
        fallback = Mock()
        self.notifier.notify("Camera detection", "Garden", 15000, Mock(), fallback)
        self.wait_until(lambda: fallback.called)
        fallback.assert_called_once_with()
        disconnected = DesktopNotifier(bus=Mock(isConnected=Mock(return_value=False)))
        disconnected_fallback = Mock()
        disconnected.notify("Camera detection", "Garden", 15000, Mock(), disconnected_fallback)
        disconnected_fallback.assert_called_once_with()


class TrayMessageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_tray_message_click_opens_the_latest_tray_detection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_notification_test_") as directory:
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            with patch("app.QSettings", return_value=settings), patch("app.QTimer.singleShot"):
                window = MainWindow()
            if window.tray is None:
                window.create_tray()
            activate = Mock()
            window.tray.showMessage = Mock()
            window.notifier.notify = lambda title, message, timeout, action, fallback: fallback()
            window.notify_detection("Local camera detection", "Garden · person", activate)
            window.tray.showMessage.assert_called_once()
            window.tray.messageClicked.emit()
            activate.assert_called_once_with()
            window.quit_requested = True
            window.close()


if __name__ == "__main__":
    unittest.main()
