import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QObject, QSettings, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QComboBox, QDialog, QDialogButtonBox, QLabel, QLineEdit

from app import ACCOUNT_REJECTED_MESSAGE, MainWindow, RtspCamera


class FakeAccountWorker(QObject):
    devices_found = pyqtSignal(list)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, username: str, password: str) -> None:
        super().__init__()
        self.username = username
        self.password = password

    def start(self) -> None:
        pass


class OnboardingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot") as scheduled:
            self.window = MainWindow()
        self.initial_action = scheduled.call_args.args[1]

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def test_first_launch_offers_camera_sources_without_forcing_okam(self) -> None:
        def choose_rtsp(dialog: QDialog) -> int:
            sources = dialog.findChild(QComboBox)
            self.assertIsNotNone(sources)
            self.assertEqual([sources.itemText(index) for index in range(sources.count())], [
                "O-KAM account", "RTSP camera", "Imou Life camera (local)",
            ])
            self.assertTrue(self.window.isVisible())
            sources.setCurrentIndex(1)
            dialog.findChild(QDialogButtonBox).accepted.emit()
            return dialog.result()
        with patch.object(QDialog, "exec", choose_rtsp), patch.object(self.window, "add_rtsp_camera") as rtsp, patch.object(
            self.window, "change_account"
        ) as okam:
            self.initial_action()
        rtsp.assert_called_once_with()
        okam.assert_not_called()

    def test_initial_imou_choice_opens_the_local_camera_form(self) -> None:
        def choose_imou(dialog: QDialog) -> int:
            dialog.findChild(QComboBox).setCurrentIndex(2)
            dialog.findChild(QDialogButtonBox).accepted.emit()
            return dialog.result()
        with patch.object(QDialog, "exec", choose_imou), patch.object(self.window, "add_imou_camera") as imou:
            self.initial_action()
        imou.assert_called_once_with()

    def test_initial_okam_choice_opens_the_account_form(self) -> None:
        def choose_okam(dialog: QDialog) -> int:
            dialog.findChild(QDialogButtonBox).accepted.emit()
            return dialog.result()
        with patch.object(QDialog, "exec", choose_okam), patch.object(self.window, "change_account") as okam:
            self.initial_action()
        okam.assert_called_once_with()

    def test_canceling_initial_setup_does_not_open_a_provider_form(self) -> None:
        with patch.object(QDialog, "exec", return_value=QDialog.DialogCode.Rejected), patch.object(
            self.window, "change_account"
        ) as okam:
            self.initial_action()
        okam.assert_not_called()

    def test_saved_rtsp_camera_uses_discovery_at_startup(self) -> None:
        self.window.rtsp_cameras = [RtspCamera("rtsp:test", "Entrance", "rtsp://192.0.2.10:554/stream")]
        self.window.save_rtsp_cameras()
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot") as scheduled:
            window = MainWindow()
        self.assertEqual(scheduled.call_args.args[1], window.find_cameras)
        window.quit_requested = True
        window.close()

    def test_rejected_saved_login_opens_the_hidden_window_and_displays_error(self) -> None:
        self.window.hide()
        self.window.on_account_failed(ACCOUNT_REJECTED_MESSAGE)
        self.window.on_account_finished()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window.account_error.isVisible())
        self.assertEqual(self.window.account_error.text(), ACCOUNT_REJECTED_MESSAGE)
        self.assertFalse(self.window.reconnect_timer.isActive())

    def test_invalid_input_stays_in_login_form_without_starting_lookup(self) -> None:
        def submit_empty(dialog: QDialog) -> int:
            dialog.findChild(QDialogButtonBox).accepted.emit()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertTrue(any("Enter your O-KAM account and password." == label.text() for label in dialog.findChildren(QLabel)))
            return dialog.result()
        with patch.object(QDialog, "exec", submit_empty), patch.object(self.window, "start_account_lookup") as lookup:
            self.window.change_account()
        lookup.assert_not_called()

    def test_failed_login_keeps_form_open_and_allows_password_correction(self) -> None:
        def submit_login(dialog: QDialog) -> int:
            username, password = dialog.findChildren(QLineEdit)
            username.setText("test@example.com")
            password.setText("wrong-password")
            buttons = dialog.findChild(QDialogButtonBox)
            buttons.accepted.emit()
            self.assertFalse(buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
            worker = self.window.account_worker
            worker.failed.emit(ACCOUNT_REJECTED_MESSAGE)
            worker.finished.emit()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertTrue(any(label.text() == ACCOUNT_REJECTED_MESSAGE for label in dialog.findChildren(QLabel)))
            self.assertTrue(buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
            password.setText("correct-password")
            buttons.accepted.emit()
            self.assertEqual(self.window.account_worker.password, "correct-password")
            self.window.account_worker.devices_found.emit([])
            self.window.account_worker.finished.emit()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            return dialog.result()
        with patch("app.AccountWorker", FakeAccountWorker), patch("app.save_account_password", return_value=True) as save, patch.object(
            QDialog, "exec", submit_login
        ):
            self.window.change_account()
        save.assert_called_once_with("test@example.com", "correct-password")

    def test_login_failure_remains_visible_in_the_real_modal_event_loop(self) -> None:
        observed: list[tuple[bool, str, bool]] = []

        def inspect_failure() -> None:
            dialog = self.window.account_dialog
            observed.append((dialog.isVisible(), dialog.error.text(), dialog.password.hasFocus()))
            dialog.reject()

        def submit_login() -> None:
            dialog = self.window.account_dialog
            dialog.username.setText("test@example.com")
            dialog.password.setText("wrong-password")
            dialog.buttons.accepted.emit()
            self.window.account_worker.failed.emit(ACCOUNT_REJECTED_MESSAGE)
            self.window.account_worker.finished.emit()
            QTimer.singleShot(0, inspect_failure)

        QTimer.singleShot(0, submit_login)
        with patch("app.AccountWorker", FakeAccountWorker):
            self.window.change_account()
        self.assertEqual(observed, [(True, ACCOUNT_REJECTED_MESSAGE, True)])


if __name__ == "__main__":
    unittest.main()
