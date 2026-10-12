/**
 * Tooltip layer — one shared, viewport-aware tooltip renderer for the whole app.
 *
 * Why this exists: the previous tooltips were pseudo-elements
 * (`[data-tooltip]::after`) positioned inside the page. Two real defects came
 * out of that:
 *
 *   1. An absolutely positioned tooltip is laid out even while invisible, so
 *      every page whose header or card holds one reported phantom horizontal
 *      overflow. `.main-scroll { overflow-x: hidden }` then CLIPPED the tooltip
 *      itself, so a button near the right edge showed a truncated hint.
 *   2. `white-space: nowrap` meant a long hint could never wrap, so on a narrow
 *      window there was no way to read it at all.
 *
 * The layer fixes both at the root: the tooltip is portalled to <body> and
 * positioned `fixed`, so it is outside every scrolling/clipping ancestor, it
 * wraps inside the viewport, and it flips to whichever side actually has room
 * (honouring `data-tooltip-side="right"` for the collapsed nav rail).
 *
 * Everything is delegated from the document, so pages keep writing the same
 * `data-tooltip` / `tooltip=` attributes and nothing else has to change.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

type Side = "top" | "bottom" | "right" | "left";

interface Anchor {
  text: string;
  side: Side;
  rect: { left: number; top: number; right: number; bottom: number; width: number; height: number };
}

const GAP = 9;          // distance between the trigger and the plate
const EDGE = 8;         // minimum distance from the viewport edge
const POINTER_DELAY = 320;  // ms of deliberate hovering before a mouse tooltip appears
const FOCUS_DELAY = 0;      // keyboard focus needs an immediate answer

/** The nearest element carrying a non-empty tooltip, if the event came from one. */
function triggerFor(node: EventTarget | null): HTMLElement | null {
  if (!(node instanceof Element)) return null;
  const el = node.closest<HTMLElement>("[data-tooltip]");
  if (!el) return null;
  return (el.getAttribute("data-tooltip") || "").trim() ? el : null;
}

function sideOf(el: HTMLElement): Side {
  const raw = el.getAttribute("data-tooltip-side");
  return raw === "right" || raw === "left" || raw === "bottom" ? raw : "top";
}

export function TooltipLayer() {
  const [tip, setTip] = useState<Anchor | null>(null);
  const plate = useRef<HTMLDivElement | null>(null);
  const anchor = useRef<HTMLElement | null>(null);
  const previousDescribed = useRef<string | null>(null);

  // Position after render but before paint, so it never flashes at a stale spot.
  useLayoutEffect(() => {
    const node = plate.current;
    if (!node || !tip) return;
    const p = node.getBoundingClientRect();
    const a = tip.rect;
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;

    const room: Record<Side, boolean> = {
      top: a.top - GAP - p.height >= EDGE,
      bottom: a.bottom + GAP + p.height <= vh - EDGE,
      right: a.right + GAP + p.width <= vw - EDGE,
      left: a.left - GAP - p.width >= EDGE,
    };
    const order: Side[] = tip.side === "right"
      ? ["right", "top", "bottom", "left"]
      : tip.side === "bottom"
        ? ["bottom", "top", "right", "left"]
        : ["top", "bottom", "right", "left"];
    const side = order.find((s) => room[s]) ?? order[0];

    let left: number;
    let top: number;
    if (side === "right") {
      left = a.right + GAP;
      top = a.top + (a.height - p.height) / 2;
    } else if (side === "left") {
      left = a.left - GAP - p.width;
      top = a.top + (a.height - p.height) / 2;
    } else if (side === "top") {
      top = a.top - GAP - p.height;
      left = a.left + (a.width - p.width) / 2;
    } else {
      top = a.bottom + GAP;
      left = a.left + (a.width - p.width) / 2;
    }
    left = Math.min(Math.max(EDGE, left), Math.max(EDGE, vw - p.width - EDGE));
    top = Math.min(Math.max(EDGE, top), Math.max(EDGE, vh - p.height - EDGE));
    node.style.left = `${Math.round(left)}px`;
    node.style.top = `${Math.round(top)}px`;
    node.dataset.side = side;
    node.dataset.ready = "true";
  }, [tip]);

  useEffect(() => {
    let timer: number | undefined;

    const stop = () => {
      if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
    };

    const hide = () => {
      stop();
      const el = anchor.current;
      if (el) {
        const prev = previousDescribed.current;
        if (prev === null) el.removeAttribute("aria-describedby");
        else el.setAttribute("aria-describedby", prev);
      }
      anchor.current = null;
      previousDescribed.current = null;
      setTip(null);
    };

    const show = (el: HTMLElement) => {
      const text = (el.getAttribute("data-tooltip") || "").trim();
      if (!text) return;
      const r = el.getBoundingClientRect();
      setTip({
        text,
        side: sideOf(el),
        rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height },
      });
    };

    const schedule = (el: HTMLElement, delay: number) => {
      stop();
      if (anchor.current === el) return;   // already showing for this trigger
      hide();
      anchor.current = el;                  // claim it now so repeats are ignored
      previousDescribed.current = el.getAttribute("aria-describedby");
      if (delay > 0) {
        timer = window.setTimeout(() => { if (anchor.current === el) show(el); }, delay);
      } else {
        show(el);
      }
    };

    const onPointerOver = (e: PointerEvent) => {
      const el = triggerFor(e.target);
      if (!el) return;
      if (el === anchor.current) return;
      schedule(el, POINTER_DELAY);
    };

    const onPointerOut = (e: PointerEvent) => {
      const el = triggerFor(e.target);
      if (!el) return;
      const to = e.relatedTarget;
      if (to instanceof Node && el.contains(to)) return;
      if (anchor.current === el) hide();
    };

    const onFocusIn = (e: FocusEvent) => {
      const el = triggerFor(e.target);
      if (el) schedule(el, FOCUS_DELAY);
    };

    const onFocusOut = (e: FocusEvent) => {
      const el = triggerFor(e.target);
      if (!el) return;
      const to = e.relatedTarget;
      if (to instanceof Node && el.contains(to)) return;
      if (anchor.current === el) hide();
    };

    const onKeyDown = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };
    const onScrollOrResize = () => hide();

    document.addEventListener("pointerover", onPointerOver, true);
    document.addEventListener("pointerout", onPointerOut, true);
    document.addEventListener("focusin", onFocusIn, true);
    document.addEventListener("focusout", onFocusOut, true);
    document.addEventListener("pointerdown", onScrollOrResize, true);
    document.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);

    return () => {
      stop();
      document.removeEventListener("pointerover", onPointerOver, true);
      document.removeEventListener("pointerout", onPointerOut, true);
      document.removeEventListener("focusin", onFocusIn, true);
      document.removeEventListener("focusout", onFocusOut, true);
      document.removeEventListener("pointerdown", onScrollOrResize, true);
      document.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, []);

  // Keep the association for assistive technology in step with what is shown.
  useEffect(() => {
    if (!tip || !anchor.current) return;
    anchor.current.setAttribute("aria-describedby", "mc-tooltip-layer");
  }, [tip]);

  if (!tip || typeof document === "undefined") return null;
  return createPortal(
    <div ref={plate} id="mc-tooltip-layer" role="tooltip" data-ready="false"
      className="tooltip-layer">
      {tip.text}
    </div>,
    document.body,
  );
}

export default TooltipLayer;
