import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QProcess, QSettings
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from app import (
    CONTINUOUS_SETTING, HIDDEN_PAUSE_DELAY_MS, HIDDEN_PAUSE_STATUS, CameraPreview, HostPresence, MainWindow,
    RtspCamera, STANDBY_HOST_MESSAGE, STANDBY_HOST_SETTING, StandbyHostDialog, valid_host,
)

HOST = "192.0.2.5"
STATUS = "Standby while 192.0.2.5 is on."
PROBE = ("ping", ["-n", "-q", "-c", "1", "-w", "3", "--", HOST])
ANSWERED = (0, QProcess.ExitStatus.NormalExit)
MISSED = (1, QProcess.ExitStatus.NormalExit)


class StandbyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        self.probe_start = patch.object(self.window.standby_probe, "start").start()
        self.preview_start = patch.object(CameraPreview, "start").start()
        self.garden = SimpleNamespace(name="Jardin", uid="garden")
        self.entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        self.window.devices = [self.garden, self.entrance]
        self.window.selected_device = self.garden
        self.window.sync_previews()
        self.preview = self.window.previews[self.entrance.uid]

    def tearDown(self) -> None:
        self.window.stream_worker = None
        self.window.quit_requested = True
        self.window.close()
        patch.stopall()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def enter_standby(self) -> None:
        self.settings.setValue(STANDBY_HOST_SETTING, HOST)
        self.window.apply_standby_host()

    def test_presence_starts_at_the_first_answer_and_ends_after_three_missed_probes(self) -> None:
        presence = HostPresence()
        answers = (False, True, False, False, True, False, False, False, False, True)
        self.assertEqual([presence.observe(answer) for answer in answers],
                         [False, True, True, True, True, True, True, False, False, True])

    def test_host_is_a_name_or_an_ip_address(self) -> None:
        for host in ("192.168.1.20", "laptop", "laptop.local", "a-b.example.org", "fe80::1"):
            self.assertTrue(valid_host(host), host)
        for host in ("", "-c", "-bad.host", "bad-.host", "a..b", "host name", "host;reboot", "http://host", "x" * 64):
            self.assertFalse(valid_host(host), host)

    def test_dialog_keeps_an_invalid_address_and_accepts_an_empty_one(self) -> None:
        dialog = StandbyHostDialog(self.window, HOST)
        self.assertEqual(dialog.host(), HOST)
        dialog.address.setText("-c 5")
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.message.text(), STANDBY_HOST_MESSAGE)
        dialog.address.setText("  ")
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.host(), "")
        dialog.deleteLater()

    def test_tray_action_saves_the_host_and_starts_standby(self) -> None:
        def accept(dialog: StandbyHostDialog) -> QDialog.DialogCode:
            dialog.address.setText(f" {HOST} ")
            return QDialog.DialogCode.Accepted
        with patch.object(self.window, "exec_camera_dialog", side_effect=accept):
            self.window.configure_standby()
        self.assertEqual(self.settings.value(STANDBY_HOST_SETTING, "", str), HOST)
        self.assertTrue(self.window.standby)
        self.probe_start.assert_called_once_with(*PROBE)

    def test_standby_closes_live_connections_until_the_host_stops_answering(self) -> None:
        self.window.stream_worker = worker = Mock()
        self.preview.worker = preview_worker = Mock()
        self.preview_start.reset_mock()
        self.enter_standby()
        self.assertTrue(self.window.standby)
        self.probe_start.assert_called_once_with(*PROBE)
        self.assertTrue(self.window.standby_timer.isActive())
        worker.stop.assert_called_once_with()
        preview_worker.stop.assert_called_once_with()
        self.assertEqual(self.preview.status_overlay.label.text(), STATUS)
        self.window.on_stream_finished()
        self.preview._on_finished()
        self.assertEqual(self.window.status_text, STATUS)
        self.assertEqual(self.preview.status_overlay.label.text(), STATUS)
        self.assertFalse(self.preview.retry_timer.isActive())
        self.assertFalse(self.window.reconnect_timer.isActive())
        with patch.object(self.window, "start_player") as start_player, patch("app.DetectionWorker") as detections:
            self.window.reconnect()
            self.window.check_detections()
            start_player.assert_not_called()
            detections.assert_not_called()
        self.assertEqual(self.window.status_text, STATUS)
        with patch.object(self.window, "reconnect") as reconnect:
            self.window.on_standby_probe_finished(*ANSWERED)
            self.window.on_standby_probe_finished(*MISSED)
            self.window.on_standby_probe_finished(*MISSED)
            self.assertTrue(self.window.standby)
            reconnect.assert_not_called()
            self.preview_start.assert_not_called()
            self.window.on_standby_probe_finished(*MISSED)
            self.assertFalse(self.window.standby)
            reconnect.assert_called_once_with()
            self.preview_start.assert_called_once_with()
        self.assertIsNone(self.preview.suspended_status)

    def test_pane_added_during_standby_stays_disconnected(self) -> None:
        self.enter_standby()
        self.preview_start.reset_mock()
        kitchen = RtspCamera("rtsp:kitchen", "Cuisine", "rtsp://192.0.2.11:554/live")
        self.window.devices.append(kitchen)
        self.window.sync_previews()
        self.assertEqual(self.window.previews[kitchen.uid].suspended_status, STATUS)
        self.preview_start.assert_not_called()

    def test_suspended_pane_starts_no_connection_until_it_resumes(self) -> None:
        patch.stopall()
        preview = CameraPreview(self.entrance)
        preview.set_suspended(STATUS)
        with patch("app.subprocess.Popen") as player:
            preview.start()
            player.assert_not_called()
        self.assertIsNone(preview.worker)
        preview._on_finished()
        self.assertFalse(preview.retry_timer.isActive())
        self.assertEqual(preview.status_overlay.label.text(), STATUS)
        with patch.object(CameraPreview, "start") as start:
            preview.set_suspended(None)
            start.assert_called_once_with()
        preview.close()

    def test_leaving_standby_reconnects_after_a_stream_that_is_still_stopping(self) -> None:
        self.window.stream_worker = Mock()
        self.enter_standby()
        self.window.on_standby_probe_finished(*MISSED)
        self.assertFalse(self.window.standby)
        self.assertFalse(self.window.reconnect_timer.isActive())
        self.window.on_stream_finished()
        self.assertTrue(self.window.reconnect_timer.isActive())

    def test_probe_that_cannot_start_counts_as_a_missed_answer(self) -> None:
        self.enter_standby()
        with patch.object(self.window, "reconnect") as reconnect:
            self.window.on_standby_probe_error(QProcess.ProcessError.Crashed)
            self.assertTrue(self.window.standby)
            self.window.on_standby_probe_error(QProcess.ProcessError.FailedToStart)
            self.assertFalse(self.window.standby)
            reconnect.assert_called_once_with()

    def test_clearing_the_host_ends_standby_and_stops_probing(self) -> None:
        self.enter_standby()
        self.settings.setValue(STANDBY_HOST_SETTING, "")
        with patch.object(self.window, "reconnect") as reconnect:
            self.window.apply_standby_host()
            reconnect.assert_called_once_with()
        self.assertFalse(self.window.standby)
        self.assertFalse(self.window.standby_timer.isActive())
        self.probe_start.assert_called_once_with(*PROBE)

    def test_camera_playback_continues_through_standby_changes(self) -> None:
        self.window.replay = Mock()
        self.window.set_status("Playback")
        with patch.object(self.window, "stop_stream") as stop_stream, patch.object(self.window, "reconnect") as reconnect:
            self.enter_standby()
            self.assertTrue(self.window.standby)
            self.assertEqual(self.window.status_text, "Playback")
            self.window.on_standby_probe_finished(*MISSED)
            self.assertFalse(self.window.standby)
            stop_stream.assert_not_called()
            reconnect.assert_not_called()
        self.window.replay = None

    def test_hidden_window_without_recording_closes_connections_until_it_is_displayed(self) -> None:
        self.settings.setValue(CONTINUOUS_SETTING, False)
        self.window.stream_worker = worker = Mock()
        self.preview.worker = preview_worker = Mock()
        self.preview_start.reset_mock()
        self.window.update_hidden_pause()
        self.assertTrue(self.window.hidden_pause_timer.isActive())
        self.assertEqual(self.window.hidden_pause_timer.interval(), HIDDEN_PAUSE_DELAY_MS)
        self.assertFalse(self.window.hidden_pause)
        worker.stop.assert_not_called()
        self.window.hidden_pause_timer.timeout.emit()
        self.assertTrue(self.window.hidden_pause)
        worker.stop.assert_called_once_with()
        preview_worker.stop.assert_called_once_with()
        self.assertEqual(self.preview.status_overlay.label.text(), HIDDEN_PAUSE_STATUS)
        self.window.on_stream_finished()
        self.assertEqual(self.window.status_text, HIDDEN_PAUSE_STATUS)
        self.assertFalse(self.window.reconnect_timer.isActive())
        with patch.object(self.window, "start_player") as start_player, patch("app.DetectionWorker") as detections:
            self.window.reconnect()
            self.window.check_detections()
            start_player.assert_not_called()
            detections.assert_not_called()
        with patch.object(self.window, "reconnect") as reconnect:
            self.window.show()
            self.application.processEvents()
            self.assertFalse(self.window.hidden_pause)
            reconnect.assert_called_with()
            self.preview_start.assert_called_with()
        self.assertIsNone(self.preview.suspended_status)
        with patch.object(self.window, "reconnect"):
            self.window.showMinimized()
            self.application.processEvents()
        self.assertTrue(self.window.hidden_pause_timer.isActive())

    def test_window_displayed_again_before_the_delay_keeps_its_connections(self) -> None:
        self.settings.setValue(CONTINUOUS_SETTING, False)
        self.window.stream_worker = worker = Mock()
        self.window.show()
        self.application.processEvents()
        self.window.hide()
        self.assertTrue(self.window.hidden_pause_timer.isActive())
        self.window.show()
        self.application.processEvents()
        self.assertFalse(self.window.hidden_pause_timer.isActive())
        self.window.pause_hidden_window()
        self.assertFalse(self.window.hidden_pause)
        worker.stop.assert_not_called()

    def test_continuous_recording_keeps_hidden_connections_open(self) -> None:
        self.window.update_hidden_pause()
        self.assertFalse(self.window.hidden_pause_timer.isActive())
        self.window.set_continuous_recording(False)
        self.window.hidden_pause_timer.timeout.emit()
        self.assertTrue(self.window.hidden_pause)
        with patch.object(self.window, "reconnect") as reconnect:
            self.window.set_continuous_recording(True)
            reconnect.assert_called_once_with()
        self.assertFalse(self.window.hidden_pause)
        self.assertIsNone(self.preview.suspended_status)

    def test_standby_status_takes_precedence_over_the_hidden_window_pause(self) -> None:
        self.settings.setValue(CONTINUOUS_SETTING, False)
        self.window.set_hidden_pause(True)
        self.enter_standby()
        self.assertEqual(self.preview.status_overlay.label.text(), STATUS)
        with patch.object(self.window, "reconnect") as reconnect:
            self.window.set_standby(False)
            reconnect.assert_not_called()
        self.assertEqual(self.preview.suspended_status, HIDDEN_PAUSE_STATUS)

    def test_startup_waits_for_the_first_probe(self) -> None:
        self.settings.setValue(STANDBY_HOST_SETTING, HOST)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"), \
                patch("app.QProcess") as process:
            process.return_value.state.return_value = process.ProcessState.NotRunning
            window = MainWindow()
            self.assertTrue(window.standby)
            self.assertEqual(window.status_text, STATUS)
            process.return_value.start.assert_called_once_with(*PROBE)
            window.quit_requested = True
            window.close()


if __name__ == "__main__":
    unittest.main()
