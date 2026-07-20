"""
board_vision.py — recognize a chess position from a screenshot.

Two-stage pipeline:
  1. Template matching (fast, free, offline) against the cburnett piece
     shape set in static/piece-templates/cburnett/ — see
     scripts/render_piece_templates.py for provenance. Covers this app's own
     board and Lichess's default look (same piece art). Chess.com's
     proprietary piece art isn't in the template set, so those screenshots
     (and anything else the template matcher isn't confident about) fall
     through to step 2.
  2. Claude vision API fallback (opt-in via ANTHROPIC_API_KEY) — sends the
     image and asks for a FEN back via structured output. Costs a fraction
     of a cent per screenshot; only used when template matching's confidence
     is below THRESHOLD or fails a sanity check.

Neither stage ever calls Stockfish or does anything the project's
no-bulk-analysis rule would apply to — this is pure image recognition, not
engine work. Nothing is written to disk; images are processed in memory only.

The caller always gets an editable board back (see /import/board-image) —
recognition here only needs to get the user *close*, not perfect.
"""

from __future__ import annotations

import base64
import io
import logging
import os

import chess
import cv2
import numpy as np
from PIL import Image

log = logging.getLogger("board_vision")

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "piece-templates")
CELL_SIZE = 64
CONFIDENCE_THRESHOLD = 0.55  # below this, prefer the Claude fallback (or "failed")
MIN_FOREGROUND_FRACTION = 0.06  # cell foreground pixels below this -> treat as empty

CHESS_VISION_MODEL = os.environ.get("CHESS_VISION_MODEL", "claude-opus-4-8")


def _load_templates(theme: str) -> dict[str, np.ndarray]:
    theme_dir = os.path.join(TEMPLATE_DIR, theme)
    templates = {}
    for piece in ("P", "N", "B", "R", "Q", "K"):
        path = os.path.join(theme_dir, f"{piece}.png")
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is not None:
            templates[piece] = img
    return templates


_TEMPLATE_CACHE: dict[str, dict[str, np.ndarray]] = {}


def _templates_for(theme: str) -> dict[str, np.ndarray]:
    if theme not in _TEMPLATE_CACHE:
        _TEMPLATE_CACHE[theme] = _load_templates(theme)
    return _TEMPLATE_CACHE[theme]


def available_themes() -> list[str]:
    if not os.path.isdir(TEMPLATE_DIR):
        return []
    return sorted(
        d for d in os.listdir(TEMPLATE_DIR) if os.path.isdir(os.path.join(TEMPLATE_DIR, d))
    )


# ── board detection ─────────────────────────────────────────────────────


def detect_board(image: np.ndarray) -> np.ndarray | None:
    """Find the largest roughly-square quadrilateral in the image and
    perspective-correct it to a square crop. Returns None if no confident
    board region is found (caller should offer a manual crop instead).

    A chessboard's own grid lines produce dense internal Canny edges, so a
    naive largest-contour search can latch onto a jagged internal shape
    instead of the true outer boundary (confirmed against a synthetic
    checkerboard test image with no surrounding frame). The 'boxiness'
    check below (contour area vs. its bounding-box area) rejects those
    jagged false positives — a genuine board-boundary contour is close to
    its own bounding rectangle; internal grid-line noise is not.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    h, w = gray.shape
    image_area = h * w
    best = None
    best_area = 0

    for c in contours:
        area = cv2.contourArea(c)
        if area < image_area * 0.5:  # board should dominate a cropped screenshot
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        if ch == 0:
            continue
        bbox_area = cw * ch
        if bbox_area == 0 or area / bbox_area < 0.85:  # reject jagged/non-boxy contours
            continue
        aspect = cw / ch
        if not (0.85 <= aspect <= 1.15):  # boards are square-ish
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) != 4:
            approx = np.array([[x, y], [x + cw, y], [x + cw, y + ch], [x, y + ch]]).reshape(-1, 1, 2)
        if area > best_area:
            best_area = area
            best = approx

    if best is None:
        # fall back to: assume the whole image roughly IS the board (a
        # common case — many screenshots are already cropped tight)
        side = min(h, w)
        return cv2.resize(
            image[(h - side) // 2 : (h - side) // 2 + side, (w - side) // 2 : (w - side) // 2 + side],
            (CELL_SIZE * 8, CELL_SIZE * 8),
        )

    pts = best.reshape(-1, 2).astype("float32")
    rect = _order_corners(pts)
    dst_size = CELL_SIZE * 8
    dst = np.array([[0, 0], [dst_size - 1, 0], [dst_size - 1, dst_size - 1], [0, dst_size - 1]], dtype="float32")
    matrix = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(image, matrix, (dst_size, dst_size))


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).flatten()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(diff)], pts[np.argmax(s)], pts[np.argmax(diff)]],
        dtype="float32",
    )


# ── cell classification ─────────────────────────────────────────────────


def _cell_foreground_mask(cell_gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Extract a solid piece-silhouette mask from one cell via edge
    detection + largest-contour fill, rather than plain background-color
    subtraction. Plain difference-from-background thresholding fails
    whenever a piece's own fill color is close to its square's background
    (a white piece's near-white fill on a light square, or a black piece's
    near-black fill on a dark square) — verified against a synthetic board
    where that combination silently dropped most of the piece from the
    mask, leaving only the thin outline stroke and producing systematic
    shape/color misclassification. A stroke outline is always
    high-contrast against *something* by design (it's how piece art reads
    at a glance), so tracing edges and filling the resulting contour
    recovers the full silhouette regardless of the fill/background
    relationship. Returns (binary mask, foreground_fraction)."""
    edges = cv2.Canny(cell_gray, 30, 90)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
    # discard a thin border — anti-aliased square edges shouldn't count as piece
    border = 2
    edges[:border, :] = 0
    edges[-border:, :] = 0
    edges[:, :border] = 0
    edges[:, -border:] = 0

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask = np.zeros_like(cell_gray)
    if contours:
        largest = max(contours, key=cv2.contourArea)
        cv2.drawContours(mask, [largest], -1, 255, thickness=cv2.FILLED)
    fraction = float((mask > 0).sum()) / mask.size
    return mask, fraction


