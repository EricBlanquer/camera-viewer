# O-KAM Linux

O-KAM Linux is a native Linux desktop viewer for O-KAM Pro cameras.
It uses Qt for the interface, mpv for H.264 playback, and the MIT-licensed [O-KAM Native Bridge](https://github.com/oleandor/okam-ha-native) for camera discovery, wake-up, and encrypted P2P transport.
It does not require Home Assistant, Wine, Waydroid, or the phone while viewing.

## Install

Requirements: Python 3.11 or newer, PyQt6, mpv, Git, `secret-tool` with an unlocked desktop keyring, and network access to O-KAM's services.
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

Use **Watch live**, **Stop**, and **Reconnect** to manage the video connection, or **Full screen** to expand the viewer.
The arrow buttons send one-step pan and tilt commands to the camera, and **Preset 1** through **Preset 5** recall its saved positions.
If the O-KAM account service omits the camera credential, the application reads a camera-specific secret from the desktop keyring under the attributes `application=okam-linux`, `camera=<camera UID>`.
The account password and camera credential are never embedded in the source code.

Battery cameras may need several seconds to wake before video appears.
The current application supports live H.264 video, directional pan and tilt, saved camera positions, and full-screen viewing.
Recordings, sound, and two-way audio are not implemented.

## Source and limits

The transport submodule is pinned to commit `eeb3e67d11a83a02ba1f408fde8df1ca73218037` of [O-KAM Native Bridge](https://github.com/oleandor/okam-ha-native), whose code is licensed under MIT.
The official O-KAM account and wake services remain necessary for camera discovery and wake-up.
The Jardin camera's live H.264 stream and a directional control response were verified on this computer.
