// Shared "What needs my attention" feed: fetched once by the shell, consumed
// by the dashboard, the notification drawer and the bell badge.

import { createContext, useContext } from "react";
import { type AttentionFeed, type AttentionItemData } from "./api";
import { AttentionItem, Button, Icon, type IconName } from "./components";

export const AttentionCtx = createContext<{
  feed: AttentionFeed | null;
  reload: () => void;
}>({ feed: null, reload: () => {} });

export function useAttention() {
  return useContext(AttentionCtx);
}

const KNOWN_ICONS: IconName[] = [
  "alert", "review", "clock", "wand", "sync", "key", "classroom", "file", "info",
];

export function AttentionPanel({ onNavigate, compact }: {
  onNavigate: (hash: string) => void; compact?: boolean;
}) {
  const { feed, reload } = useAttention();
  const items = feed?.items ?? [];
  return (
    <div>
      <div className="row between" style={{ marginBottom: 10 }}>
        <span className="small muted">
          {feed ? `${items.length} item${items.length === 1 ? "" : "s"} need you` : "Checking…"}
        </span>
        <Button size="sm" variant="ghost" icon="refresh" onClick={reload}>Refresh</Button>
      </div>
      {items.length === 0 ? (
        <div className="notice ok">
          <Icon name="check-circle" size={16} />
          <span>Nothing needs your attention right now.</span>
        </div>
      ) : (
        items.map((item, i) => (
          <AttentionRow key={i} item={item} onNavigate={onNavigate} compact={compact} />
        ))
      )}
    </div>
  );
}

function AttentionRow({ item, onNavigate, compact }: {
  item: AttentionItemData; onNavigate: (hash: string) => void; compact?: boolean;
}) {
  const icon = KNOWN_ICONS.includes(item.icon as IconName)
    ? (item.icon as IconName) : "info";
  return (
    <AttentionItem
      icon={icon}
      tone={item.tone}
      title={compact ? `${item.title} (${item.count})` : item.title}
      detail={item.detail}
      onAction={() => onNavigate(item.route)}
      action={<Icon name="chevron-right" size={16} style={{ color: "var(--text-faint)" }} />}
    />
  );
}