def _match_shape(mask: np.ndarray, templates: dict[str, np.ndarray]) -> tuple[str, float]:
    """Best-matching piece letter + normalized cross-correlation score."""
    mask_f = mask.astype(np.float32)
    best_piece, best_score = "P", -1.0
    for piece, tmpl in templates.items():
        tmpl_f = tmpl.astype(np.float32)
        result = cv2.matchTemplate(mask_f, tmpl_f, cv2.TM_CCOEFF_NORMED)
        score = float(result.max())
        if score > best_score:
            best_piece, best_score = piece, score
    return best_piece, best_score


def _piece_color(cell_gray: np.ndarray, mask: np.ndarray) -> str:
    """White pieces render light-filled with a dark outline; black pieces
    the reverse (a light outline on a dark fill). A raw bright-pixel count
    over-triggers on black pieces: their light outline stroke, once the
    full silhouette is filled solid (see _cell_foreground_mask), can cover
    enough pixels on detailed shapes (a king's crown, a bishop's mitre) to
    cross a naive brightness threshold. The distinguishing signal is shape,
    not quantity: a white piece's fill is one large solid bright *region*
    that survives erosion; a black piece's bright pixels form only a thin
    ring around the edge that erosion wipes out. Verified 32/32 correct
    across full rendered starting positions on two different board color
    schemes (all piece types, both colors, both square colors)."""
    bright = (((cell_gray > 200) & (mask > 0)).astype(np.uint8)) * 255
    eroded = cv2.erode(bright, np.ones((3, 3), np.uint8), iterations=1)
    core = int((eroded > 0).sum())
    return "w" if core / cell_gray.size > 0.02 else "b"


def classify_board(board_img: np.ndarray, theme: str) -> tuple[list[list[str | None]], float]:
    """8x8 grid (rank 8 at index 0, file a at index 0), each cell either
    None (empty) or a piece letter (uppercase=white, lowercase=black).
    Returns (grid, mean confidence over occupied squares)."""
    templates = _templates_for(theme)
    gray = cv2.cvtColor(board_img, cv2.COLOR_BGR2GRAY)
    side = gray.shape[0]
    cell_px = side // 8

    grid: list[list[str | None]] = [[None] * 8 for _ in range(8)]
    scores = []

    for row in range(8):
        for col in range(8):
            cell = gray[row * cell_px : (row + 1) * cell_px, col * cell_px : (col + 1) * cell_px]
            cell = cv2.resize(cell, (CELL_SIZE, CELL_SIZE))
            mask, fraction = _cell_foreground_mask(cell)
            if fraction < MIN_FOREGROUND_FRACTION:
                continue
            piece, score = _match_shape(mask, templates)
            color = _piece_color(cell, mask)
            grid[row][col] = piece if color == "w" else piece.lower()
            scores.append(score)

    confidence = float(np.mean(scores)) if scores else 0.0
    return grid, confidence


def _sanity_check(grid: list[list[str | None]]) -> bool:
    flat = [p for row in grid for p in row if p]
    if flat.count("K") != 1 or flat.count("k") != 1:
        return False
    if flat.count("P") > 8 or flat.count("p") > 8:
        return False
    for col in range(8):
        if grid[0][col] in ("P", "p") or grid[7][col] in ("P", "p"):
            return False  # pawns can't be on rank 8 or rank 1
    return True


def _grid_to_fen(grid: list[list[str | None]], side_to_move: str = "w") -> str:
    rows = []
    for row in grid:
        fen_row, empty = "", 0
        for cell in row:
            if cell is None:
                empty += 1
            else:
                if empty:
                    fen_row += str(empty)
                    empty = 0
                fen_row += cell
        if empty:
            fen_row += str(empty)
        rows.append(fen_row)
    placement = "/".join(rows)
    return f"{placement} {side_to_move} - - 0 1"


