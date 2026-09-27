#!/bin/sh
set -eu

app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
data_dir="$HOME/.local/share/okam-linux/vendor"
desktop_dir="$HOME/.local/share/applications"
autostart_dir="$HOME/.config/autostart"

git -C "$app_dir" submodule update --init
python3 -m venv --system-site-packages "$app_dir/.venv"
"$app_dir/.venv/bin/python" -m pip install --no-deps -e "$app_dir/vendor/okam-ha-native"
mkdir -p "$data_dir" "$desktop_dir"
if [ ! -f "$data_dir/device_wakeup_server.dart" ]; then
    "$app_dir/.venv/bin/python" "$app_dir/vendor/okam-ha-native/tools/fetch_official_sdk.py" --wake-only --destination "$data_dir"
fi
chmod 600 "$data_dir/device_wakeup_server.dart"
cat > "$desktop_dir/okam-linux.desktop" <<EOF
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
chmod 644 "$desktop_dir/okam-linux.desktop"
mkdir -p "$autostart_dir"
sed -e "s|^Exec=.*|Exec=$app_dir/.venv/bin/python $app_dir/app.py --tray|" \
    -e '$a X-GNOME-Autostart-enabled=true' \
    -e '$a X-GNOME-Autostart-Delay=10' \
    "$desktop_dir/okam-linux.desktop" > "$autostart_dir/okam-linux.desktop"
chmod 644 "$autostart_dir/okam-linux.desktop"
