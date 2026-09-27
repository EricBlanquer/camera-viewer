# Camera Viewer

Camera Viewer is a native Linux desktop viewer for security cameras.
O-KAM Pro account cameras and local RTSP cameras are supported.
It uses Qt for the interface, mpv for H.264 and H.265 playback, and the MIT-licensed [O-KAM Native Bridge](https://github.com/oleandor/okam-ha-native) for O-KAM camera discovery, wake-up, and encrypted P2P transport.
It does not require Home Assistant, Wine, Waydroid, or the phone while viewing.

## Install

Requirements: Python 3.11 or newer, PyQt6, python3-xlib, mpv, ffmpeg with ffplay, and Git.
O-KAM accounts also require `secret-tool` with an unlocked desktop keyring and network access to O-KAM's services.
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
On first launch, enter an O-KAM account email and password in the sign-in dialog, or close that dialog and add an RTSP camera from the tray menu.
For O-KAM, the application saves the password in the desktop keyring and remembers the account in Qt settings.
Later launches start the last selected camera or Jardin automatically.
Use **Add camera > O-KAM account...** in the tray menu to add another account.
Use **Add camera > RTSP camera...** to add a camera by name, RTSP URL without embedded credentials, and UDP or TCP transport.
The **Cameras** submenu lists O-KAM cameras by account and local RTSP cameras by name, switches the displayed camera, and can remove the selected RTSP camera.
With **Show all cameras** enabled in the tray menu, the selected camera and the other cameras appear together in a grid; this is on by default.
Use **Camera layout > Side by side** or **Stacked** to place the feeds horizontally or vertically; the layout choice is remembered.
When the window is docked wide or tall, the camera layout adapts to its available shape and returns to the saved choice when undocked.
Drag one camera title onto another to exchange their positions; the order is remembered without restarting either stream.
The camera panes share the available space equally in a side-by-side layout.
Each camera has its own local playback, photo, recording, and zoom controls; O-KAM camera panes also have sound, pan-and-tilt, saved positions, light, and video quality controls when the camera supports them.
Local playback lists that camera's recordings from the last 24 hours with pause, seeking, speeds up to 8x, zoom, photo, and full-screen controls.
For an O-KAM pane, the playback button offers the camera's microSD recordings and its local 24-hour recordings.
Selecting microSD playback makes that camera the selected pane while the other live feed remains visible.
The other camera views reconnect independently.
In multi-camera view, click the selected camera's video to show or hide its control bar.
Turn off **Show all cameras** to return to one video.
Use **Refresh camera list** there after adding a camera to an existing O-KAM account.
Later launches show only the video and its controls.
Run `.venv/bin/python app.py --forget-account` to remove all saved O-KAM accounts.

The TERUHAL QW55 used with iCam365 provides an RTSP stream at `rtsp://<camera-LAN-address>:8001/0`.
Select UDP transport for this model.
Give the camera a stable LAN address so the saved URL remains valid.
For RTSP cameras, live view, photo, local video recording, continuous recording, and playback of local recordings are available; iCam365 microSD playback, sound, detection notifications, and camera movement controls are not available through this RTSP connection.
An RTSP URL is reachable outside the home only when the computer has a secure route to the camera's LAN, such as a VPN; the iCam365 account relay is not integrated.

Each camera keeps its 16:9 ratio in single-camera and multi-camera views, including when the window is resized.
The current state appears in the window title.
Click the video to show or hide the translucent control bar.
Live video starts automatically, and the viewer reconnects by itself after a network or transport interruption.
Use **Full screen** to expand the viewer.
**Photo** saves a picture and **Record** saves the live video as a Matroska file in `Pictures/O-KAM Linux`.
The existing media and settings paths retain their O-KAM Linux names so earlier recordings and account settings remain available.
A counter at the top of the video shows the recording duration.
For O-KAM cameras, use **Sound** to listen to or silence the camera microphone, and the bulb button to switch the camera's white light.
The magnifier buttons zoom the picture locally.
For O-KAM cameras, the arrow buttons send short pan and tilt pulses to the camera.
For O-KAM cameras, drag the video horizontally or vertically with the left mouse button to move it by up to four short pulses.
For O-KAM cameras, **Preset 1** through **Preset 5** recall its saved positions.

When the desktop provides a notification area, Camera Viewer shows an icon there instead of a taskbar entry.
Click the icon to show or hide the window, or open its menu to use the same controls or quit.
Opening Camera Viewer again from the application menu shows the running instance instead of starting a second one.
Closing the window hides it; live video keeps running in the background so continuous recording and detection checks continue.

For O-KAM cameras, the playback button switches the window to the recordings stored on the camera's microSD card, and **LIVE** returns to live video.
In playback, the control bar shows a timeline with continuous recording in blue and detections in red; drag it or click a time to play from there, and use the wheel or the magnifier buttons to show a shorter or longer period.
Playback starts while the recording is still loading, with sound, pause, and speeds up to 8x.
The picture button saves the current image, and the download button saves the loaded recording as a Matroska file.
Clicking a detection notification opens the playback at that detection.

Every minute, Camera Viewer checks the selected O-KAM camera's microSD card for new detection recordings and shows a desktop notification with the time of the latest one.
The check uses the live connection.

With **Show all cameras** enabled, Camera Viewer records every displayed live video continuously on this computer, in ten-minute Matroska files in the desktop's `Videos/O-KAM Linux/Continuous` folder, and deletes files older than 24 hours.
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
