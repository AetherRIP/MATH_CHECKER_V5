import { useEffect, useMemo, useRef, useState } from "react";
import { api, type SearchResponse } from "./api";

/**
 * Global search (§15-§16): grouped results over the LOCAL database only —
 * never a Google API call per keystroke. Keyboard: `/` focuses, arrows move,
 * Enter opens, Esc closes. Clicking a result navigates via the hash, so
 * every destination stays bookmarkable.
 */
export default function SearchBox({ onNavigate }: { onNavigate: (hash: string) => void }) {
  const [query, setQuery] = useState("");
  const [data, setData] = useState<SearchResponse | null>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const requestId = useRef(0);

  const flat = useMemo(
    () => (data?.groups ?? []).flatMap((g) => g.items),
    [data],
  );

  // Debounced search against the local DB.
  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setData(null);
      setOpen(false);
      setLoading(false);
      return;
    }
    setLoading(true);
    const id = ++requestId.current;
    const timer = setTimeout(() => {
      api<SearchResponse>(`/search?q=${encodeURIComponent(q)}&limit=6`)
        .then((res) => {
          if (id !== requestId.current) return; // stale response
          setData(res);
          setActive(0);
          setOpen(true);
          setLoading(false);
        })
        .catch(() => {
          if (id === requestId.current) setLoading(false);
        });
    }, 180);
    return () => clearTimeout(timer);
  }, [query]);

  // `/` focuses the search from anywhere (unless already typing).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing = !!target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
      if (e.key === "/" && !typing) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Close when clicking outside.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  function go(index: number) {
    const item = flat[index];
    if (!item) return;
    setOpen(false);
    setQuery("");
    onNavigate(item.route);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (flat.length ? (i + 1) % flat.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (flat.length ? (i - 1 + flat.length) % flat.length : 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(active);
    } else if (e.key === "Escape") {
      setOpen(false);
      inputRef.current?.blur();
    }
  }

  let flatIndex = -1;

  return (
    <div className="search-box" ref={boxRef}>
      <span className="search-icon" aria-hidden>⌕</span>
      <input
        ref={inputRef}
        className="input search-input"
        type="search"
        value={query}
        placeholder="Search students, assignments, courses…"
        aria-label="Global search"
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => query.trim().length >= 2 && setOpen(true)}
        onKeyDown={onKeyDown}
      />
      {loading && open && <div className="search-panel"><div className="search-empty">Searching…</div></div>}
      {open && !loading && data && (
        <div className="search-panel" role="listbox">
          {data.total === 0 ? (
            <div className="search-empty">No matches for “{data.query}”</div>
          ) : (
            data.groups.map((group) => (
              <div key={group.key}>
                <div className="search-group-label">{group.label}</div>
                {group.items.map((item) => {
                  flatIndex += 1;
                  const index = flatIndex;
                  return (
                    <button
                      key={`${group.key}:${item.id}`}
                      type="button"
                      className={`search-item ${index === active ? "active" : ""}`}
                      role="option"
                      aria-selected={index === active}
                      onMouseEnter={() => setActive(index)}
                      onMouseDown={(e) => e.preventDefault() /* keep focus */}
                      onClick={() => go(index)}
                    >
                      <span className="t">{item.title}</span>
                      {item.subtitle && <span className="s">{item.subtitle}</span>}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
