import os
import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
ACTIVATIONS = 300

SERVER = r"""
import os, subprocess, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, sys.argv[1])
from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QLocalServer
from PyQt6.QtWidgets import QApplication
import app as viewer


class Window:
    shown = 0

    def show_window(self):
        Window.shown += 1
        for _ in range(3):
            QApplication.processEvents()


qt = QApplication(sys.argv)
name = f"okam-instance-test-{os.getpid()}"
QLocalServer.removeServer(name)
server = QLocalServer()
assert server.listen(name)
window = Window()
server.newConnection.connect(lambda: viewer.accept_instance_request(server, window))
client = subprocess.Popen([sys.executable, "-c", sys.argv[2], name, sys.argv[1], sys.argv[3]])
timer = QTimer()
timer.timeout.connect(lambda: client.poll() is not None and qt.quit())
timer.start(50)
qt.exec()
print(Window.shown)
"""

CLIENT = r"""
import sys
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtNetwork import QLocalSocket
sys.path.insert(0, sys.argv[2])
QCoreApplication(sys.argv)
from app import SHOW_WINDOW_REQUEST
for _ in range(int(sys.argv[3])):
    socket = QLocalSocket()
    socket.connectToServer(sys.argv[1])
    if socket.waitForConnected(1000):
        socket.write(SHOW_WINDOW_REQUEST)
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()
"""


class SingleInstanceTest(unittest.TestCase):
    def test_repeated_show_requests_never_crash_the_running_viewer(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", SERVER, str(REPOSITORY), CLIENT, str(ACTIVATIONS)],
            capture_output=True, text=True, timeout=120, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertGreaterEqual(int(result.stdout.strip().splitlines()[-1]), 1)


if __name__ == "__main__":
    unittest.main()
