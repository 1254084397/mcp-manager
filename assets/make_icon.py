"""Render a filled isometric cube icon to multi-size ICO using Pillow."""

from PIL import Image, ImageDraw

BASE = 512


def lerp(a, b, t):
    return int(a + (b - a) * t)


def gradient_bg(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            r = lerp(14, 99, t)
            g = lerp(165, 102, t)
            b = lerp(233, 241, t)
            px[x, y] = (r, g, b, 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (size, size)], radius=int(size * 0.22), fill=255
    )
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def make_icon(size: int) -> Image.Image:
    img = gradient_bg(size)
    draw = ImageDraw.Draw(img)
    s = size
    cx, cy = s / 2, s / 2

    # isometric cube: three visible faces with different shades of white
    top = (cx, cy - s * 0.22)
    tr = (cx + s * 0.24, cy - s * 0.10)
    br = (cx + s * 0.24, cy + s * 0.14)
    bot = (cx, cy + s * 0.26)
    bl = (cx - s * 0.24, cy + s * 0.14)
    tl = (cx - s * 0.24, cy - s * 0.10)

    # top face (lightest)
    draw.polygon([top, tr, (cx, cy), tl], fill=(255, 255, 255, 255))
    # left face (medium)
    draw.polygon([tl, (cx, cy), bl, (cx, cy - s * 0.36)], fill=(0, 0, 0, 0))  # placeholder
    # redraw left face properly
    draw.polygon([tl, (cx, cy), bot, bl], fill=(220, 225, 255, 255))
    # right face (darker)
    draw.polygon([tr, br, bot, (cx, cy)], fill=(195, 205, 255, 255))

    # thick outline
    lw = max(4, int(s * 0.028))
    outline = (255, 255, 255, 255)
    draw.line([top, tr, br, bot, bl, tl, top], fill=outline, width=lw, joint="curve")
    draw.line([top, (cx, cy)], fill=outline, width=lw)
    draw.line([tl, (cx, cy)], fill=outline, width=lw)
    draw.line([tr, (cx, cy)], fill=outline, width=lw)
    draw.line([(cx, cy), bot], fill=outline, width=lw)

    # center dot
    dot_r = max(5, int(s * 0.05))
    draw.ellipse(
        [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
        fill=(255, 255, 255, 255),
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
    make_icon(256).save("assets/icon.png")
    print("Saved assets/icon.ico and assets/icon.png")
