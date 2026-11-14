"""Render the cube icon to a multi-size ICO using Pillow only."""

from PIL import Image, ImageDraw

SIZE = 256


def make_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # background rounded square with gradient (approximated by solid + subtle)
    radius = int(size * 0.22)
    # simple two-stop gradient via horizontal interpolation
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    for x in range(size):
        t = x / size
        r = int(14 + (99 - 14) * t)
        g = int(165 + (102 - 165) * t)
        b = int(233 + (241 - 233) * t)
        bg_draw.line([(x, 0), (x, size)], fill=(r, g, b, 255))
    # rounded mask
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (size, size)], radius=radius, fill=255)
    img.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(img)
    s = size
    cx, cy = s / 2, s / 2
    # cube vertices (isometric-ish)
    top = (cx, cy - s * 0.20)
    tr = (cx + s * 0.22, cy - s * 0.08)
    br = (cx + s * 0.22, cy + s * 0.16)
    bot = (cx, cy + s * 0.28)
    bl = (cx - s * 0.22, cy + s * 0.16)
    tl = (cx - s * 0.22, cy - s * 0.08)
    mid = (cx, cy)

    lw = max(3, int(s * 0.035))
    white = (255, 255, 255, 255)

    # outer hexagon
    draw.line([top, tr, br, bot, bl, tl, top], fill=white, width=lw, joint="curve")
    # center vertical
    draw.line([top, mid], fill=white, width=lw)
    draw.line([mid, bot], fill=white, width=lw)
    # top Y
    draw.line([tl, mid, tr], fill=white, width=lw)
    # center dot
    dot_r = max(4, int(s * 0.045))
    draw.ellipse(
        [mid[0] - dot_r, mid[1] - dot_r, mid[0] + dot_r, mid[1] + dot_r],
        fill=white,
    )
    return img


if __name__ == "__main__":
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [make_icon(sz) for sz in sizes]
    images[0].save(
        "assets/icon.ico",
        format="ICO",
        sizes=[(sz, sz) for sz in sizes],
    )
    # also save a PNG preview at 256
    make_icon(256).save("assets/icon.png")
    print("Saved assets/icon.ico and assets/icon.png")
