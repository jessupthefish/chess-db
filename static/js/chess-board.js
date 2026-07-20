// Shared Chessground/chess.js helpers used by every interactive board on the
// site (game detail, position search, puzzles, board-image "play from here",
// and the opening explorer) so they stay mechanically identical instead of
// drifting via copy-paste.

import { Chess } from 'https://esm.sh/chess.js@1.0.0-beta.8';

// Legal-move destinations for the current side to move, in the shape
// Chessground's `movable.dests` expects: Map<fromSquare, [toSquare, ...]>.
export function computeDests(fen) {
  const chess = new Chess(fen);
  const dests = new Map();
  for (const m of chess.moves({ verbose: true })) {
    if (!dests.has(m.from)) dests.set(m.from, []);
    dests.get(m.from).push(m.to);
  }
  return dests;
}

// Wires a promotion-picker element (four buttons with data-piece="q/r/b/n")
// to a callback fired once the user picks a piece. Returns { show, hide,
// isPending } so the caller can trigger it from its own move-legality check
// and query it (e.g. from a keydown handler) without tracking state twice.
export function createPromotionPicker(pickerEl, onPick) {
  let pending = null;

  pickerEl.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-piece]');
    if (!btn || !pending) return;
    const { orig, dest } = pending;
    pending = null;
    pickerEl.style.display = 'none';
    onPick(orig, dest, btn.dataset.piece);
  });

  return {
    show(orig, dest) {
      pending = { orig, dest };
      pickerEl.style.display = 'flex';
    },
    hide() {
      pending = null;
      pickerEl.style.display = 'none';
    },
    isPending() {
      return pending !== null;
    },
  };
}

// Display string for one /api/analyze-position line: mate score, or pawns
// with a sign, or an em-dash when neither is present.
export function formatEvalScore(line) {
  if (line.mate_in != null) return `M${Math.abs(line.mate_in)}`;
  if (line.score_cp == null) return '—';
  const pawns = (line.score_cp / 100).toFixed(1);
  return line.score_cp > 0 ? `+${pawns}` : pawns;
}

// Replays a plain SAN move list from a FEN via chess.js, building HTML with
// one hoverable <span class="eval-move" data-fen="..."> per move — each
// carrying the cumulative position *at that exact move*, precomputed once —
// plus lichess-style move-number prefixes (e.g. "14." / "14...") as plain
// text between them. Lets a caller preview any point along a line, not just
// its final position.
function buildPvSpans(fen, sans) {
  if (!sans.length) return '';
  const chess = new Chess(fen);
  const fields = fen.split(' ');
  let moveNo = parseInt(fields[5], 10) || 1;
  let whiteToMove = fields[1] !== 'b';
  const parts = sans.map((san, i) => {
    let prefix = '';
    if (whiteToMove) {
      prefix = `${moveNo}.`;
    } else if (i === 0) {
      prefix = `${moveNo}...`;
    }
    let moveFen = fen;
    try {
      chess.move(san);
      moveFen = chess.fen();
    } catch (e) { /* malformed SAN — span still renders, just won't preview correctly */ }
    if (!whiteToMove) moveNo++;
    whiteToMove = !whiteToMove;
    return `${prefix}<span class="eval-move" data-fen="${moveFen}">${san}</span>`;
  });
  return parts.join(' ');
}

// Renders /api/analyze-position's `lines` array into the .eval-line markup
// shared by every analysis panel on the site: a rank badge, the score, and
// the full move-numbered preview line. When `fen`+`cg` (the position being
// analyzed and its Chessground instance) are given, hovering any individual
// move within a line previews the board exactly as of that move, reverting
// to the real position on mouse-leave.
export function renderEvalLines(container, lines, fen, cg) {
  if (!lines.length) {
    container.innerHTML = '<span class="stat-sub">No lines returned.</span>';
    return;
  }
  container.innerHTML = lines.map((l) => {
    const sans = (l.preview_san || l.best_move_san || '').split(' ').filter(Boolean);
    const movesHtml = fen ? buildPvSpans(fen, sans) : sans.join(' ');
    return `
      <div class="eval-line">
        <span class="eval-rank-badge rank-${l.rank}">${l.rank}</span>
        <div class="eval-line-body">
          <span class="eval-line-score">${formatEvalScore(l)}</span>
          <span class="eval-line-moves">${movesHtml}</span>
        </div>
      </div>
    `;
  }).join('');

  if (!fen || !cg) return;
  container.querySelectorAll('.eval-move').forEach((span) => {
    span.addEventListener('mouseenter', () => {
      cg.set({ fen: span.dataset.fen, movable: { dests: new Map() } });
    });
    span.addEventListener('mouseleave', () => {
      cg.set({ fen, movable: { dests: computeDests(fen) } });
    });
  });
}

// Converts a top engine line into a 0-100 white-advantage percentage
// (lichess's own sigmoid curve) and updates the eval bar's fill height.
// `orientation` flips which edge the white fill grows from when the board
// is showing Black's perspective, via the `.flipped` CSS class.
export function updateEvalBar(barEl, topLine, orientation) {
  if (!barEl) return;
  const fillEl = barEl.querySelector('.eval-bar-fill');
  if (!fillEl) return;
  let pct = 50;
  if (topLine) {
    if (topLine.mate_in != null) {
      pct = topLine.mate_in > 0 ? 99 : 1;
    } else if (topLine.score_cp != null) {
      pct = 50 + 50 * (2 / (1 + Math.exp(-0.00368 * topLine.score_cp)) - 1);
    }
  }
  pct = Math.max(1, Math.min(99, pct));
  barEl.classList.toggle('flipped', orientation === 'black');
  fillEl.style.height = `${pct}%`;
}

const BOARD_SIZE_KEY = 'chessdb.boardSize';
const BOARD_SIZE_MIN = 320;
const BOARD_SIZE_MAX = 900;

// Applies any saved board size from a previous visit, and wires up a drag
// handle (bottom-right corner of the page's .board-wrap) so the user can
// resize the board directly. Every board container on the site shares the
// .board class and reads the same --board-size custom property, so setting
// it once at :root resizes whichever board is on the current page.
export function initBoardResize() {
  const saved = parseInt(localStorage.getItem(BOARD_SIZE_KEY), 10);
  if (saved >= BOARD_SIZE_MIN && saved <= BOARD_SIZE_MAX) {
    document.documentElement.style.setProperty('--board-size', `${saved}px`);
  }

  const wrap = document.querySelector('.board-wrap');
  if (!wrap) return;

  const handle = document.createElement('div');
  handle.className = 'board-resize-handle';
  handle.title = 'Drag to resize the board';
  handle.textContent = '⇲';
  wrap.appendChild(handle);

  function currentSize() {
    const boardEl = wrap.querySelector('.board');
    return boardEl ? boardEl.getBoundingClientRect().width : 480;
  }

  let dragging = false;
  let startX = 0;
  let startY = 0;
  let startSize = 0;

  handle.addEventListener('pointerdown', (e) => {
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    startSize = currentSize();
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  handle.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const delta = Math.max(e.clientX - startX, e.clientY - startY);
    const size = Math.min(BOARD_SIZE_MAX, Math.max(BOARD_SIZE_MIN, Math.round(startSize + delta)));
    document.documentElement.style.setProperty('--board-size', `${size}px`);
  });

  function endDrag() {
    if (!dragging) return;
    dragging = false;
    localStorage.setItem(BOARD_SIZE_KEY, Math.round(currentSize()));
  }

  handle.addEventListener('pointerup', endDrag);
  handle.addEventListener('pointercancel', endDrag);
}
