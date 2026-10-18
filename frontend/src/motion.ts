// Motion utilities (motion spec §12).
//
// Rules encoded here so every page follows them:
//  * motion is transform+opacity only, short (200-280ms), and never delays an
//    action or hides a state change;
//  * prefers-reduced-motion collapses everything to the final state — the
//    stylesheet kills animations globally, and these hooks skip the animated
//    path entirely so no JavaScript timer churn remains either.

import { useEffect, useRef, useState } from "react";

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Re-renders when the user's motion preference changes. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion);
  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  return reduced;
}

/**
 * Count-up for loaded statistics (600-900ms, ease-out, tabular numerals).
 *
 * Runs ONCE per numeric value — never on every poll refresh — and snaps
 * straight to the answer under reduced motion or when the value is not
 * finite. Values are display-only; the real number is in `target` the whole
 * time for assistive tech via the caller's rendering of `value`.
 */
export function useCountUp(target: number, durationMs = 750): number {
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(target);
  const fromRef = useRef(target);
  const ranFor = useRef<number | null>(null);

  useEffect(() => {
    if (!Number.isFinite(target)) { setShown(target); return; }
    if (reduced) { setShown(target); return; }
    // Only animate the transition INTO a value (first load / big change),
    // not the ±1 drift of live polling.
    if (ranFor.current !== null && Math.abs(target - ranFor.current) <= 1) {
      ranFor.current = target;
      setShown(target);
      return;
    }
    const from = fromRef.current;
    const start = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
      setShown(from + (target - from) * eased);
      if (t < 1) frame = requestAnimationFrame(step);
      else { fromRef.current = target; ranFor.current = target; }
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, reduced, durationMs]);

  useEffect(() => { fromRef.current = shown; }, [shown]);
  return shown;
}

/** Staggered entrance style for index-driven lists (max ~6 visible steps). */
export function staggerStyle(index: number): React.CSSProperties {
  return { animationDelay: `${Math.min(index, 5) * 40}ms` };
}
