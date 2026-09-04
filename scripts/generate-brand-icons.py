from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = PROJECT_ROOT / "assets"
PUBLIC_DIR = PROJECT_ROOT / "frontend" / "public"

GREEN = "#103d2c"
MINT = "#75c7a1"
CREAM = "#f4f0e6"


def _rounded_line(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], width: int, fill: str) -> None:
    radius = width // 2
    draw.line(points, fill=fill, width=width, joint="curve")
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def render_master(size: int = 1024) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 1024

    def box(values: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return tuple(round(value * scale) for value in values)

    draw.rounded_rectangle(box((36, 36, 988, 988)), radius=round(232 * scale), fill=GREEN)
    draw.rounded_rectangle(
        box((54, 54, 970, 970)),
        radius=round(214 * scale),
        outline=MINT,
        width=max(1, round(24 * scale)),
    )

    draw.polygon(
        [(round(x * scale), round(y * scale)) for x, y in ((224, 340), (288, 152), (448, 328))],
        fill=CREAM,
    )
    draw.polygon(
        [(round(x * scale), round(y * scale)) for x, y in ((576, 328), (736, 152), (800, 340))],
        fill=CREAM,
    )
    draw.polygon(
        [(round(x * scale), round(y * scale)) for x, y in ((270, 285), (294, 215), (354, 282))],
        fill=MINT,
    )
    draw.polygon(
        [(round(x * scale), round(y * scale)) for x, y in ((670, 282), (730, 215), (754, 285))],
        fill=MINT,
    )

    stroke = round(116 * scale)
    _rounded_line(draw, [(round(290 * scale), round(380 * scale)), (round(290 * scale), round(744 * scale))], stroke, CREAM)
    _rounded_line(draw, [(round(290 * scale), round(390 * scale)), (round(734 * scale), round(744 * scale))], stroke, CREAM)
    _rounded_line(draw, [(round(734 * scale), round(380 * scale)), (round(734 * scale), round(744 * scale))], stroke, CREAM)
    draw.ellipse(box((756, 756, 876, 876)), fill=MINT)
    return image


def main() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    master = render_master()
    master.save(BRAND_DIR / "neko-ai.png")
    master.save(
        BRAND_DIR / "neko-ai.ico",
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    master.save(
        PUBLIC_DIR / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )
    for filename, size in (
        ("favicon-32x32.png", 32),
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),
    ):
        master.resize((size, size), Image.Resampling.LANCZOS).save(PUBLIC_DIR / filename)


if __name__ == "__main__":
    main()
