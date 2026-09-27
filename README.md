# O-KAM Linux

O-KAM Linux is a native Linux desktop viewer for O-KAM Pro cameras.
It uses Qt for the interface, mpv for H.264 playback, and the MIT-licensed [O-KAM Native Bridge](https://github.com/oleandor/okam-ha-native) for camera discovery, wake-up, and encrypted P2P transport.
It does not require Home Assistant, Wine, Waydroid, or the phone while viewing.

## Install

Requirements: Python 3.11 or newer, PyQt6, python3-xlib, mpv, ffmpeg with ffplay, Git, `secret-tool` with an unlocked desktop keyring, and network access to O-KAM's services.
On this computer these programs are already installed.

```sh
git clone --recurse-submodules <repository-url> okam-linux
cd okam-linux
./install.sh
```

The installer creates a virtual environment, installs the pinned transport package, downloads and verifies the official wake configuration, and adds an **O-KAM Linux** application launcher.
It keeps the vendor wake configuration in `~/.local/share/okam-linux/vendor` with owner-only permissions.

## Use

Open **O-KAM Linux** from the application menu or run `.venv/bin/python app.py` from this directory.
On first launch, enter the O-KAM account email and password in the sign-in dialog.
The application saves the password in the desktop keyring, remembers the email in Qt settings, finds the Jardin camera, and starts live video automatically.
Later launches show only the video and its controls.
Run `.venv/bin/python app.py --forget-account` to remove the saved account and show the sign-in dialog on the next launch.

The window keeps the camera's 16:9 ratio while it is resized, and the video fills it without black bars.
The current state appears in the window title.
Click the video to show or hide the translucent control bar.
Use **Watch live**, **Stop**, and **Reconnect** to manage the video connection, or **Full screen** to expand the viewer.
The viewer reconnects automatically after a transport interruption; **Stop** cancels this retry.
**Photo** saves a picture and **Record** saves the live video as a Matroska file in `Pictures/O-KAM Linux`.
A counter at the top of the video shows the recording duration.
Use **Sound** to listen to or silence the camera microphone, and the bulb button to switch the camera's white light.
The magnifier buttons zoom the picture locally.
The arrow buttons send short pan and tilt pulses to the camera.
Drag the video horizontally or vertically with the left mouse button to move it by up to four short pulses.
**Preset 1** through **Preset 5** recall its saved positions.

When the desktop provides a notification area, O-KAM Linux shows an icon there instead of a taskbar entry.
Click the icon to show or hide the window, or open its menu to use the same controls or quit.
Closing the window hides it and stops the video; showing it again restarts live video.

The video quality selector appears only when the camera can switch quality and return to its full resolution without restarting.
If the O-KAM account service omits the camera credential, the application reads a camera-specific secret from the desktop keyring under the attributes `application=okam-linux`, `camera=<camera UID>`.
The account password and camera credential are never embedded in the source code.

Battery cameras may need several seconds to wake before video appears.
Two-way audio is not implemented because the official SDK does not fully document the talk stream.
The application never sends siren or alarm commands.

## Source and limits

The transport submodule is pinned to commit `eeb3e67d11a83a02ba1f408fde8df1ca73218037` of [O-KAM Native Bridge](https://github.com/oleandor/okam-ha-native), whose code is licensed under MIT.
The official O-KAM account and wake services remain necessary for camera discovery and wake-up.
The Jardin camera's live H.264 stream, short directional movement, mouse dragging, camera sound, saved position recall, recording, and white light were verified on this computer.
