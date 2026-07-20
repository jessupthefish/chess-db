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

// Renders /api/analyze-position's `lines` array into the .eval-line markup
// shared by every analysis panel on the site (rank swatch, score, preview
// SAN). Board-specific extras (drawing arrows on the board itself) stay in
// each caller — this only builds the text panel.
export function renderEvalLines(container, lines) {
  container.innerHTML = lines.map((l) => `
    <div class="eval-line">
      <span class="eval-line-rank rank-${l.rank}"></span>
      <span class="eval-line-score">${formatEvalScore(l)}</span>
      <span class="eval-line-moves">${l.preview_san || l.best_move_san || ''}</span>
    </div>
  `).join('') || '<span class="stat-sub">No lines returned.</span>';
}
