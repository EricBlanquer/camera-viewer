import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import v380
from app import (
    MPV_SOFTWARE_DECODE_OPTION, MPV_VAAPI_DECODE_OPTION, RTSP_DENOISE_FILTER, RTSP_VAAPI_DENOISE_FILTER,
    V380_PROVIDER, RtspCamera, mpv_rtsp_command, v380_lens_uid,
)

SOCKET = Path("/tmp/denoise-test.sock")
RENDER_NODE = "/dev/dri/renderD128"


class LiveDenoiseTest(unittest.TestCase):
    def test_live_view_denoises_on_the_intel_graphics_when_a_render_node_works(self) -> None:
        camera = RtspCamera("rtsp:garden", "Jardin", "rtsp://192.0.2.10:554/stream")
        with patch("app.hardware_decode_device", return_value=RENDER_NODE):
            command = mpv_rtsp_command(SOCKET, 7, True, camera)
        self.assertIn(MPV_VAAPI_DECODE_OPTION, command)
        self.assertNotIn(MPV_SOFTWARE_DECODE_OPTION, command)
        self.assertIn(RTSP_VAAPI_DENOISE_FILTER, command)
        self.assertNotIn(RTSP_DENOISE_FILTER, command)
        self.assertEqual(RTSP_VAAPI_DENOISE_FILTER, "--vf=lavfi=[denoise_vaapi=denoise=48]")
        self.assertNotIn("hqdn3d", " ".join(command))
        self.assertEqual(command[-1], camera.url)

    def test_live_view_keeps_the_software_denoise_without_a_usable_render_node(self) -> None:
        camera = RtspCamera("rtsp:garden", "Jardin", "rtsp://192.0.2.10:554/stream")
        with patch("app.hardware_decode_device", return_value=None):
            command = mpv_rtsp_command(SOCKET, 7, True, camera)
        self.assertIn(MPV_SOFTWARE_DECODE_OPTION, command)
        self.assertNotIn(MPV_VAAPI_DECODE_OPTION, command)
        self.assertEqual(MPV_SOFTWARE_DECODE_OPTION, "--hwdec=auto-safe")
        self.assertIn(RTSP_DENOISE_FILTER, command)
        self.assertNotIn(RTSP_VAAPI_DENOISE_FILTER, command)
        self.assertIn("hqdn3d=4:3:6:4.5", " ".join(command))
        self.assertEqual(command[-1], camera.url)

    def test_hardware_live_view_leaves_the_v380_lens_crop_untouched(self) -> None:
        camera = RtspCamera(
            v380_lens_uid(42, 1), "Cour 2", "", provider=V380_PROVIDER, device_uid="rtsp:v380-42", lens=1,
        )
        crop = f"--vf=lavfi=[crop=iw:ih/{v380.LENS_COUNT}:0:ih/{v380.LENS_COUNT}*1]"
        bridge = SimpleNamespace(url="rtsp://127.0.0.1:8554/v380-42")
        for device, decode in ((RENDER_NODE, MPV_VAAPI_DECODE_OPTION), (None, MPV_SOFTWARE_DECODE_OPTION)):
            with self.subTest(device=device), \
                    patch("app.hardware_decode_device", return_value=device), \
                    patch("app.v380.peek_bridge", return_value=bridge):
                command = mpv_rtsp_command(SOCKET, 7, True, camera)
            self.assertIn(decode, command)
            self.assertIn(crop, command)
            self.assertEqual(command[-1], bridge.url)
            # The crop is the last --vf, so it still decides the filter chain.
            self.assertEqual([option for option in command if option.startswith("--vf=")][-1], crop)


if __name__ == "__main__":
    unittest.main()
