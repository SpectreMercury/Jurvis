"""Builds every Jurvis icon from design/logo/source.png, the ink portrait of the butler (transparent background).

    .venv/bin/python design/logo/build.py

Writes the traced mark (design/logo/jurvis-mark.svg), the web icons (web/icon.svg, icon-192.png, icon-512.png)
the app icon set (native/Assets.xcassets/AppIcon.appiconset), and for GitHub the README logo (docs/logo.png)
and the repository's social preview (design/logo/github-social.png, uploaded by hand in the repository settings).
"""
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
INK = "#111111"
PAPER = "#ffffff"
CROP = (100, 22, 1000)        # x, y, side in source pixels: the head and bow tie; the shoulders run off the tile
HEAD = (270, 40, 700)         # the face alone, for icons too small to show the suit
RADIUS = 0.2237               # corner radius of a rounded tile, as a share of its side


def source():
    art = Image.open(HERE / "source.png").convert("RGBA")
    return Image.alpha_composite(Image.new("RGBA", art.size, PAPER), art).convert("L")


def trace(gray, scale=3):
    """The ink as one SVG path in source coordinates. Nested outlines alternate, so it fills with the even-odd rule."""
    big = cv2.resize(np.array(gray), None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    big = cv2.GaussianBlur(big, (0, 0), 1.2)
    ink = (big < 128).astype(np.uint8)
    outlines, _ = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    parts = []
    for outline in outlines:
        if cv2.contourArea(outline) < 6 * scale * scale:
            continue
        points = cv2.approxPolyDP(outline, 0.45 * scale, True).reshape(-1, 2) / scale
        if len(points) < 3:
            continue
        parts.append("M" + "L".join(f"{x:.1f} {y:.1f}".replace(".0", "") for x, y in points) + "Z")
    return "".join(parts)


def tile(gray, size, rounded, margin=0.0, shadow=False, crop=CROP):
    """The portrait on a white tile, `size` pixels square. `margin` is the empty share on each side (macOS wants one)."""
    x, y, side = crop
    over = 4
    inner = round(size * (1 - 2 * margin))
    art = gray.crop((x, y, x + side, y + side)).resize((inner * over, inner * over), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", art.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, art.width - 1, art.height - 1), radius=round(inner * over * RADIUS) if rounded else 0, fill=255)
    art.putalpha(mask)
    art = art.resize((inner, inner), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = (size - inner) // 2
    if shadow:
        shade = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        shade.paste((0, 0, 0, 70), (offset, offset + round(size * 0.012)), art.getchannel("A"))
        out = shade.filter(ImageFilter.GaussianBlur(size * 0.014))
    out.alpha_composite(art, (offset, offset))
    return out


def main():
    gray = source()
    path = trace(gray)
    w, h = gray.size
    (HERE / "jurvis-mark.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-label="Jurvis">'
        f'<path fill="{INK}" fill-rule="evenodd" d="{path}"/></svg>\n')
    x, y, side = CROP
    (ROOT / "web/icon.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {side} {side}" role="img" aria-label="Jurvis">'
        f'<defs><clipPath id="t"><rect x="{x}" y="{y}" width="{side}" height="{side}" rx="{side * RADIUS:.0f}"/></clipPath></defs>'
        f'<g clip-path="url(#t)"><rect x="{x}" y="{y}" width="{side}" height="{side}" fill="{PAPER}"/>'
        f'<path fill="{INK}" fill-rule="evenodd" d="{path}"/></g></svg>\n')
    for size in (192, 512):      # square and opaque: phones cut their own shape out of it
        tile(gray, size, rounded=False).convert("RGB").save(ROOT / f"web/icon-{size}.png", optimize=True)
    icons = ROOT / "native/Assets.xcassets/AppIcon.appiconset"
    tile(gray, 1024, rounded=False).convert("RGB").save(icons / "icon-1024.png", optimize=True)
    for points in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            tile(gray, points * scale, rounded=True, margin=100 / 1024, shadow=points >= 128,
                 crop=HEAD if points <= 32 else CROP).save(icons / f"mac-{points}-{scale}.png", optimize=True)
    tile(gray, 256, rounded=False).convert("RGB").save(ROOT / "native/Assets.xcassets/BrandMark.imageset/brandmark.png", optimize=True)
    tile(gray, 512, rounded=True).save(HERE / "preview.png")
    tile(gray, 320, rounded=True).save(ROOT / "docs/logo.png", optimize=True)
    card = Image.new("RGB", (1280, 640), PAPER)       # GitHub's social preview size
    card.paste(tile(gray, 640, rounded=False).convert("RGB"), (320, 0))
    card.save(HERE / "github-social.png", optimize=True)
    print("mark path:", len(path) // 1024, "KB")


if __name__ == "__main__":
    main()