# ── Claude vision fallback ──────────────────────────────────────────────


def _recognize_via_claude(image_bytes: bytes, media_type: str = "image/png") -> dict | None:
    """Ask Claude to read the board straight from the screenshot. Returns
    {fen, side_to_move, confidence} or None on any failure (missing key,
    API error, malformed response) — caller treats None as 'failed'."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        log.warning("board_vision: anthropic package not installed, skipping vision fallback")
        return None

    schema = {
        "type": "object",
        "properties": {
            "placement_fen": {
                "type": "string",
                "description": "Just the piece-placement field of a FEN (ranks 8 to 1, "
                "'/'-separated, digits for empty runs) — no side-to-move or other fields.",
            },
            "side_to_move_guess": {
                "type": "string",
                "enum": ["w", "b", "unknown"],
                "description": "Whose turn it is, if determinable from the image (e.g. a "
                "highlighted clock or move indicator); 'unknown' otherwise.",
            },
            "confidence": {
                "type": "number",
                "description": "0-1 confidence that the placement_fen is fully correct.",
            },
        },
        "required": ["placement_fen", "side_to_move_guess", "confidence"],
        "additionalProperties": False,
    }

    try:
        client = anthropic.Anthropic(api_key=api_key)
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        response = client.messages.create(
            model=CHESS_VISION_MODEL,
            max_tokens=1024,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {
                            "type": "text",
                            "text": "This image contains a chess board (a screenshot from a "
                            "chess site, an app, or a photo of a physical board). Read the "
                            "position and return it as the piece-placement field of a FEN.",
                        },
                    ],
                }
            ],
        )
        if response.stop_reason == "refusal":
            log.warning("board_vision: Claude declined the vision request")
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        import json

        data = json.loads(text)
        placement = data["placement_fen"].strip()
        side = data.get("side_to_move_guess") or "w"
        if side == "unknown":
            side = "w"
        fen = f"{placement} {side} - - 0 1"
        chess.Board(fen)  # validates shape; raises ValueError if malformed
        return {
            "fen": fen,
            "side_to_move": side,
            "confidence": float(data.get("confidence", 0.5)),
        }
    except Exception:
        log.exception("board_vision: Claude vision fallback failed")
        return None


# ── entry point ──────────────────────────────────────────────────────────


def recognize(image_bytes: bytes, media_type: str = "image/png") -> dict:
    """Full pipeline: decode -> detect board -> template match -> (fallback
    to Claude vision if low confidence or a sanity check fails) -> result.

    Returns {fen, side_to_move, confidence, method, theme} where method is
    "template", "claude", or "failed" (fen is None when failed).
    """
    try:
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        cv_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    except Exception:
        log.exception("board_vision: failed to decode image")
        return {"fen": None, "side_to_move": "w", "confidence": 0.0, "method": "failed", "theme": None}

    board_img = detect_board(cv_img)
    if board_img is None:
        claude_result = _recognize_via_claude(image_bytes, media_type)
        if claude_result:
            return {**claude_result, "method": "claude", "theme": None}
        return {"fen": None, "side_to_move": "w", "confidence": 0.0, "method": "failed", "theme": None}

    best_theme, best_grid, best_confidence = None, None, -1.0
    for theme in available_themes() or ["cburnett"]:
        grid, confidence = classify_board(board_img, theme)
        if confidence > best_confidence:
            best_theme, best_grid, best_confidence = theme, grid, confidence

    if best_grid is not None and best_confidence >= CONFIDENCE_THRESHOLD and _sanity_check(best_grid):
        fen = _grid_to_fen(best_grid)
        return {
            "fen": fen,
            "side_to_move": "w",
            "confidence": round(best_confidence, 3),
            "method": "template",
            "theme": best_theme,
        }

    claude_result = _recognize_via_claude(image_bytes, media_type)
    if claude_result:
        return {**claude_result, "method": "claude", "theme": None}

    if best_grid is not None and not _sanity_check(best_grid):
        # a sanity-check failure (wrong king count, pawns on back ranks, ...)
        # means this isn't a plausible chess position at all — garbage in,
        # not just a low-confidence real board. Don't dress it up as a FEN.
        best_grid = None

    # low-confidence template result is still more useful than nothing —
    # hand it back so the editor starts pre-filled rather than blank, but
    # be honest about the method/confidence so the UI can warn the user
    if best_grid is not None:
        fen = _grid_to_fen(best_grid)
        return {
            "fen": fen,
            "side_to_move": "w",
            "confidence": round(max(best_confidence, 0.0), 3),
            "method": "template",
            "theme": best_theme,
        }
    return {"fen": None, "side_to_move": "w", "confidence": 0.0, "method": "failed", "theme": None}
