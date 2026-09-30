import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class NativeVideoGeometryTest(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(command) for command in ("xvfb-run", "mpv", "ffmpeg")),
                         "Native video verification requires Xvfb, mpv and ffmpeg")
    def test_mapped_player_fits_its_host_and_preserves_user_zoom(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="xcb")
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        completed = subprocess.run(["xvfb-run", "-a", sys.executable, __file__, "--native-check"],
                                   env=environment, capture_output=True, text=True, timeout=25)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


def check_native_geometry():
    from PyQt6.QtCore import QEventLoop, QTimer
    from PyQt6.QtWidgets import QApplication
    from app import ControlsOverlay, VideoWidget, mpv_request, stop_mpv_player

    application = QApplication([])
    video = VideoWidget()
    video.resize(490, 276)
    video.set_controls_overlay(ControlsOverlay(video))
    video.show()

    def advance():
        loop = QEventLoop()
        QTimer.singleShot(600, loop.quit)
        loop.exec()

    with tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_geometry_") as directory:
        source = Path(directory) / "video.mp4"
        socket_path = Path(directory) / "player.sock"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
                        "-t", "2", "-c:v", "libx264", "-threads", "1", str(source)], check=True, timeout=10)
        player = subprocess.Popen(["mpv", "--no-config", "--no-terminal", "--really-quiet", "--vo=x11",
                                   "--no-audio", "--loop-file=inf", "--panscan=1",
                                   f"--input-ipc-server={socket_path}", f"--wid={int(video.winId())}", str(source)])
        try:
            advance()
            parent = video.x_display.create_resource_object("window", int(video.winId()))
            child = next(window for window in parent.query_tree().children if window.get_wm_class() == ("x11", "mpv"))
            assert mpv_request(socket_path, ["set_property", "video-zoom", 0.5])[0]
            assert mpv_request(socket_path, ["set_property", "video-pan-x", 0.1])[0]
            for width, height in ((490, 276), (720, 405), (490, 276)):
                video.resize(width, height)
                advance()
                child.unmap()
                child.configure(width=width * 2, height=height * 2)
                child.map()
                video.x_display.flush()
                advance()
                geometry = child.get_geometry()
                assert (geometry.width, geometry.height) == (width, height), (geometry.width, geometry.height)
                dimensions = mpv_request(socket_path, ["get_property", "osd-dimensions"])[1]
                assert (dimensions["w"], dimensions["h"]) == (width, height), dimensions
                assert mpv_request(socket_path, ["get_property", "video-zoom"])[1] == 0.5
                assert mpv_request(socket_path, ["get_property", "video-pan-x"])[1] == 0.1
        finally:
            stop_mpv_player(player)
            video.close()
            application.quit()


if __name__ == "__main__":
    if sys.argv[1:] == ["--native-check"]:
        check_native_geometry()
    else:
        unittest.main()
