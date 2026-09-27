# Camera Viewer

Camera Viewer is a native Linux desktop viewer for security cameras.
O-KAM Pro account cameras, local RTSP cameras, and Imou Life cameras with local RTSP access are supported.
It uses Qt for the interface, mpv for H.264 and H.265 playback, and the MIT-licensed [O-KAM Native Bridge](https://github.com/oleandor/okam-ha-native) for O-KAM camera discovery, wake-up, and encrypted P2P transport.
It does not require Home Assistant, Wine, Waydroid, or the phone while viewing.

## Install

Requirements: Python 3.11 or newer, PyQt6, python3-xlib, mpv, ffmpeg with ffplay, and Git.
O-KAM accounts also require `secret-tool` with an unlocked desktop keyring and network access to O-KAM's services.
On this computer these programs are already installed.

```sh
git clone --recurse-submodules https://github.com/EricBlanquer/camera-viewer.git
cd camera-viewer
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
Use **Add camera > Imou Life camera (local)...** to add an Imou camera by local IP address, RTSP port, channel, camera username, and device password or safety code.
The device password is kept in the desktop keyring; saved camera settings and media-player command lines omit it.
The **Cameras** submenu lists O-KAM cameras by account and local RTSP or Imou cameras by name, switches the displayed camera, and can remove the selected local camera.
With **Show all cameras** enabled in the tray menu, the selected camera and the other cameras appear together in a grid; this is on by default.
Use **Camera layout > Side by side** or **Stacked** to place the feeds horizontally or vertically; the layout choice is remembered.
When the window is docked wide or tall, the camera layout adapts to its available shape and returns to the saved choice when undocked.
Drag one camera title onto another to exchange their positions; the order is remembered without restarting either stream.
The camera panes share the available space equally in a side-by-side layout.
Each camera has its own local playback, photo, recording, zoom, and full-screen controls; compatible RTSP panes also have sound and vertical tilt when the camera supports them, while O-KAM panes also have pan-and-tilt, saved positions, light, and video quality controls when the camera supports them.
Compatible iCam365 camera panes have a bulb button that switches white light between On and Automatic.
Local playback stays in that camera's video pane and uses the same controls and timeline as camera microSD playback, with recordings from the last 24 hours, pause, seeking, speeds up to 8x, sound, photo, saved clips, and full-screen controls; **LIVE** returns to the camera stream.
For an O-KAM pane, the playback button offers the camera's microSD recordings and its local 24-hour recordings.
Selecting microSD playback makes that camera the selected pane while the other live feed remains visible.
The other camera views reconnect independently.
Click either video to show or hide its own control bar; the bars are hidden initially and close after five seconds.
Double-click either video to toggle full screen.
Turn off **Show all cameras** to return to one video.
Use **Refresh camera list** there after adding a camera to an existing O-KAM account.
Later launches show the video with controls available on click.
Run `.venv/bin/python app.py --forget-account` to remove all saved O-KAM accounts.

The TERUHAL QW55 used with iCam365 provides video and sound at `rtsp://<camera-LAN-address>:8001/0/av0`.
Select TCP transport for this model.
Give the camera a stable LAN address so the saved URL remains valid.
For RTSP cameras, live view, photo, local video and audio recording, continuous recording, and playback of local recordings are available when the configured stream carries audio.
The live sound choice is saved per RTSP camera and restored after a stream reconnect or application restart.
RTSP live sound is amplified in the player to make low-level camera microphones audible; the saved recording keeps the original audio.
RTSP live view and local playback apply noise reduction in the player; saved recordings retain the original camera stream.
When the camera exposes the TAS-Tech local white light endpoint, the bulb button switches between On and Automatic.
When the camera exposes the TAS-Tech local tilt endpoint, the PTZ control appears after the live stream starts and offers short Up and Down movements; a vertical mouse drag also moves the camera.
The tested TERUHAL QW55 accepts these movements on its local HTTP service at port 8001.
On the tested camera, the local endpoint acknowledged horizontal commands without moving the image and exposed no verified saved-position command; the iCam365 mobile app uses its proprietary P2P protocol for these controls.
iCam365 microSD playback, talkback microphone, and detection notifications are not available through this RTSP connection.
An RTSP URL is reachable outside the home only when the computer has a secure route to the camera's LAN, such as a VPN; the iCam365 account relay is not integrated.

The Imou form uses the [Dahua main-stream RTSP path](https://www.dahuasecurity.com/asset/upload/download/DS-PSD8802-A180_Operation_Manual__201709251.pdf) `/cam/realmonitor?channel=<channel>&subtype=0` and TCP port 554 by default.
The camera must expose that RTSP stream on the local network; model support depends on the camera and its settings.
Use the camera's device password or safety code, which may differ from the Imou Life account password.
Imou cameras have the RTSP live-view and local recording controls described above; Imou Life account discovery, cloud relay, microSD playback, talkback, and remote movement are not integrated.
For access away from the camera's network, the computer needs a route to its local IP address, such as a VPN.

Each camera keeps its 16:9 ratio in single-camera and multi-camera views, including when the window is resized.
The selected camera's live state appears next to its name; playback loading and errors appear over the affected video.
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
For compatible iCam365 cameras, **PTZ** offers Up and Down after the local control endpoint is detected.

When the desktop provides a notification area, Camera Viewer shows an icon there instead of a taskbar entry.
Click the icon to show or hide the window, or open its menu for camera selection, playback, and the **Camera controls** submenu.
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
