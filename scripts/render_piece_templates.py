"""
scripts/render_piece_templates.py — build the "cburnett" piece-shape template
set used by board_vision.py's template-matching recognizer.

Source: the 12 piece SVGs in scripts/cburnett_svg/, extracted from
chessground's own bundled cburnett piece set (the same art this app's board
already renders, and visually the same set Lichess ships as its default
"cburnett" piece theme) — pulled from the public chessground npm package via
its CDN-hosted CSS (base64-embedded SVGs), not scraped from any live board.

Templates are piece SHAPE only (a binary silhouette mask, not the coloured
artwork) at a fixed size, because chessground's cburnett SVGs use the same
outline shape for a piece regardless of color — cell classification matches
shape here and determines white/black separately from a brightness check on
the matched region (see board_vision.py). This makes one shape template set
usable across any board whose piece art follows the same white-fill/
dark-outline vs dark-fill/light-outline convention as cburnett, which covers
this app's own board and Lichess's default look. Chess.com's piece art is
proprietary and different — matching it well would need real chess.com
screenshots to bootstrap from (the recognizer falls back to the Claude vision
API for boards the template set doesn't confidently match).

Run once (or whenever scripts/cburnett_svg/ changes):
    python3 scripts/render_piece_templates.py
"""

import os

import cairosvg
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SVG_DIR = os.path.join(HERE, "cburnett_svg")
OUT_DIR = os.path.join(os.path.dirname(HERE), "static", "piece-templates", "cburnett")

TEMPLATE_SIZE = 64  # px, square — matches the cell size board_vision.py resizes to
PIECES = ["P", "N", "B", "R", "Q", "K"]  # shape-only: color determined separately


def render_shape_mask(svg_path: str, size: int) -> np.ndarray:
    """Rasterize one piece SVG to a binary shape mask (uint8, 0/255) at
    size x size, using the SVG's alpha channel (pieces are drawn on a
    transparent background) as the silhouette."""
    png_bytes = cairosvg.svg2png(url=svg_path, output_width=size, output_height=size)
    img = Image.open(__import__("io").BytesIO(png_bytes)).convert("RGBA")
    alpha = np.array(img)[:, :, 3]
    return np.where(alpha > 40, 255, 0).astype(np.uint8)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    written = []
    for piece in PIECES:
        # white and black cburnett SVGs share the same outline shape — use
        # the white variant as the canonical shape source (wP.svg etc.)
        svg_path = os.path.join(SVG_DIR, f"w{piece}.svg")
        mask = render_shape_mask(svg_path, TEMPLATE_SIZE)
        out_path = os.path.join(OUT_DIR, f"{piece}.png")
        Image.fromarray(mask, mode="L").save(out_path)
        written.append(out_path)

    print(f"wrote {len(written)} shape templates to {OUT_DIR}")
    for p in written:
        print(" ", os.path.basename(p))


if __name__ == "__main__":
    main()
