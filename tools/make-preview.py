#!/usr/bin/env python3
"""Draw the marketplace preview image.

This is a *mockup*, drawn from Panel.qml's layout -- not a screenshot. It exists
so the repository is complete without a running Quickshell, and so the preview
can be regenerated when the panel changes. Replace it with a real screenshot of
your own bar when you have one; that is always the better listing image.

    ./tools/make-preview.py            # writes preview.png

Nothing here talks to the mouse.
"""

from __future__ import annotations

import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow is needed to draw the preview: pip install --user pillow")

W, H = 1280, 800
BG = (24, 24, 28)
PANEL = (32, 32, 38)
BAR = (18, 18, 22)
FG = (232, 232, 236)
DIM = (128, 128, 138)
LINE = (52, 52, 60)
ACCENT = (126, 178, 240)
URGENT = (232, 138, 120)

STAGES = [
    (1, 400, (170, 0, 0)),
    (2, 1600, (255, 165, 0)),
    (3, 1600, (255, 255, 0)),
    (4, 3200, (0, 255, 0)),
    (5, 4500, (0, 255, 255)),
    (6, 5000, (0, 0, 255)),
    (7, 6400, (128, 0, 128)),
]
ACTIVE_STAGE = 2

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def font(name: str, size: int):
    path = os.path.join(FONT_DIR, name)
    if os.path.exists(path):
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def rounded(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def main() -> int:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    sans = font("DejaVuSans.ttf", 20)
    sans_sm = font("DejaVuSans.ttf", 17)
    sans_bold = font("DejaVuSans-Bold.ttf", 22)
    mono = font("DejaVuSansMono.ttf", 20)
    mono_sm = font("DejaVuSansMono.ttf", 17)

    # --- the bar ------------------------------------------------------------
    d.rectangle((0, 0, W, 52), fill=BAR)
    d.text((28, 16), "1  2  3  4", font=sans, fill=DIM)

    widget_x = W - 250
    rounded(d, (widget_x - 14, 8, W - 28, 44), 8, fill=(44, 44, 52))
    d.text((widget_x, 14), "94%", font=sans, fill=FG)
    d.text((widget_x + 58, 14), "1600 DPI", font=sans, fill=DIM)
    d.text((widget_x - 40, 13), "●", font=sans, fill=ACCENT)

    # --- the panel ----------------------------------------------------------
    px, py, pw = 700, 56, 540
    ph = 740
    rounded(d, (px, py, px + pw, py + ph), 12, fill=PANEL, outline=LINE, width=1)

    d.text((px + 28, py + 26), "G-Wolves HSK Pro 4K", font=sans_bold, fill=FG)
    d.text((px + 28, py + 56), "94%  ·  1600 DPI  ·  4000 Hz  ·  dongle",
           font=sans_sm, fill=DIM)
    d.text((px + pw - 56, py + 30), "↻", font=sans_bold, fill=DIM)

    y = py + 100
    d.line((px + 24, y, px + pw - 24, y), fill=LINE, width=1)

    y += 20
    d.text((px + 28, y), "DPI STAGES", font=mono_sm, fill=DIM)
    y += 32

    for stage, dpi, colour in STAGES:
        active = stage == ACTIVE_STAGE
        row_h = 40
        if active:
            rounded(d, (px + 20, y - 6, px + pw - 20, y + row_h - 10), 6,
                    fill=(46, 46, 56))

        glyph = "●" if active else "○"
        d.text((px + 34, y), glyph, font=sans, fill=FG if active else DIM)
        d.text((px + 66, y), str(stage), font=sans, fill=FG)

        # stepper: [-]  value  [+]
        bx = px + 250
        for offset, label in ((0, "−"), (150, "+")):
            rounded(d, (bx + offset, y - 2, bx + offset + 34, y + 32), 5,
                    outline=DIM, width=1)
            d.text((bx + offset + 12, y + 2), label, font=sans, fill=FG)
        value = str(dpi)
        vw = d.textlength(value, font=mono)
        d.text((bx + 92 - vw / 2, y + 2), value, font=mono, fill=FG)

        # colour swatch
        sx = px + pw - 76
        rounded(d, (sx, y + 1, sx + 26, y + 27), 4, fill=colour, outline=DIM,
                width=1)
        y += row_h

    y += 12
    d.line((px + 24, y, px + pw - 24, y), fill=LINE, width=1)
    y += 20
    d.text((px + 28, y), "POLLING RATE  (Hz)", font=mono_sm, fill=DIM)
    y += 32

    # polling rate segmented control
    rates = ["250", "500", "1000", "2000", "4000"]
    seg_x, seg_w = px + 28, (pw - 56) / len(rates)
    for i, rate in enumerate(rates):
        chosen = rate == "4000"
        box = (seg_x + i * seg_w, y - 2, seg_x + (i + 1) * seg_w - 6, y + 32)
        rounded(d, box, 5, fill=(60, 76, 100) if chosen else None,
                outline=ACCENT if chosen else LINE, width=1)
        tw = d.textlength(rate, font=sans_sm)
        d.text((box[0] + (seg_w - 6 - tw) / 2, y + 5), rate, font=sans_sm,
               fill=FG if chosen else DIM)

    y += 52
    d.line((px + 24, y, px + pw - 24, y), fill=LINE, width=1)
    y += 20
    d.text((px + 28, y), "SENSOR", font=mono_sm, fill=DIM)
    y += 32

    for label, on in (("Motion Sync", True), ("Angle snapping", False)):
        d.text((px + 34, y), label, font=sans_sm, fill=FG)
        tx = px + pw - 96
        rounded(d, (tx, y + 2, tx + 52, y + 28), 13,
                fill=(60, 96, 140) if on else (52, 52, 60))
        knob = tx + 28 if on else tx + 4
        d.ellipse((knob, y + 5, knob + 21, y + 26), fill=FG if on else DIM)
        y += 38

    d.text((px + 34, y), "Lift-off distance", font=sans_sm, fill=FG)
    d.text((px + pw - 96, y), "1 mm", font=sans_sm, fill=DIM)

    # --- caption ------------------------------------------------------------
    cx, cy = 70, 220
    d.text((cx, cy), "HSK Mouse", font=font("DejaVuSans-Bold.ttf", 54), fill=FG)
    d.text((cx, cy + 76),
           "Configure a G-Wolves HSK Pro 4K\nfrom the Omarchy bar.",
           font=font("DejaVuSans.ttf", 26), fill=DIM, spacing=10)

    bullets = [
        "Seven DPI stages, each with its own LED colour",
        "250 - 4000 Hz polling, measured not guessed",
        "Battery, motion sync, angle snap, lift-off",
        "No libratbag, no daemon, no vendor software",
    ]
    by = cy + 180
    for line in bullets:
        d.text((cx, by), "•", font=sans, fill=ACCENT)
        d.text((cx + 26, by), line, font=sans, fill=FG)
        by += 40

    out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview.png"
    )
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out}  ({W}x{H})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
