# Camera Viewer

Camera Viewer is a native Linux desktop viewer for security cameras.
O-KAM Pro and Imou Life account cameras, local RTSP cameras, Imou cameras with local RTSP access, and configured iCam365 native connections are supported.
It uses Qt for the interface, mpv for H.264 and H.265 playback, and the MIT-licensed [O-KAM Native Bridge](https://github.com/oleandor/okam-ha-native) for O-KAM camera discovery, wake-up, and encrypted P2P transport.
It does not require Home Assistant, Wine, Waydroid, or the phone while viewing.

## Install

Requirements: Python 3.11 or newer, PyQt6, python3-xlib, mpv, ffmpeg with ffplay, Git, OpenCV, and NumPy.
O-KAM and Imou accounts also require `secret-tool` with an unlocked desktop keyring and network access to their account services.
On this computer these programs are already installed.

```sh
git clone --recurse-submodules https://github.com/EricBlanquer/camera-viewer.git
cd camera-viewer
./install.sh
```

The installer creates a virtual environment, installs the pinned transport package, downloads and verifies the official wake configuration and [OpenCV Zoo YOLOX-s model](https://huggingface.co/opencv/opencv_zoo/tree/main/models/object_detection_yolox), and adds a **Camera Viewer** application launcher.
It also starts Camera Viewer in the notification area when the desktop session opens, without waking the camera until the window is shown.
It keeps the vendor wake configuration in `~/.local/share/okam-linux/vendor` with owner-only permissions.

## Use

Open **Camera Viewer** from the application menu or run `.venv/bin/python app.py` from this directory.
On first launch, the window opens automatically and offers a choice of O-KAM account, Imou Life account, RTSP camera, or local Imou camera.
The O-KAM sign-in form stays open while the account is checked and displays connection errors beside the fields so the password can be corrected immediately.
If a saved O-KAM login is rejected while the viewer is hidden, the window opens automatically and displays the error.
Camera setup and sign-in dialogs open on the video window's screen and stay within its usable area.
For O-KAM, the application saves the password in the desktop keyring and remembers the account in Qt settings.
Later launches start the last selected camera or Jardin automatically.
Use **Add camera > O-KAM account...** in the tray menu to add another account.
Use **Add camera > RTSP camera...** to add a camera by name, RTSP URL without embedded credentials, and UDP or TCP transport.
Use **Add camera > Imou Life camera (local)...** to add an Imou camera by local IP address, RTSP port, channel, camera username, and device password or safety code.
The device password is kept in the desktop keyring; saved camera settings and media-player command lines omit it.
Use **Add camera > Imou Life account...** to discover the cameras owned by or shared with your Imou Life account.
Sign into [Imou Open Platform](https://open.imoulife.com/consoleNew) with the same Imou Life account and complete its developer profile to obtain your own AppId and AppSecret.
Enter these application credentials, your account email, and the server region in Camera Viewer.
The AppSecret stays in the desktop keyring; account settings contain camera identities and names, and omit access tokens and temporary stream URLs.
Account discovery runs in the background and displays authentication errors in the open setup form.
The viewer authenticates with Imou's [HMAC-SHA256 request signature](https://open.imoulife.com/book/http/develop.html) and retrieves short-lived live and camera playback streams with separate camera-scoped tokens from the [official Imou player service](https://open.imoulife.com/book/js/sdk.html).
This connection uses the account credentials and does not require the camera's device password or the phone.
Live view, sound, photos, manual video and audio recording, zoom, and playback use the existing camera controls.
Supported pan, tilt, and saved positions use the shared movement panel.
The playback menu offers camera microSD recordings and local 24-hour recordings, with the same timeline, pause, seeking, speeds up to 8x, sound, photos, and clip saving.
Imou recording times retain the camera's local clock, with motion recordings shown in red and continuous recordings in blue.
Zero-duration camera files are omitted from the recording catalog while later pages remain accessible.
Cloud live and camera playback share a certificate-verified secure WebSocket transport exposed through loopback-only listeners.
Cloud live video uses RTSP, while cloud camera playback uses Imou's native recording transport and FFmpeg's DHAV demuxer for video, audio, pause, and supported camera playback speeds.
Accelerated camera playback is silent; normal speed restores sound.
Private upstream URLs and tokens remain in memory; player and recorder command lines contain only the loopback URL.
Camera clip saving runs in the background, publishes the complete file atomically, and removes temporary downloads on cancellation.
Cloud video consumes the account's Imou traffic allowance; continuous cloud recording requires an explicit opt-in in that account's form and the global continuous recording setting.
When the Imou traffic allowance is exhausted, the camera displays an explanation and pauses automatic reconnection; after adding traffic in Imou Cloud, use **Refresh camera list** to reconnect.
Use **Cameras > Imou Life account > Local video access > camera** to route live video, photos, local recording, detection, and microSD video playback and saving directly to that camera's RTSP service.
Enter its LAN address, camera username, and device password or the security code shown by Imou Life under the camera's device label.
Outside the home, connect to the home VPN before opening these cameras; a local connection failure never falls back to cloud video.
For cameras with TLS enabled, the viewer pins the camera certificate during configuration and provides the encrypted stream to the existing player through an authenticated loopback connection.
One camera connection supplies live video, local recording, and detection through the same loopback media broadcaster used for native iCam365 video.
Both live transports limit audio/video interleaving to 100 ms so sparse audio packets do not hold back video.
The camera remains in its Imou account with the same identity, visibility, controls, and recording history.
Movement controls and the microSD recording catalog use the account API and require Internet access and API request allowance.
When local video access is configured, microSD playback and clip saving use the local main stream with the existing camera credentials and certificate pin, without requesting a cloud video URL or consuming Imou video traffic.
Local microSD playback uses the shared timeline, seeking, pause, sound, photos, and clip saving controls, with speeds of 1x, 2x, and 4x.
The camera delivers recorded media ahead of playback into a bounded 256 MiB player cache; available network throughput still limits accelerated playback.
Camera playback without a configured local connection continues to use the cloud video allowance.
Local continuous recording uses the global recording setting and does not consume Imou video traffic.
Use the account's submenu under **Cameras** to edit its region or recording choice, or remove the account and its keyring secret.
Privacy mode is preserved: a masked camera displays its state and does not open a video connection.
Use **Refresh camera list** after changing privacy mode in Imou Life or adding a camera.
The **Cameras** submenu lists O-KAM cameras by account and RTSP, Imou, or configured native iCam365 cameras by name and source.
Each checkbox independently shows or hides its camera, and the choice is remembered across application restarts.
Checked cameras appear together in a grid; all cameras are checked by default.
Unchecking a camera stops its live connection, local recording, and local detection, while the other checked feeds continue.
All cameras can be unchecked to pause viewing and recording.
The previous single-camera or all-camera choice is preserved when migrating existing settings.
The submenu can also remove the selected local camera.
Use **Camera layout > Side by side** or **Stacked** to place the feeds horizontally or vertically; the layout choice is remembered.
Use **Camera layout > Grid 2 × 2** for two columns and at least two rows, with empty cells transparent to the desktop.
With three cameras, the fourth cell stays empty; additional cameras extend the grid by rows.
Adjacent empty cells form one transparent area without separators.
Full-screen view displays empty cells on a black background; leaving full screen restores their transparency.
When the window is docked wide or tall, the camera layout adapts to its available shape and returns to the saved choice when undocked.
Use **Camera profiles > Save current view...** to name the checked cameras, their order, and the camera layout.
Choosing a saved profile restores that selection, order, and layout; the profile matching the current view is checked.
Saving under an existing name, ignoring case, replaces that profile, and **Remove profile** deletes one.
Cameras removed since a profile was saved are ignored when it is applied.
The grid keeps its saved layout when docked.
Camera videos appear without names, title bars, borders, or gaps between their panes.
Embedded video players synchronize with their pane size after mapping and resizing, while preserving the selected zoom and pan.
With multiple cameras displayed, drag one video onto another with the left mouse button to exchange their positions; the order is remembered without restarting either stream.
The same gesture works during playback.
Use the right mouse button to drag the zoomed image or move a supported camera while multiple cameras are displayed.
The camera panes share the available space equally in a side-by-side layout.
Each camera has its own local playback, photo, recording, and zoom controls.
Double-click a live or playback video to enter or leave full-screen view.
Control overlays use the same width and height for equal-sized live videos, with spacing and button widths adapting to fit inside each video.
All panes use the same inline movement panel and translucent controls, with directions and saved positions shown according to the connected camera's capabilities.
Compatible RTSP panes also have sound and vertical tilt, native iCam365 panes have pan-and-tilt and their camera's saved positions, and O-KAM panes have pan-and-tilt, saved positions, and light when supported.
Compatible iCam365 camera panes have a bulb button that switches white light between On and Automatic.
Local playback stays in that camera's video pane and uses the same controls and timeline as camera microSD playback, with recordings from the last 24 hours, pause, seeking, speeds up to 8x, sound, photo, saved clips, and full-screen controls; **LIVE** returns to the camera stream.
For an O-KAM or Imou Life account pane, the playback button offers the camera's microSD recordings and its local 24-hour recordings.
Playback and tray menus use light text on dark backgrounds, including selected and disabled items.
Selecting O-KAM microSD playback makes that camera the selected pane while the other live feed remains visible.
Imou microSD playback stays in its camera pane while the other live feeds continue.
The other camera views reconnect independently.
Click either video to show or hide its own control bar; the bars are hidden initially and close after five seconds.
Keep only one camera checked to display one video.
Use **Refresh camera list** there after adding a camera to an existing account.
Later launches show the video with controls available on click.
Run `.venv/bin/python app.py --forget-account` to remove all saved O-KAM accounts.

The tested TERUHAL QW55 can provide video and sound at `rtsp://<camera-LAN-address>:8001/0/av0`.
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
iCam365 microSD playback and talkback microphone are unavailable through this RTSP connection; local people and animal detection uses the continuous recording.
An RTSP URL is reachable outside the home only when the computer has a secure route to the camera's LAN, such as a VPN; the iCam365 account relay is not integrated.

For a TERUHAL QW55 whose RTSP service is unavailable, Camera Viewer can use its authenticated iCam365 CS2/PPCS connection.
The native connection was verified with HEVC video at 2304 × 1296 and 8 frames per second, plus G.711 A-law audio at 8 kHz.
Add the camera as an RTSP camera, then import a device-list detail response obtained from your authenticated iCam365 account:

```sh
.venv/bin/python icam365.py --device-response /path/to/device-response.json --camera-name Entrance
```

The response must contain exactly one device with `p2p_id`, `p2p_platform`, and `password` fields.
Connection parameters are stored in `~/.config/camera-viewer/icam365.json` with owner-only permissions and associated with the saved camera ID.
The camera credential can change: importing only a device response retains a static credential.
To fetch its current value before each native connection, also pass `--cloud-session /path/to/session.json` when importing.
The private session JSON contains `origin` (`https://api-we01.tange365.com`), `token`, `appid`, `uuid`, and the Android request metadata in `query`.
The session must identify the imported device and is saved in the same owner-only configuration file.
If the account service is unavailable, the viewer tries the saved camera credential; a rejected credential then displays the account-session error.
When the account token expires, import a renewed authenticated session; automatic iCam365 account sign-in is not implemented.
After restarting the viewer, that camera uses its native transport for live view and local recordings; the phone is not needed.
One native session supplies all local consumers, with ordered packets, acknowledgements, bounded retries, and explicit video/audio stop commands before disconnecting.
Each missing packet has an eight-second recovery window; recovering a gap starts a fresh window for the next missing packet while duplicate packets keep the current deadline.
Incoming datagrams are acknowledged immediately.
Camera readiness is acknowledged during connection setup and repeated readiness or punch messages are answered during streaming.
Closing a native session waits up to five seconds for video/audio stop acknowledgements despite media gaps, and closes all punched endpoints even after an unsuccessful connection attempt.
A loopback-only HTTP stream feeds the existing player and recording UI, preserving the original HEVC video while converting A-law audio to AAC for the local stream.
Audio is resampled against the live timestamps so differences in the camera's audio clock do not accumulate playback delay.
The native muxer keeps AAC priming timestamps within the current stream timeline.
The native connection supports the On/Automatic light control, short directional motor pulses, and horizontal or vertical mouse dragging.
The camera's saved positions are retrieved over its native connection, displayed with their saved names in tooltips, and recalled with their original position coordinates and identifiers.
Local people and animal detection is available with the native iCam365 connection and continuous recording.
Directory lookup and a direct native connection were verified locally; the relay path outside the home remains unverified.
This connection does not provide camera microSD playback or talkback.

The Imou form uses the [Dahua main-stream RTSP path](https://www.dahuasecurity.com/asset/upload/download/DS-PSD8802-A180_Operation_Manual__201709251.pdf) `/cam/realmonitor?channel=<channel>&subtype=0` and TCP port 554 by default.
The camera must expose that RTSP stream on the local network; model support depends on the camera and its settings.
Use the camera's device password or safety code, which may differ from the Imou Life account password.
Local Imou cameras have the RTSP live-view and local recording controls described above.
This local connection uses local recording playback; the Imou Life account connection provides camera microSD playback and supported remote movement.
Talkback is unavailable in the viewer.
For access away from the camera's network, the computer needs a route to its local IP address, such as a VPN.

Each camera keeps its 16:9 ratio in single-camera and multi-camera views, including when the window is resized.
Connection state, playback loading, and errors appear over the affected video.
Click the video to show or hide the translucent control bar.
Live video starts automatically, and the viewer reconnects by itself after a network or transport interruption.
RTSP and native iCam365 live views also reconnect when playback accumulates at least six seconds of additional delay for three seconds.
The delay is measured against monotonic elapsed time and the fastest observed playback position, independently of the camera's displayed clock and initial connection delay.
Reconnecting closes the current recording segment and starts a fresh transport, player, and continuous recording segment.
Each video keeps its last decoded camera image during reconnection and replaces it when the new player has a frame.
The connection message identifies this paused image; changing or stopping a camera clears it.
Use **Full screen** to expand the viewer.
**Photo** saves a picture and **Record** saves the live video as a Matroska file in `Pictures/O-KAM Linux`.
The existing media and settings paths retain their O-KAM Linux names so earlier recordings and account settings remain available.
A counter at the top of the video shows the recording duration.
For O-KAM cameras, use **Sound** to listen to or silence the camera microphone, and the bulb button to switch the camera's white light.
The magnifier buttons zoom the picture locally.
For O-KAM cameras, the arrow buttons send short pan and tilt pulses to the camera.
For O-KAM cameras, drag the video horizontally or vertically with the right mouse button to move it by up to four short pulses; the left button also moves the camera when only one camera is displayed.
For O-KAM cameras, **Preset 1** through **Preset 5** recall its saved positions.
For compatible iCam365 cameras, **PTZ** offers the movements available through the connected transport: Up and Down over the local HTTP endpoint, or pan-and-tilt and saved positions over the native connection.

The title bar provides the desktop's minimize, maximize, and close buttons, and the window remains available from the taskbar.
The application and desktop launcher share the `camera-viewer` identity so the taskbar uses the Camera Viewer icon.
When the desktop provides a notification area, Camera Viewer also shows an icon there.
Click the icon to show or hide the window, or open its menu for camera selection, layout, camera setup, recording, and local detection settings.
Playback and camera controls are available in each camera's video pane.
Opening Camera Viewer again from the application menu shows the running instance instead of starting a second one.
Closing the window hides it; live video keeps running in the background so continuous recording and detection checks continue.
Clicking the tray icon brings a window behind other applications to the front; clicking it while Camera Viewer is active hides it.
Camera selection, connection retries, and account errors preserve the window's visibility, minimization, and focus.
Video controls and status messages remain hidden while Camera Viewer is behind another application, minimized, or hidden; current messages reappear when its window becomes active.
The tray menu enables local detection of people and animals while continuous recording is on.
Use **Local detection cameras** to enable or disable analysis and notifications independently for each camera.
The global detection switch preserves these camera choices; disabling a camera's detection keeps its video and continuous recording running.
It analyzes one 640 × 360 frame per second with the SHA-256-verified OpenCV Zoo YOLOX-s model and confirms an event when the same object appears twice within two seconds.
A person shape confirms an event only after its position or size changes, so motionless scenery that resembles a person is ignored.
Cats, dogs, and birds recognized by the model are reported as animals without naming the species, because dark animals in infrared images are often confused.
Event excerpts include up to five seconds before and after the detected passage and are stored without audio in `Videos/O-KAM Linux/Detections`, using the system's configured Videos folder.
Each excerpt embeds, as its `cover_land.jpg` Matroska cover, the analyzed frame with the highest detection score, with the detected people or animals outlined.
File managers that use embedded covers, such as Dolphin, show this image as the excerpt's thumbnail instead of the first frame.
When the analyzed frame is unavailable, such as for a detection recovered after an interruption, the cover comes from the excerpt at the middle of the detected passage.
At startup, excerpts listed in the day folders' metadata without a cover receive one in the background; other folders are left unchanged.
The `Detections` folder groups excerpts by day in `YYYY-MM-DD` folders, then in `animal` and `person` folders; clips with both types use the `animal_person` folder, and each clip name starts with its type and camera name.
Each day folder also contains a hidden `.metadata` folder describing its detections for the playback timeline.
Local detections appear in red on the local playback timeline, and its Previous and Next buttons jump between detected passages.
Clicking a local detection notification plays that notification's excerpt in its camera pane, including when the viewer was hidden in the notification area.
Detection notifications are sent to the desktop notification service with a click action, and fall back to the notification area message when that service is unavailable.
Local detection clips and metadata are kept until deleted manually; the playback timeline shows detections from the last 24 hours, and turning off continuous recording pauses local analysis.

For O-KAM cameras, the playback button switches the window to the recordings stored on the camera's microSD card, and **LIVE** returns to live video.
In playback, the control bar shows a timeline with continuous recording in blue and detections in red; drag it or click a time to play from there, and use the wheel or the magnifier buttons to show a shorter or longer period.
Playback starts while the recording is still loading, with sound, pause, and speeds up to 8x.
The picture button saves the current image, and the download button saves the loaded recording as a Matroska file.
Clicking a microSD detection notification opens that camera's playback at the detection, selecting the camera first when another one is selected.

Every minute, Camera Viewer checks the selected O-KAM camera's microSD card for new detection recordings and shows a desktop notification with the time of the latest one.
The check uses the live connection.

Camera Viewer records every checked live camera continuously on this computer, in ten-minute Matroska files in the desktop's `Videos/O-KAM Linux/Continuous` folder, and deletes files older than 24 hours.
This keeps a copy of the last day even if the camera and its microSD card are taken, as long as the computer and the application are running.
The tray menu switches continuous recording on or off and opens the recordings folder.
A segment interrupted by a crash or power loss is recovered at the next start.
The first check only records the latest existing detection, so older events are not announced.

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
