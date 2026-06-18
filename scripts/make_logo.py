"""Generate the hermes-imessage-adapter logo.

Outputs:
  - assets/logo.png       (1200×1200, the GitHub README hero)
  - assets/logo-small.png (256×256, favicon/avatar)

Design:
  - Dark base (#0a0a0a) matching the ClawPlex colorway
  - iMessage-green speech bubble (#34da5b) — rounded square with tail
  - White lightning bolt inside the bubble (the "agent delivery" signal)
  - Electric-blue (#3b82f6) circuit traces in the background corners
  - No text on the logo — README provides the title separately

Run from the repo root: `uv run --with Pillow python scripts/make_logo.py`.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# ---- palette (kept in sync with the README badge colorway) ----
BG = (10, 10, 10)               # #0a0a0a
BG_INNER = (26, 26, 26)         # #1a1a1a
BUBBLE = (52, 218, 91)          # #34da5b iMessage green
BUBBLE_DARK = (24, 142, 56)     # darker green for shadow
BUBBLE_HIGHLIGHT = (148, 255, 175)  # lighter green rim
ACCENT = (59, 130, 246)         # #3b82f6 electric blue
WHITE = (250, 250, 250)         # #fafafa
BUBBLE_GLOW = (52, 218, 91, 90)  # translucent green for outer glow


def make_layer(size: int) -> Image.Image:
    """Create the base dark canvas with subtle radial vignette."""
    img = Image.new("RGBA", (size, size), BG + (255,))
    draw = ImageDraw.Draw(img)
    # radial vignette — lighter center, dark edges
    cx = cy = size / 2
    max_r = size * 0.7
    for i in range(80, 0, -1):
        t = i / 80
        r = max_r * t
        col = (
            int(BG_INNER[0] * (1 - (1 - t) * 0.6)),
            int(BG_INNER[1] * (1 - (1 - t) * 0.6)),
            int(BG_INNER[2] * (1 - (1 - t) * 0.6)),
            255,
        )
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=col,
        )
    return img


def draw_circuit_traces(img: Image.Image, size: int) -> None:
    """Draw subtle electric-blue circuit lines in the corners.

    Suggests automation/agent infrastructure without competing with the
    bubble. Uses a few stepped paths in each corner, with end-dots.
    """
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad = size * 0.08
    span = size * 0.20
    line_w = max(1, size // 400)
    dot_r = max(2, size // 400)

    for corner in [
        (pad, pad, 1, 1),                # top-left
        (size - pad, pad, -1, 1),        # top-right
        (pad, size - pad, 1, -1),        # bottom-left
        (size - pad, size - pad, -1, -1),  # bottom-right
    ]:
        x, y, dx, dy = corner
        for offset in (0, size * 0.025, size * 0.05):
            sx = x + offset * abs(dx)
            sy = y + offset * abs(dy)
            midx = sx + span * dx
            midy = sy
            endx = midx
            endy = midy + span * dy
            d.line([(sx, sy), (midx, midy), (endx, endy)], fill=ACCENT + (160,), width=line_w)
            d.ellipse((endx - dot_r, endy - dot_r, endx + dot_r, endy + dot_r), fill=ACCENT + (220,))
            d.ellipse((sx - dot_r // 2, sy - dot_r // 2, sx + dot_r // 2, sy + dot_r // 2), fill=WHITE + (220,))
    layer = layer.filter(ImageFilter.GaussianBlur(radius=size / 600))
    img.alpha_composite(layer)


def draw_bubble(img: Image.Image, size: int) -> None:
    """Draw an iMessage-style speech bubble (rounded square with tail).

    Uses a rounded square as the body and a tail polygon on top, then
    subtracts the notch between the tail and the body so the silhouette
    is one continuous shape with no visible seam.
    """
    cx = size / 2
    cy = size * 0.46  # slightly above center to leave room for tail
    half = size * 0.28  # half-side of the rounded square
    corner_r = size * 0.10  # corner radius
    tail_h = size * 0.09  # how far the tail drops below the body
    tail_w = size * 0.12  # how wide the tail base is

    left = cx - half
    right = cx + half
    top = cy - half
    bottom = cy + half

    # Outer glow first
    glow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.rounded_rectangle(
        (left - size * 0.04, top - size * 0.04, right + size * 0.04, bottom + tail_h * 0.7),
        radius=corner_r * 1.3,
        fill=BUBBLE_GLOW,
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=size / 70))
    img.alpha_composite(glow_layer)

    # Build the bubble as a rounded rectangle + tail drawn on a single
    # layer, then composite. Pillow's rounded_rectangle doesn't support
    # notches, so we draw the rect and tail as one operation by filling
    # the tail triangle and letting it overlap the bottom edge of the
    # rect.  The visual result is seamless because the fill is the same
    # green.
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # Body: rounded rectangle
    d.rounded_rectangle(
        (left, top, right, bottom),
        radius=corner_r,
        fill=BUBBLE,
    )

    # Tail: triangle from the bottom edge slightly left of center,
    # pointing down and to the left. The base of the tail overlaps the
    # rect's bottom edge, so the join is invisible.
    tail_base_left = cx - tail_w * 0.65
    tail_base_right = cx - tail_w * 0.05
    tail_tip_x = cx - tail_w * 0.55
    tail_tip_y = bottom + tail_h
    d.polygon(
        [
            (tail_base_left, bottom),
            (tail_base_right, bottom),
            (tail_tip_x, tail_tip_y),
        ],
        fill=BUBBLE,
    )

    # No top-left highlight arc — the iMessage aesthetic is flat +
    # shadow, not glossy.  A bright crescent here read as a balloon.

    img.alpha_composite(layer)


def draw_lightning(img: Image.Image, size: int) -> None:
    """Draw a white lightning bolt centered inside the bubble.

    The bolt is the visual signal that the message is being sent at
    agent-speed — a Hermes "fast messenger" iconography. Hand-drawn
    polygon path, not a font glyph, for clean scale.
    """
    cx = size / 2
    cy = size * 0.46
    w = size * 0.08   # half-width
    h_top = size * 0.20
    h_bot = size * 0.20

    # Standard lightning bolt: top, zig left, mid-left, bottom, zig right, mid-right
    pts = [
        (cx + w * 0.3, cy - h_top),            # top
        (cx - w * 0.9, cy + h_top * 0.05),     # mid-left tip (zags left)
        (cx - w * 0.1, cy + h_top * 0.05),     # inner-left
        (cx - w * 0.5, cy + h_bot),            # bottom
        (cx + w * 0.7, cy - h_bot * 0.05),     # mid-right tip (zags right)
        (cx - w * 0.1, cy - h_bot * 0.05),     # inner-right
    ]

    # Soft drop shadow under the bolt
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    shifted = [(x + size * 0.005, y + size * 0.008) for (x, y) in pts]
    sd.polygon(shifted, fill=(0, 0, 0, 100))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=size / 300))
    img.alpha_composite(shadow)

    # The bolt itself
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.polygon(pts, fill=WHITE)
    img.alpha_composite(layer)


def draw_corner_pulse(img: Image.Image, size: int) -> None:
    """Add a small 'live' pulse dot in the bottom-right — agent is online."""
    cx = size * 0.82
    cy = size * 0.82
    r = size * 0.025
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((cx - r * 2.5, cy - r * 2.5, cx + r * 2.5, cy + r * 2.5), fill=(52, 218, 91, 70))
    layer = layer.filter(ImageFilter.GaussianBlur(radius=size / 200))
    img.alpha_composite(layer)
    d2 = ImageDraw.Draw(img)
    d2.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(148, 255, 175, 255))


def make_logo(size: int) -> Image.Image:
    img = make_layer(size)
    draw_circuit_traces(img, size)
    draw_bubble(img, size)
    draw_lightning(img, size)
    draw_corner_pulse(img, size)
    return img


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)

    for size, name in [(1200, "logo.png"), (256, "logo-small.png")]:
        img = make_logo(size)
        out = Image.new("RGB", (size, size), BG)
        out.paste(img, mask=img.split()[3])
        out_path = out_dir / name
        out.save(out_path, "PNG", optimize=True)
        print(f"wrote {out_path} ({size}×{size}, {out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
