#!/bin/sh
set -eu

app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
data_dir="$HOME/.local/share/okam-linux/vendor"
desktop_dir="$HOME/.local/share/applications"
autostart_dir="$HOME/.config/autostart"
launcher_name="camera-viewer.desktop"
legacy_launcher_name="okam-linux.desktop"

git -C "$app_dir" submodule update --init
python3 -m venv --system-site-packages "$app_dir/.venv"
"$app_dir/.venv/bin/python" -m pip install --no-deps -e "$app_dir/vendor/okam-ha-native"
"$app_dir/.venv/bin/python" -m pip install "cs2pppp==0.2.11"
"$app_dir/.venv/bin/python" -m pip install 'websocket-client>=1.9,<2'
if ! "$app_dir/.venv/bin/python" -c 'import cv2, numpy' >/dev/null 2>&1; then
    "$app_dir/.venv/bin/python" -m pip install 'opencv-python-headless>=4.6,<5' 'numpy>=1.24,<3'
fi
"$app_dir/.venv/bin/python" "$app_dir/local_detection.py" --install-model
mkdir -p "$data_dir" "$desktop_dir"
if [ ! -f "$data_dir/device_wakeup_server.dart" ]; then
    "$app_dir/.venv/bin/python" "$app_dir/vendor/okam-ha-native/tools/fetch_official_sdk.py" --wake-only --destination "$data_dir"
fi
chmod 600 "$data_dir/device_wakeup_server.dart"
cat > "$desktop_dir/$launcher_name" <<EOF
[Desktop Entry]
Type=Application
Name=Camera Viewer
Comment=Watch your security cameras
Exec=$app_dir/.venv/bin/python $app_dir/app.py
TryExec=$app_dir/.venv/bin/python
Icon=$app_dir/assets/icons/app.svg
Terminal=false
Categories=AudioVideo;Video;
EOF
chmod 644 "$desktop_dir/$launcher_name"
mkdir -p "$autostart_dir"
sed -e "s|^Exec=.*|Exec=$app_dir/.venv/bin/python $app_dir/app.py --tray|" \
    -e '$a X-GNOME-Autostart-enabled=true' \
    -e '$a X-GNOME-Autostart-Delay=10' \
    "$desktop_dir/$launcher_name" > "$autostart_dir/$launcher_name"
chmod 644 "$autostart_dir/$launcher_name"
rm -f "$desktop_dir/$legacy_launcher_name" "$autostart_dir/$legacy_launcher_name"
