from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path


LOG = logging.getLogger("okam-linux.hardware")
DEVICE_VARIABLE = "CAMERA_VIEWER_VAAPI_DEVICE"
DEVICE_OFF_VALUES = frozenset({"0", "off", "none", "false"})
PROBE_WIDTH = 640
PROBE_HEIGHT = 360
PROBE_FPS = 12
PROBE_TIMEOUT_SECONDS = 20

_HARDWARE_LOCK = threading.Lock()
_HARDWARE_DEVICE: str | None | object = None
_ENCODE_DISABLED = False


def _candidate_render_nodes() -> list[str]:
    override = os.environ.get(DEVICE_VARIABLE, "").strip()
    if override:
        return [] if override.lower() in DEVICE_OFF_VALUES else [override]
    try:
        return sorted(str(node) for node in Path("/dev/dri").glob("renderD*"))
    except OSError:
        return []


def _probe_render_node(device: str) -> bool:
    """Encode a couple of frames on the iGPU to prove VAAPI really works here."""
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-vaapi_device", device,
        "-f", "lavfi", "-i", f"testsrc=size={PROBE_WIDTH}x{PROBE_HEIGHT}:rate={PROBE_FPS}",
        "-frames:v", "2", "-vf", "format=nv12,hwupload",
        "-c:v", "h264_vaapi", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _usable_render_node() -> str | None:
    """Render node where VAAPI really works, or None. Probed once per process."""
    global _HARDWARE_DEVICE
    with _HARDWARE_LOCK:
        if _HARDWARE_DEVICE is not None:
            return _HARDWARE_DEVICE if isinstance(_HARDWARE_DEVICE, str) else None
        for device in _candidate_render_nodes():
            if os.access(device, os.R_OK | os.W_OK) and _probe_render_node(device):
                LOG.info("Video will use %s (VAAPI)", device)
                _HARDWARE_DEVICE = device
                return device
        LOG.info("Video stays in software (no usable VAAPI render node)")
        _HARDWARE_DEVICE = False
        return None


def hardware_encode_device() -> str | None:
    """Render node usable for H.264 encoding, or None to stay on libx264.

    Encoding three lenses in software costs about two cores permanently, and a
    detection excerpt export spikes to four, which is what pushes this laptop
    into thermal throttling.
    """
    if _ENCODE_DISABLED:
        return None
    return _usable_render_node()


def hardware_decode_device() -> str | None:
    """Render node usable for decoding and VAAPI video filters, or None.

    Same probe and same cache as the encoder: one render node either serves this
    machine's video or it does not. An encoder that failed to stay up does not
    disable decoding, so this ignores disable_hardware_encode().
    """
    return _usable_render_node()


def disable_hardware_encode() -> None:
    """Stop encoding on the iGPU after hardware encoding failed to stay up."""
    global _ENCODE_DISABLED
    with _HARDWARE_LOCK:
        _ENCODE_DISABLED = True
