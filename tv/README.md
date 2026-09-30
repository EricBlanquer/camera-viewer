# Camera Viewer for Samsung TV

The TV establishes its own authenticated UDP sessions with iCam365 and O-KAM cameras.
It uses the camera vendor's directory service for discovery and can retry an O-KAM connection through that vendor's P2P service after a direct connection failure.
Imou Life cameras use the account's Imou Open Platform AppId and AppSecret to obtain private RTSP video addresses over HTTPS.
The TV renews its account token automatically, obtains a fresh stream address on reconnection, and receives video over its own TCP connection.
Camera video never passes through a PC.

Four equal cells remain visible in a 2 × 2 grid, including when only one or two cameras are configured.
Each cell has its own session, H.264 or HEVC software decoder, status, and reconnection timer.
Camera titles and labels in unused cells are hidden.
The full-screen grid has an opaque black background, including unused cells.
During reconnection, the last decoded image remains visible with a small connection-status caption until the next image arrives.
Changing or removing a camera clears its previous image.
The diagnostic panel hides as soon as a decoded image appears.
No rendered image for twelve seconds triggers a new connection; compressed and decoded buffers are bounded to limit accumulated delay.
Decoders wait for an image keyframe at startup and after dropping compressed packets.
The Imou decoder can queue up to 32 frames for TCP bursts, with a 16 MiB compressed-data limit; UDP decoders retain their four-frame limit.

## Compatibility and verified behavior

Entrée (iCam365, HEVC), Jardin (O-KAM, H.264), and Cuisine (Imou Life, H.264 secondary stream) were connected simultaneously from a Samsung QE55Q85RATXXC with Tizen 5.0.
Simultaneous playback through AVPlay on this TV stopped one feed while packets continued arriving.
The application therefore decodes the feeds on the TV using FFmpeg workers and renders them to independent canvases, at up to 960 × 540 pixels per cell.
Three distinct camera connections and advancing decoded images were verified; four distinct cameras and remote networks remain unverified.
Audio, PTZ, playback, recording, detection, and an on-TV account setup interface are outside this application's current scope.

This build targets ARMv7 NaCl-enabled Samsung TVs.
Samsung documents [NaCl support through 2021 product models](https://developer.samsung.com/smarttv/develop/extension-libraries/nacl/download.html); newer TVs need a separate WebAssembly port.
The widget requests Internet, network information, and remote-control input privileges.

## Build

Install Docker, Tizen Studio with the Samsung TV extension, and a Samsung signing profile authorized for the TV's DUID.
Download [Samsung Pepper 63](https://developer.samsung.com/smarttv/develop/extension-libraries/nacl/download.html) and extract it.
The downloaded SDK archive used for validation has SHA-256 `e9ac8535e04d133e64790fbaa3a0be81902dd1e61bfcacff5afff642a0c90e69`.

```sh
NACL_SDK_ROOT=/path/to/pepper_63 TIZEN_PROFILE=YourProfile tv/build.sh
```

The build uses a Python 2.7 Docker image for Samsung's legacy tools, fetches FFmpeg revision `241fe649406143735cb559b53b48044c933801d5`, and enables only H.264/HEVC decoding and pixel conversion.
Default dependency paths are under `~/.cache/camera-viewer-tv`; `CAMERA_TV_CACHE`, `FFMPEG_SRC`, and `TIZEN_CLI` can override them.
Using `FFMPEG_SRC` also permits rebuilding and relinking with a modified FFmpeg source tree; remove its static libraries to rebuild them after changes.
The signed package is `tv/dist/camera-viewer.wgt`, and the relinkable application object is `tv/dist/transport_armv7.o`.
Dependencies, native binaries, packages, and credentials are excluded from Git.
The build does not launch or modify the Linux viewer.

```sh
node --test tv/tests/*.test.js
```

## Install and configure

Enable the TV's developer mode and authorize this computer through Tizen Studio.
Use the TV's actual LAN address in the following commands.

```sh
~/tizen-studio/tools/sdb connect TV_ADDRESS
~/tizen-studio/tools/ide/bin/tizen install -n tv/dist/camera-viewer.wgt -s TV_ADDRESS:26101
~/tizen-studio/tools/sdb -s TV_ADDRESS:26101 shell 0 debug CamViewTV1.CameraViewer
```

The last command prints a debug port.
Install Python's `websocket-client` package for the provisioning helper.
Store an array of one to four camera configurations in a private JSON file outside the checkout and set its permissions to `600`.
An iCam365 entry contains `type: "icam365"`, `name`, `p2p_id`, `p2p_platform`, and `password` from an authenticated device detail response.
Its optional `cloud_session` uses the same private metadata described in the root README; the TV refreshes the device credential over HTTPS before connecting.
An O-KAM entry contains `type: "okam"`, `name`, the resolved fifteen-character `uid`, `service_parameter`, and its device `password`.
An Imou Life entry contains `type: "imou"`, `name`, `device_id`, `channel_id` (a string, usually `"0"`), and `account` with `app_id`, `app_secret`, and `region` (`"Europe"`, `"Singapore"`, or `"North America"`).
The default `stream_id: 1` selects the secondary stream to reduce decoding load and cloud traffic in the grid; `stream_id: 0` selects the HD main stream and can reduce frame rate on older TVs.
IoT devices can additionally specify `product_id`.
Use keys registered for your account in the [Imou Open Platform](https://open.imoulife.com/book/http/develop.html) and the device identity returned by authenticated discovery.
Imou video uses the TV's Web Crypto API for HMAC-SHA256 signing and the native transport for RTSP interleaved H.264/HEVC video; audio tracks are ignored.
Viewing consumes the Imou account's cloud traffic allowance.
Account tokens and expiring stream URLs remain in memory and are excluded from saved configuration.
The Linux viewer's account discovery and desktop keyring can supply these values during setup.
Passwords, application secrets, and provisioned iCam365 account tokens belong only in that private file, never in the widget or repository.

```sh
chmod 600 /private/path/cameras.json
python3 tv/device.py http://TV_ADDRESS:DEBUG_PORT --configure /private/path/cameras.json
python3 tv/device.py http://TV_ADDRESS:DEBUG_PORT
```

Provisioning writes the camera settings to the TV application's local storage.
Subsequent launches connect autonomously; a PC is needed only for building, installation, and setup.
The status helper reports connection and frame counters without credentials.
Renew the provisioned iCam365 account session when its account token expires.

## Remote control

The arrow keys select a cell, OK reconnects the selected camera, and Back exits the application.
Hiding the application stops its sessions; returning to it connects again.
Camera stop commands are acknowledged for up to five seconds before closing the native socket.

## Third-party notices

The widget includes FFmpeg's LGPL 2.1 license, the O-KAM bridge's MIT notice for the adapted protocol, and Samsung NaCl/Chromium notices in `web/licenses`.
FFmpeg source, configuration flags, and the application object needed for relinking are available through this build workflow.
Redistributions of the binary must include the corresponding source and relinking materials required by those licenses.
