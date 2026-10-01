from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
subprocess.run([
    "rsvg-convert", "--width", "128", "--height", "128",
    "--output", str(root / "web/icon.png"),
    str(root.parent / "assets/icons/app.svg"),
], check=True)
