/**
 * Workspace pane width math (marking workspace §11).
 *
 * Pure and dependency-free so `frontend/tests/paneWidths.test.ts` can cover
 * it: the resizable workspace keeps explicit widths for every pane except the
 * last, and the LAST pane must always keep at least `min` pixels. A teacher
 * who dragged the leading panes wide in a big window and then narrowed the
 * window used to leave the result pane a sliver — the whole marking result
 * then overflowed sideways inside it.
 *
 * Dividers are 8px wide with 4px margins on each side.
 */

export const DIVIDER_WIDTH = 16;

/**
 * Return widths fitted to `containerW`: shrink the leading panes (widest
 * first) until the last pane would still get at least `min`. Panes never go
 * below `min`; if even the floors do not fit, the layout simply overflows
 * (the stacked layout takes over below 900px of viewport anyway).
 */
export function fitPaneWidths(
  widths: readonly number[],
  count: number,
  containerW: number,
  min: number,
): number[] {
  const out = widths.slice();
  if (!containerW || count < 2) return out;
  for (let i = 0; i < count - 1; i += 1) if (out[i] == null) out[i] = min;
  const budget = containerW - (count - 1) * DIVIDER_WIDTH - min;
  let total = 0;
  for (let i = 0; i < count - 1; i += 1) total += out[i] ?? min;
  while (total > budget) {
    let idx = -1;
    let widest = min;
    for (let i = 0; i < count - 1; i += 1) {
      if ((out[i] ?? min) > widest) { widest = out[i]; idx = i; }
    }
    if (idx < 0) break; // every leading pane is already at its floor
    const take = Math.min(total - budget, widest - min);
    out[idx] = widest - take;
    total -= take;
  }
  return out;
}
