// Dependency-free icon set (inline SVG, inherits currentColor).
//
// Why not an icon package: the app ships a prebuilt `frontend/dist` and must
// stay portable/offline-friendly, so we avoid adding a runtime dependency for
// what is, in the end, a handful of stroked paths. Icons are semantic (they
// name an object or action), consistent in stroke weight, and never decorative.

import type { SVGProps } from "react";

export type IconName =
  | "dashboard" | "class" | "assignments" | "students" | "reports"
  | "classroom" | "ai" | "settings" | "bell" | "search" | "sync"
  | "wand" | "review" | "download" | "upload" | "plus" | "edit" | "trash"
  | "external" | "chevron-left" | "chevron-right" | "chevron-down"
  | "check" | "check-circle" | "x" | "alert" | "info" | "clock" | "file"
  | "paperclip" | "sun" | "moon" | "menu" | "more" | "user" | "key" | "gear"
  | "link" | "lock" | "shield" | "play" | "pause" | "refresh" | "send"
  | "chart" | "grid" | "list" | "filter" | "logout" | "eye" | "tag"
  | "trending" | "activity" | "database" | "wifi" | "cpu" | "sparkles"
  | "inbox" | "cloud" | "party" | "arrow-right" | "arrow-left" | "copy";

const PATHS: Record<IconName, string> = {
  dashboard: "M3 3h7v7H3zM14 3h7v4h-7zM14 10h7v11h-7zM3 13h7v8H3z",
  class: "M3 7l9-4 9 4-9 4-9-4zM7 10v5c0 1 2.2 2.5 5 2.5s5-1.5 5-2.5v-5M21 8v6",
  assignments: "M9 4h6a2 2 0 0 1 2 2v0h1a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h1a2 2 0 0 1 2-2zM9 4a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 12l1.6 1.6L14 10M9 17h6",
  students: "M8 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2 20c0-3.3 2.7-6 6-6s6 2.7 6 6M17 11a3 3 0 1 0 0-6M16 20c0-2.2-1-4.2-2.5-5.5M22 20c0-2.5-1.7-4.6-4-5.3",
  reports: "M5 3h14a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM8 12v5M12 8v9M16 14v3",
  classroom: "M4 5h16v11H4zM9 20l3-4 3 4M9 9l2 2 4-4",
  ai: "M12 3v4M12 17v4M3 12h4M17 12h4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM5 5l3 3M16 16l3 3M19 5l-3 3M8 16l-3 3",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 0 1-4 0v-.1A1.6 1.6 0 0 0 7 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0-1.1-2.7H1a2 2 0 0 1 0-4h.1A1.6 1.6 0 0 0 2.6 7a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 7 2.6h.1A1.6 1.6 0 0 0 9 1V1a2 2 0 0 1 4 0v.1A1.6 1.6 0 0 0 15 2.6a1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1h.1a2 2 0 0 1 0 4h-.1a1.6 1.6 0 0 0-1.4 1z",
  bell: "M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  sync: "M21 12a9 9 0 0 1-15.3 6.4L3 16M3 12a9 9 0 0 1 15.3-6.4L21 8M21 4v4h-4M3 20v-4h4",
  refresh: "M20 11a8 8 0 1 0-2.3 6.3M20 4v4h-4",
  wand: "M15 4V2M15 10V8M11 6H9M21 6h-2M18.5 3.5l-1.4 1.4M18.5 8.5l-1.4-1.4M11.5 3.5l1.4 1.4M3 21l9-9M13.5 6.5l1 1",
  review: "M9 4h6a2 2 0 0 1 2 2v0h1a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h1a2 2 0 0 1 2-2zM9 4a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 13l2 2 4-4",
  download: "M12 3v12M7 10l5 5 5-5M4 19h16",
  upload: "M12 21V9M7 14l5-5 5 5M4 5h16",
  plus: "M12 5v14M5 12h14",
  edit: "M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3zM14 6l4 4",
  trash: "M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13M10 11v6M14 11v6",
  external: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  "chevron-left": "M15 5l-7 7 7 7",
  "chevron-right": "M9 5l7 7-7 7",
  "chevron-down": "M5 9l7 7 7-7",
  check: "M5 13l4 4L19 7",
  "check-circle": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8.5 12.5l2.5 2.5 4.5-5",
  x: "M6 6l12 12M18 6L6 18",
  alert: "M12 3l9 16H3zM12 10v4M12 17h.01",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  file: "M6 2h8l4 4v16a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM14 2v4h4M9 13h6M9 17h6",
  paperclip: "M21 11l-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 6",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  menu: "M3 6h18M3 12h18M3 18h18",
  more: "M12 6h.01M12 12h.01M12 18h.01",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21c0-4 3.6-7 8-7s8 3 8 7",
  key: "M15 7a4 4 0 1 1-3.5 5.9L10 14l-1.5 1.5L10 17l-1.5 1.5L7 17l-1.5 1.5L3 16l7-7A4 4 0 0 1 15 7z",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.3 1a7 7 0 0 0-1.7-1L14.5 3h-5l-.4 2.6a7 7 0 0 0-1.7 1l-2.3-1-2 3.4L4.1 11a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 1.7 1l.4 2.6h5l.4-2.6a7 7 0 0 0 1.7-1l2.3 1 2-3.4-2-1.5c.1-.3.1-.7.1-1z",
  link: "M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.5-1.5",
  lock: "M6 11h12a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1zM8 11V8a4 4 0 0 1 8 0v3",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4",
  play: "M7 4l12 8-12 8z",
  pause: "M8 5v14M16 5v14",
  send: "M22 2L11 13M22 2l-7 20-4-9-9-4z",
  chart: "M4 20V4M4 20h16M8 17v-5M12 17V9M16 17v-8",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  filter: "M3 4h18l-7 8v6l-4 2v-8z",
  logout: "M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l-5-5 5-5M5 12h11",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  tag: "M3 3h8l10 10-7 7L3 11z M7.5 7.5h.01",
  trending: "M3 17l6-6 4 4 8-8M15 7h6v6",
  activity: "M3 12h4l3 8 4-16 3 8h4",
  database: "M12 7c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3zM4 4v6c0 1.7 3.6 3 8 3s8-1.3 8-3V4M4 10v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
  wifi: "M2 8.5a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8.5 15.5a6 6 0 0 1 7 0M12 19h.01",
  cpu: "M6 6h12v12H6zM9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3",
  sparkles: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z",
  inbox: "M4 13l2-8h12l2 8v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1zM4 13h5l1 2h4l1-2h5",
  cloud: "M6 18a4 4 0 0 1 0-8 5.5 5.5 0 0 1 10.7-1.3A4 4 0 0 1 17 18z",
  party: "M4 20l4-10 10 10zM14 3l1 2 2-1-1 2 2 1-2 1M20 9l-2 1 1 2M9 4l1 1.5",
  "arrow-right": "M5 12h14M13 6l6 6-6 6",
  "arrow-left": "M19 12H5M11 6l-6 6 6 6",
  copy: "M9 9h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
};

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, "name"> {
  name: IconName;
  size?: number;
  filled?: boolean;
}

export function Icon({ name, size = 18, filled, style, ...rest }: IconProps) {
  const d = PATHS[name] ?? PATHS.info;
  return (
    <svg
      className="mk-icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      style={style}
      {...rest}
    >
      <path d={d} />
    </svg>
  );
}

export default Icon;
