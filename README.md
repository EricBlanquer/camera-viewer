# Camera Viewer

Camera Viewer is a native Linux desktop viewer for security cameras.
O-KAM Pro cameras are currently supported.
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

The installer creates a virtual environment, installs the pinned transport package, downloads and verifies the official wake configuration, and adds a **Camera Viewer** application launcher.
It also starts Camera Viewer in the notification area when the desktop session opens, without waking the camera until the window is shown.
It keeps the vendor wake configuration in `~/.local/share/okam-linux/vendor` with owner-only permissions.

## Use

Open **Camera Viewer** from the application menu or run `.venv/bin/python app.py` from this directory.
On first launch, enter an O-KAM account email and password in the sign-in dialog.
The application saves the password in the desktop keyring, remembers the account in Qt settings, finds its cameras, and starts the last selected camera or Jardin automatically.
Use **Add camera > O-KAM account...** in the tray menu to add another account.
The **Cameras** submenu lists the discovered cameras by name and account and switches the displayed camera.
Use **Refresh camera list** there after adding a camera to an existing O-KAM account.
Later launches show only the video and its controls.
Run `.venv/bin/python app.py --forget-account` to remove all saved O-KAM accounts and show the sign-in dialog on the next launch.

The window keeps the camera's 16:9 ratio while it is resized, and the video fills it without black bars.
The current state appears in the window title.
Click the video to show or hide the translucent control bar.
Live video starts automatically, and the viewer reconnects by itself after a network or transport interruption.
Use **Full screen** to expand the viewer.
**Photo** saves a picture and **Record** saves the live video as a Matroska file in `Pictures/O-KAM Linux`.
The existing media and settings paths retain their O-KAM Linux names so earlier recordings and account settings remain available.
A counter at the top of the video shows the recording duration.
Use **Sound** to listen to or silence the camera microphone, and the bulb button to switch the camera's white light.
The magnifier buttons zoom the picture locally.
The arrow buttons send short pan and tilt pulses to the camera.
Drag the video horizontally or vertically with the left mouse button to move it by up to four short pulses.
**Preset 1** through **Preset 5** recall its saved positions.

When the desktop provides a notification area, Camera Viewer shows an icon there instead of a taskbar entry.
Click the icon to show or hide the window, or open its menu to use the same controls or quit.
Opening Camera Viewer again from the application menu shows the running instance instead of starting a second one.
Closing the window hides it; live video keeps running in the background so continuous recording and detection checks continue.

The playback button switches the window to the recordings stored on the camera's microSD card, and **LIVE** returns to live video.
In playback, the control bar shows a timeline with continuous recording in blue and detections in red; drag it or click a time to play from there, and use the wheel or the magnifier buttons to show a shorter or longer period.
Playback starts while the recording is still loading, with sound, pause, and speeds up to 8x.
The picture button saves the current image, and the download button saves the loaded recording as a Matroska file.
Clicking a detection notification opens the playback at that detection.

Every minute, Camera Viewer checks the selected camera's microSD card for new detection recordings and shows a desktop notification with the time of the latest one.
The check uses the live connection.

Camera Viewer also records the selected live video continuously on this computer, in ten-minute Matroska files in `Videos/O-KAM Linux/Continuous`, and deletes files older than 24 hours.
This keeps a copy of the last day even if the camera and its microSD card are taken, as long as the computer and the application are running.
The tray menu switches continuous recording on or off and opens the recordings folder.
A segment interrupted by a crash or power loss is recovered at the next start.
The first check only records the latest existing detection, so older events are not announced.

The video quality selector appears only when the camera can switch quality and return to its full resolution without restarting.
If the O-KAM account service omits the camera credential, the application reads a camera-specific secret from the desktop keyring under the attributes `application=okam-linux`, `camera=<camera UID>`.
The account password and camera credential are never embedded in the source code.

Battery cameras may need several seconds to wake before video appears.
Two-way audio is not implemented because the official SDK does not fully document the talk stream.
The application never sends siren or alarm commands.

The application writes a log to `~/.cache/okam-linux/okam-linux.log`.

## Camera password

The application fetches the camera's local password from the O-KAM account automatically.
If the camera rejects the connection, `.venv/bin/python app.py --camera-password` prints the password the account returns for each camera.

## Source and limits

The transport submodule is pinned to commit `eeb3e67d11a83a02ba1f408fde8df1ca73218037` of [O-KAM Native Bridge](https://github.com/oleandor/okam-ha-native), whose code is licensed under MIT.
The official O-KAM account and wake services remain necessary for camera discovery and wake-up.
The Jardin camera's live H.264 stream, short directional movement, mouse dragging, camera sound, saved position recall, recording, and white light were verified on this computer.
