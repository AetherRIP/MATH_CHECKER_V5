/**
 * Sortable-table math + the `useSort` hook (shared list/table component).
 *
 * The pure core (state machine, comparators, sorting) lives here without any
 * React import side effects beyond the hook itself, so `frontend/tests/useSort.test.ts`
 * exercises exactly the logic the hook runs (§ testing: "Node tests cover
 * useSort / topic-group math").
 *
 * Design rules:
 *  * sorting NEVER mutates the input array — always a fresh copy;
 *  * an unsorted state returns the rows in their original order;
 *  * `null`/`undefined` always sort last, in both directions (a blank cell is
 *    never "the biggest number");
 *  * question numbers ("1", "2", "10", "1a") compare naturally, not as text.
 */
import { useMemo, useState } from "react";

export type SortDir = "asc" | "desc";

export interface SortState<K extends string = string> {
  /** Active column key, or null for "unsorted — original order". */
  key: K | null;
  dir: SortDir;
}

export type SortValue = string | number | null | undefined;

export interface SortColumn<T> {
  /** Extracts the comparable value for a row. */
  value: (row: T) => SortValue;
  /** Numeric comparison instead of natural text comparison. */
  numeric?: boolean;
  /**
   * Optional explicit ordering that wins over the extracted value
   * (e.g. triage severity). Lower rank sorts first ascending.
   */
  rank?: (row: T) => number;
}

export type SortColumns<T> = Record<string, SortColumn<T>>;

/** Initial state: optionally pre-sorted by one column. */
export function initialSort<K extends string = string>(
  key: K | null = null,
  dir: SortDir = "asc",
): SortState<K> {
  return { key, dir };
}

/**
 * The click cycle for one column header:
 *   other column → that column ascending;
 *   ascending    → descending;
 *   descending   → unsorted (back to the original order).
 */
export function nextSort<K extends string = string>(
  state: SortState<K>,
  key: K,
): SortState<K> {
  if (state.key !== key) return { key, dir: "asc" };
  if (state.dir === "asc") return { key, dir: "desc" };
  return { key: null, dir: "asc" };
}

/** aria-sort value for the header of `key`. */
export function ariaSortFor(
  state: SortState,
  key: string,
): "ascending" | "descending" | "none" {
  if (state.key !== key) return "none";
  return state.dir === "asc" ? "ascending" : "descending";
}

/**
 * Natural text comparison: digit runs compare as numbers, so "2" < "10" and
 * "1a" < "2". Case-insensitive; ties fall back to a plain locale-free
 * comparison so the result is deterministic across machines.
 */
export function naturalCompare(a: string, b: string): number {
  const re = /(\d+)|(\D+)/g;
  const ax = a.toLowerCase().match(re) ?? [];
  const bx = b.toLowerCase().match(re) ?? [];
  const n = Math.min(ax.length, bx.length);
  for (let i = 0; i < n; i += 1) {
    const at = ax[i];
    const bt = bx[i];
    const an = /^\d+$/.test(at);
    const bn = /^\d+$/.test(bt);
    if (an && bn) {
      const d = Number(at) - Number(bt);
      if (d !== 0) return d;
    } else if (at !== bt) {
      return at < bt ? -1 : 1;
    }
  }
  return ax.length - bx.length;
}

/** Null-safe value comparison. Blanks sort last in BOTH directions. */
export function compareValues(a: SortValue, b: SortValue, numeric?: boolean): number {
  const aBlank = a == null || a === "";
  const bBlank = b == null || b === "";
  if (aBlank || bBlank) return aBlank && bBlank ? 0 : aBlank ? 1 : -1;
  if (numeric || (typeof a === "number" && typeof b === "number")) {
    return Number(a) - Number(b);
  }
  return naturalCompare(String(a), String(b));
}

/**
 * Sort a copy of `rows` by the active column. Unsorted state → a copy in the
 * original order. Stable: equal rows keep their input order.
 */
export function sortRows<T>(
  rows: readonly T[],
  state: SortState,
  columns: SortColumns<T>,
): T[] {
  const out = rows.slice();
  if (!state.key) return out;
  const col = columns[state.key];
  if (!col) return out;
  const sign = state.dir === "asc" ? 1 : -1;
  return out.sort((ra, rb) => {
    if (col.rank) {
      const d = col.rank(ra) - col.rank(rb);
      if (d !== 0) return sign * d;
    }
    const a = col.value(ra);
    const b = col.value(rb);
    // Blanks are never the extreme: the rule is direction-INDEPENDENT, so it
    // must not go through the sign flip below.
    const aBlank = a == null || a === "";
    const bBlank = b == null || b === "";
    if (aBlank || bBlank) return aBlank && bBlank ? 0 : aBlank ? 1 : -1;
    return sign * compareValues(a, b, col.numeric);
  });
}

/**
 * Table sorting state + sorted rows + a header click handler.
 *
 * `columns` should be memoised by the caller (or defined at module level) so
 * the sorted memo does not recompute on every render.
 */
export function useSort<T>(
  rows: readonly T[],
  columns: SortColumns<T>,
  defaultSort: SortState = { key: null, dir: "asc" },
) {
  const [state, setState] = useState<SortState>(defaultSort);
  const sorted = useMemo(() => sortRows(rows, state, columns), [rows, state, columns]);
  const onSort = (key: string) => setState((s) => nextSort(s, key));
  return { state, sorted, onSort, ariaSortFor: (key: string) => ariaSortFor(state, key) };
}
