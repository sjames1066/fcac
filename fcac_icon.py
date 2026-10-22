"""
FCAC icon: a phone-app-style rounded tile (purple to orange) with a white airliner and cabin-announcement waves.
Drawn with Pillow so it ships inside the app. Run this file to export fcac.ico and fcac_icon.png:

    python fcac_icon.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

PURPLE = (84, 30, 176)
MAGENTA = (206, 58, 128)  # mid stop keeps the blend vivid instead of muddy grey-pink
ORANGE = (255, 146, 24)
WORK = 1024  # everything is drawn large and scaled down for smooth edges


def _background(size: int) -> Image.Image:
    """Diagonal purple (top left) to orange (bottom right) gradient in a rounded square."""
    vertical = Image.linear_gradient("L").resize((size, size))
    t = ImageChops.add(vertical, vertical.rotate(90), scale=2)  # 0 top left ... 255 bottom right
    first = t.point(lambda v: min(255, v * 2))                  # purple -> magenta over the first half
    second = t.point(lambda v: max(0, (v - 128) * 2))           # magenta -> orange over the second half
    solid = lambda colour: Image.new("RGB", (size, size), colour)
    img = Image.composite(solid(MAGENTA), solid(PURPLE), first)
    img = Image.composite(solid(ORANGE), img, second)
    tile = Image.new("L", (size, size), 0)
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.23), fill=255)
    img = img.convert("RGBA")
    img.putalpha(tile)
    return img


def _airliner(size: int) -> Image.Image:
    """White top-down airliner pointing up, on a transparent layer."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = size / 2
    u = size / 1024
    white = (255, 255, 255, 255)

    def pt(x: float, y: float) -> tuple[float, float]:
        return (c + (x - 512) * u, y * u)

    d.rounded_rectangle((*pt(463, 150), *pt(561, 880)), radius=49 * u, fill=white)           # fuselage
    d.polygon([pt(x, y) for x, y in [(463, 420), (90, 660), (90, 730), (463, 600),             # main wings
                                       (561, 600), (934, 730), (934, 660), (561, 420)]], fill=white)
    d.polygon([pt(x, y) for x, y in [(463, 745), (285, 860), (285, 910), (463, 850),           # tailplane
                                       (561, 850), (739, 910), (739, 860), (561, 745)]], fill=white)
    return layer


def _waves(size: int, center: tuple[float, float], angle_deg: float) -> Image.Image:
    """Three announcement-style arcs fanning out from `center` in direction `angle_deg` (0 = right, clockwise)."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for radius, alpha in ((105, 255), (170, 200), (235, 140)):
        box = (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius)
        d.arc(box, angle_deg - 32, angle_deg + 32, fill=(255, 255, 255, alpha), width=26)
    return layer


def _build(detail: bool) -> Image.Image:
    img = _background(WORK)
    plane = _airliner(WORK)
    scale = 0.74 if detail else 0.88
    turn = -40  # banked, nose up and to the right
    plane = plane.rotate(turn, resample=Image.BICUBIC, center=(WORK / 2, WORK / 2))
    plane = plane.resize((int(WORK * scale), int(WORK * scale)), Image.LANCZOS)
    offset = ((WORK - plane.width) // 2 - (36 if detail else 0), (WORK - plane.height) // 2 + (22 if detail else 0))

    shadow = Image.new("RGBA", (WORK, WORK), (0, 0, 0, 0))
    shadow.paste((45, 10, 90, 110), (offset[0], offset[1] + 18), plane.split()[3])
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(14)))
    img.alpha_composite(plane, offset)

    if detail:
        # nose position after rotation + scale, then fan the waves out ahead of it
        angle = math.radians(turn)
        nose_x = WORK / 2 + (512 - 150) * 0 + 362 * math.sin(-angle) * scale  # (0,-362) rotated by `turn`
        nose_y = WORK / 2 - 362 * math.cos(angle) * scale
        nose = (nose_x + offset[0] - (WORK - plane.width) // 2, nose_y + offset[1] - (WORK - plane.height) // 2)
        direction = -90 - turn  # up is -90 deg in PIL's arc space; banking right adds to it
        ahead = (nose[0] + 38 * math.cos(math.radians(direction)), nose[1] + 38 * math.sin(math.radians(direction)))
        waves = _waves(WORK, ahead, direction)
        img = Image.alpha_composite(img, waves)

    # keep only the rounded tile
    img.putalpha(ImageChops.multiply(img.split()[3], _background(WORK).split()[3]))
    return img


def make_icon(size: int = 256) -> Image.Image:
    """The FCAC icon as an RGBA image. Small sizes drop the waves so the aircraft stays readable."""
    return _build(detail=size > 40).resize((size, size), Image.LANCZOS)


def main() -> int:
    here = Path(__file__).resolve().parent
    big = make_icon(256)
    big.save(here / "fcac_icon.png")
    big.save(here / "fcac.ico", format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Wrote {here / 'fcac.ico'} and {here / 'fcac_icon.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
