from pathlib import Path


ICON_SHAPES = {
    "play": '<path d="M8 5 19 12 8 19Z"/>',
    "pause": '<path d="M9 5v14m6-14v14"/>',
    "replay": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
    "previous_detection": '<path d="M6 5v14"/><path d="M18 5 9 12l9 7Z"/>',
    "next_detection": '<path d="M18 5v14"/><path d="M6 5l9 7-9 7Z"/>',
    "download": '<path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/>',
    "photo": '<path d="M3 7h4l2-2h6l2 2h4v12H3Z"/><circle cx="12" cy="13" r="3.5"/>',
    "record": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4" fill="#f55" stroke="none"/>',
    "recording": '<rect x="5" y="5" width="14" height="14" rx="2" fill="#f55" stroke="none"/>',
    "sound": '<path d="M4 9h4l5-4v14l-5-4H4Z"/><path d="M16 9a5 5 0 0 1 0 6M18 6a9 9 0 0 1 0 12"/>',
    "mute": '<path d="M4 9h4l5-4v14l-5-4H4Z"/><path d="m16 9 5 6m0-6-5 6"/>',
    "zoom_in": '<circle cx="10" cy="10" r="6"/><path d="M10 7v6m-3-3h6m1.5 4.5L21 21"/>',
    "zoom_out": '<circle cx="10" cy="10" r="6"/><path d="M7 10h6m1.5 4.5L21 21"/>',
    "ptz": '<circle cx="12" cy="12" r="2"/><path d="m12 2-3 3h6Zm0 20-3-3h6ZM2 12l3-3v6Zm20 0-3-3v6Z"/>',
    "fullscreen": '<path d="M3 9V3h6m6 0h6v6m0 6v6h-6m-6 0H3v-6"/>',
    "exit_fullscreen": '<path d="M9 3v6H3m12-6v6h6m0 6h-6v6M3 15h6v6"/>',
    "left": '<path d="m14 5-7 7 7 7M7 12h14"/>',
    "right": '<path d="m10 5 7 7-7 7m7-7H3"/>',
    "up": '<path d="m5 14 7-7 7 7M12 7v14"/>',
    "down": '<path d="m5 10 7 7 7-7m-7 7V3"/>',
    "light": '<path d="M9 18h6m-5 3h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3Z"/>',
}


APP_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
    '<defs>'
    '<linearGradient id="shell" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#c9d1d9"/></linearGradient>'
    '<radialGradient id="lens" cx="0.4" cy="0.35" r="0.7">'
    '<stop offset="0" stop-color="#3b82f6"/><stop offset="0.45" stop-color="#1e3a8a"/>'
    '<stop offset="1" stop-color="#0b1020"/></radialGradient>'
    '</defs>'
    '<rect x="14" y="50" width="36" height="10" rx="5" fill="#9aa4ae"/>'
    '<rect x="26" y="44" width="12" height="9" rx="2" fill="#b8c0c8"/>'
    '<circle cx="32" cy="27" r="22" fill="url(#shell)" stroke="#8a949e" stroke-width="1.5"/>'
    '<circle cx="32" cy="27" r="14" fill="#1f2328"/>'
    '<circle cx="32" cy="27" r="10.5" fill="url(#lens)"/>'
    '<circle cx="28" cy="23" r="3" fill="#ffffff" fill-opacity="0.85"/>'
    '<circle cx="32" cy="10" r="1.8" fill="#22c55e"/>'
    '</svg>\n'
)


def write_icon(name: str, shape: str, color: str = "#f5f5f5") -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
        f'<g fill="none" stroke="{color}" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f"{shape}</g></svg>\n"
    )
    (Path(__file__).parent / "icons" / f"{name}.svg").write_text(svg, encoding="utf-8")


def main() -> None:
    (Path(__file__).parent / "icons").mkdir(parents=True, exist_ok=True)
    (Path(__file__).parent / "icons" / "app.svg").write_text(APP_ICON, encoding="utf-8")
    for name, shape in ICON_SHAPES.items():
        write_icon(name, shape)
    write_icon("sound_on", ICON_SHAPES["sound"], "#38c7dc")
    write_icon("light_on", ICON_SHAPES["light"], "#ffd54a")
    for index in range(1, 6):
        shape = (
            '<circle cx="12" cy="12" r="9"/>'
            f'<text x="12" y="17" text-anchor="middle" fill="#f5f5f5" '
            f'stroke="none" font-family="sans-serif" font-size="12">{index}</text>'
        )
        write_icon(f"preset_{index}", shape)


if __name__ == "__main__":
    main()
