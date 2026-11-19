"""Vim-style Ctrl-W h/j/k/l: pick the neighbouring pane from on-screen rectangles."""
from __future__ import annotations

from typing import Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
Rect = tuple[int, int, int, int]  # x, y, width, height


def neighbor(rects: dict[K, Rect], current: K, direction: str,
             cursor: tuple[int, int] | None = None) -> K | None:
    """Closest pane in `direction` (h/j/k/l) that shares a row/column span with
    `current`. Like Vim, ties go to the pane lined up with the cursor."""
    x, y, w, h = rects[current]
    cx, cy = cursor if cursor else (x + w // 2, y + h // 2)
    best, best_key = None, None
    for k, (X, Y, W, H) in rects.items():
        if k == current:
            continue
        if direction in "hl":
            gap = x - (X + W) if direction == "h" else X - (x + w)
            lo, hi, mine, point = Y, Y + H, (y, y + h), cy
        elif direction in "jk":
            gap = y - (Y + H) if direction == "k" else Y - (y + h)
            lo, hi, mine, point = X, X + W, (x, x + w), cx
        else:
            raise ValueError(direction)
        if gap < 0 or hi <= mine[0] or lo >= mine[1]:
            continue  # wrong side, or no shared span
        off = 0 if lo <= point < hi else min(abs(point - lo), abs(point - (hi - 1)))
        key = (gap, off)
        if best_key is None or key < best_key:
            best, best_key = k, key
    return best
