import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import type { JobStatus } from "./api";
import { Icon, type IconName } from "./Icons";

/* ================================================================ badges */

export type Tone = "ok" | "err" | "warn" | "info" | "mute";

export function Badge({ tone, children, icon }: {
  tone: Tone; children: ReactNode; icon?: IconName;
}) {
  return (
    <span className={`badge ${tone}`}>
      {icon ? <Icon name={icon} size={12} /> : <span className="dot" />}
      <span className="badge-text">{children}</span>
    </span>
  );
}

const STATUS_TONES: Record<JobStatus, Tone> = {
  QUEUED: "mute", PREPARING: "info", DOWNLOADING: "info", UPLOADING: "info",
  MARKING: "info", VERIFYING: "info", SAVING: "info",
  WAITING_FOR_API: "warn", RETRYING: "warn",
  COMPLETED: "ok", FAILED: "err", CANCELLED: "mute",
};

export function StatusBadge({ status }: { status: JobStatus }) {
  return <Badge tone={STATUS_TONES[status] ?? "mute"}>{status.replace(/_/g, " ")}</Badge>;
}

/* =============================================================== buttons */

export type ButtonVariant =
  | "default" | "primary" | "secondary" | "success" | "warning"
  | "danger" | "ghost" | "nav";

export function Button({
  variant = "secondary", size, icon, iconRight, loading, block, tooltip, className,
  children, ...rest
}: {
  variant?: ButtonVariant;
  size?: "sm" | "lg";
  icon?: IconName;
  iconRight?: IconName;
  loading?: boolean;
  block?: boolean;
  tooltip?: string;
  className?: string;
  children?: ReactNode;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const classes = [
    "btn", variant !== "default" ? variant : "",
    size ?? "", loading ? "loading" : "", block ? "block" : "", className ?? "",
  ].filter(Boolean).join(" ");
  return (
    <button
      className={classes}
      data-tooltip={tooltip}
      aria-busy={loading || undefined}
      disabled={rest.disabled || loading}
      {...rest}
    >
      {icon && <Icon name={icon} size={size === "sm" ? 14 : 16} />}
      {children}
      {iconRight && <Icon name={iconRight} size={size === "sm" ? 14 : 16} />}
    </button>
  );
}

export function IconButton({
  icon, label, variant = "ghost", size, active, ...rest
}: {
  icon: IconName; label: string; variant?: ButtonVariant; size?: "sm";
  active?: boolean;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={`btn ${variant} icon-only ${size ?? ""} ${active ? "active" : ""}`}
      aria-label={label}
      data-tooltip={label}
      {...rest}
    >
      <Icon name={icon} size={size === "sm" ? 15 : 17} />
    </button>
  );
}

/* ============================================================ stat tiles */

export function Stat({
  label, value, tone, compact,
}: { label: ReactNode; value: ReactNode; tone?: "ok" | "err" | "warn"; compact?: boolean }) {
  return (
    <div className={`stat ${compact ? "compact" : ""}`}>
      <div className="label">{label}</div>
      <div className={`value ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

/** Polished, clickable status card: icon + big value + supporting text. */
export function StatusCard({
  icon, value, label, sub, tone = "info", onClick,
}: {
  icon: IconName; value: ReactNode; label: ReactNode; sub?: ReactNode;
  tone?: "ok" | "err" | "warn" | "info" | "mute"; onClick?: () => void;
}) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag
      className={`status-card tone-${tone} ${onClick ? "clickable" : ""}`}
      onClick={onClick}
      type={onClick ? "button" : undefined}
    >
      <div className="sc-icon"><Icon name={icon} size={20} /></div>
      <div style={{ minWidth: 0 }}>
        <div className="sc-value">{value}</div>
        <div className="sc-label">{label}</div>
        {sub != null && <div className="sc-sub">{sub}</div>}
      </div>
    </Tag>
  );
}

/** A single "needs attention" row. */
export function AttentionItem({
  icon, tone = "info", title, detail, action, onAction,
}: {
  icon: IconName; tone?: "ok" | "err" | "warn" | "info";
  title: ReactNode; detail?: ReactNode; action?: ReactNode; onAction?: () => void;
}) {
  return (
    <div className={`attention-item tone-${tone} ${onAction ? "clickable" : ""}`}
      onClick={onAction} role={onAction ? "button" : undefined} tabIndex={onAction ? 0 : undefined}
      onKeyDown={onAction ? (e) => { if (e.key === "Enter") onAction(); } : undefined}>
      <span className="ai-icon"><Icon name={icon} size={17} /></span>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="ai-title">{title}</div>
        {detail != null && <div className="ai-detail">{detail}</div>}
      </div>
      {action}
    </div>
  );
}

export function Progress({ value, thin, tone, label }: {
  value: number; thin?: boolean; tone?: "ok";
  /** Accessible name, e.g. "Correcting Yahia Mohamed". Without it a progress
   *  bar is announced as an unlabelled "0%" — useless to a screen reader. */
  label?: string;
}) {
  const pct = Math.round(Math.min(100, Math.max(0, value * 100)));
  return (
    <div className={`progress ${thin ? "thin" : ""} ${tone ? `tone-${tone}` : ""}`}
      role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
      aria-valuetext={`${pct}%`} aria-label={label}>
      <div className="bar" style={{ width: `${pct}%` }} />
    </div>
  );
}

/**
 * Keyboard activation props for click-only rows and cards.
 *
 * A `<tr onClick>` is invisible to keyboard users: it is not focusable and
 * Enter/Space do nothing. These props give any element the same contract a
 * button has — focusable, Enter and Space activate it — without nesting a
 * <button> inside a <tr> (invalid table markup).
 */
export function rowActivate(onActivate: () => void) {
  return {
    tabIndex: 0,
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
      // Space must not scroll the page, and Enter must not follow a nested link.
      e.preventDefault();
      e.stopPropagation();
      onActivate();
    },
  };
}

/* ============================================================== states */

export function Empty({ icon, title, hint, action }: {
  icon: string | IconName; title: ReactNode; hint?: ReactNode; action?: ReactNode;
}) {
  const isName = typeof icon === "string" && /^[a-z-]+$/.test(icon);
  return (
    <div className="empty">
      <div className="big">{isName ? <Icon name={icon as IconName} size={30} /> : <span>{icon}</span>}</div>
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint faint">{hint}</div>}
      {action && <div style={{ marginTop: 14, display: "flex", justifyContent: "center" }}>{action}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="stack" role="status" aria-live="polite">
      <div className="skeleton" />
      <div className="skeleton-lines">
        <div className="skeleton" />
        <div className="skeleton" />
      </div>
      <span className="faint small">{label}</span>
    </div>
  );
}

export function ErrorState({ title, detail, onRetry }: {
  title: string; detail?: string; onRetry?: () => void;
}) {
  return (
    <div className="card" style={{ borderColor: "var(--err-line)" }}>
      <div className="empty" style={{ padding: "var(--s6) var(--s4)" }}>
        <div className="big" style={{ color: "var(--err)" }}><Icon name="alert" size={30} /></div>
        <div className="empty-title" style={{ color: "var(--err)" }}>{title}</div>
        {detail && <div className="empty-hint faint">{detail}</div>}
        {onRetry && (
          <Button variant="secondary" size="sm" icon="refresh" style={{ marginTop: 12 }} onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    </div>
  );
}

/* ============================================================ page frame */

export function PageHeader({ title, sub, children, icon }: {
  title: string; sub?: string; children?: ReactNode; icon?: IconName;
}) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div className="row between wrap" style={{ gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <h1 className="page-title" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {icon && <Icon name={icon} size={26} style={{ color: "var(--accent)" }} />}
            {title}
          </h1>
          {sub && <p className="page-sub" style={{ marginBottom: 0 }}>{sub}</p>}
        </div>
        {children && <div className="row wrap">{children}</div>}
      </div>
    </div>
  );
}

/** Section heading with optional right-aligned actions. */
export function Section({ title, hint, actions, children, icon }: {
  title: ReactNode; hint?: ReactNode; actions?: ReactNode; children: ReactNode; icon?: IconName;
}) {
  return (
    <div className="card">
      <div className="row between wrap" style={{ marginBottom: 12, gap: 8 }}>
        <h3 style={{ margin: 0 }}>
          {icon && <Icon name={icon} size={16} style={{ color: "var(--accent)" }} />}
          {title}
          {hint != null && <span className="hint">{hint}</span>}
        </h3>
        {actions && <div className="row wrap">{actions}</div>}
      </div>
      {children}
    </div>
  );
}

export function KV({ k, v }: { k: ReactNode; v: ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

/* =============================================================== tables */

export function DataTable({ head, children, minWidth }: {
  head: ReactNode; children: ReactNode; minWidth?: number;
}) {
  return (
    <div className="table-wrap">
      <table className="table table-stack" style={minWidth ? { minWidth } : undefined}>
        <thead>{head}</thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/* =========================================================== segmented */

export function Segmented<T extends string>({ value, options, onChange, ariaLabel }: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={value === o.value ? "active" : ""}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ============================================================== tabs */

export function Tabs<T extends string>({ value, options, onChange, ariaLabel }: {
  value: T;
  options: { value: T; label: string; icon?: IconName }[];
  onChange: (v: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={ariaLabel}>
      {options.map((o) => (
        <button key={o.value} type="button" role="tab"
          className={`tab ${value === o.value ? "active" : ""}`}
          aria-selected={value === o.value} onClick={() => onChange(o.value)}>
          {o.icon && <Icon name={o.icon} size={15} />}
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ============================================================ dropdown */

export function Dropdown({ label, children, align, icon }: {
  label: ReactNode; children: (close: () => void) => ReactNode;
  align?: "left" | "right"; icon?: IconName;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") setOpen(false); }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="dropdown" ref={ref}>
      <button type="button" className="btn secondary sm" aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen((v) => !v)}>
        {icon && <Icon name={icon} size={14} />}
        {label}
        <Icon name="chevron-down" size={13} />
      </button>
      {open && (
        <div className="dropdown-menu" role="menu"
          style={align === "left" ? { left: 0, right: "auto" } : undefined}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

/* ================================================================ modal */

/* Shared overlay behaviour: Escape closes, Tab cycles inside the panel, the
   background stops scrolling while it is open, and focus returns where it
   came from. Without the scroll lock a teacher can open a modal and then
   scroll the page behind it into a state they never clicked. */
function useOverlayBehaviour(ref: React.RefObject<HTMLElement>,
                             onClose: () => void) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const node = ref.current;
    const previous = document.activeElement as HTMLElement | null;

    const scroller = document.querySelector<HTMLElement>(".main-scroll");
    const prevOverflow = scroller ? scroller.style.overflow : "";
    if (scroller) scroller.style.overflow = "hidden";

    const focusables = () => Array.from(
      node?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), '
        + 'input:not([disabled]), select:not([disabled]), '
        + '[tabindex]:not([tabindex="-1"])') ?? []
    ).filter((el) => el.offsetParent !== null || el === document.activeElement);

    const target = node?.querySelector<HTMLElement>("[data-autofocus]")
      ?? focusables()[0] ?? node;
    target?.focus?.();

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") { closeRef.current(); return; }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === node)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (scroller) scroller.style.overflow = prevOverflow;
      previous?.focus?.();
    };
  }, [ref]);
}

export function Modal({ title, onClose, children, footer, wide, icon }: {
  title: ReactNode; onClose: () => void; children: ReactNode;
  footer?: ReactNode; wide?: boolean; icon?: IconName;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useOverlayBehaviour(dialog, onClose);

  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true"
        ref={dialog} tabIndex={-1}
        aria-label={typeof title === "string" ? title : undefined}>
        <div className="modal-head">
          <h3 style={{ display: "flex", alignItems: "center", gap: 9 }}>
            {icon && <Icon name={icon} size={18} style={{ color: "var(--accent)" }} />}
            {title}
          </h3>
          <IconButton icon="x" label="Close" onClick={onClose} />
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/* =============================================================== drawer */

export function Drawer({ title, onClose, children }: {
  title: ReactNode; onClose: () => void; children: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  useOverlayBehaviour(panel, onClose);

  return (
    <div className="drawer-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <aside className="drawer" role="dialog" aria-modal="true" ref={panel}
        tabIndex={-1}
        aria-label={typeof title === "string" ? title : undefined}>
        <div className="drawer-head">
          <h3 style={{ margin: 0 }}>{title}</h3>
          <IconButton icon="x" label="Close" onClick={onClose} />
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
      <div />
    </div>
  );
}

/* ================================================================ toasts */

type ToastTone = "ok" | "err" | "warn" | "info";
interface ToastItem { id: number; tone: ToastTone; message: string }

const ToastCtx = createContext<(tone: ToastTone, message: string) => void>(() => {});

export function useToast() {
  return useContext(ToastCtx);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const push = useCallback((tone: ToastTone, message: string) => {
    const id = nextId.current++;
    setItems((cur) => [...cur.slice(-4), { id, tone, message }]);
    window.setTimeout(() => {
      setItems((cur) => cur.filter((t) => t.id !== id));
    }, 5200);
  }, []);

  const value = useMemo(() => push, [push]);
  const iconFor = (tone: ToastTone): IconName =>
    tone === "ok" ? "check-circle" : tone === "err" ? "alert" : tone === "warn" ? "alert" : "info";

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast tone-${t.tone}`}>
            <Icon name={iconFor(t.tone)} size={16} style={{ marginTop: 1 }} />
            <span style={{ flex: 1, minWidth: 0 }}>{t.message}</span>
            <button className="toast-close" aria-label="Dismiss"
              onClick={() => setItems((cur) => cur.filter((x) => x.id !== t.id))}>✕</button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* =========================================================== export menu */

const EXPORT_LABELS: Record<string, string> = {
  csv: "CSV", xlsx: "Excel", pdf: "PDF", json: "JSON", md: "Markdown",
};

export function ExportMenu({ base, params, formats = ["csv", "xlsx", "pdf", "json"], onExported }: {
  base: string; params?: Record<string, string | undefined>;
  formats?: string[]; onExported?: (fmt: string) => void;
}) {
  const qs = useMemo(() => {
    const pairs = Object.entries(params ?? {})
      .filter(([, v]) => v != null && v !== "")
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    return pairs.length ? `&${pairs.join("&")}` : "";
  }, [params]);

  return (
    <Dropdown label="Export" icon="download" align="right">
      {(close) => (
        <>
          <div className="menu-label">Download as</div>
          {formats.map((f) => (
            <button key={f} role="menuitem" onClick={() => { close(); onExported?.(f); }}>
              <a href={`/api${base}?format=${f}${qs}`}
                style={{ color: "inherit", textDecoration: "none", display: "flex",
                  alignItems: "center", gap: 9, width: "100%" }}>
                <Icon name="file" size={15} />
                {EXPORT_LABELS[f] ?? f.toUpperCase()}
              </a>
            </button>
          ))}
        </>
      )}
    </Dropdown>
  );
}

export { Icon };
export type { IconName };
