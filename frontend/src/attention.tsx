// Shared "What needs my attention" feed: fetched once by the shell, consumed
// by the dashboard, the notification drawer and the bell badge.

import { createContext, useContext } from "react";
import { type AttentionFeed, type AttentionItemData } from "./api";
import { AttentionItem, Button, Icon, type IconName } from "./components";
import { friendlyError } from "./errors";

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

export function AttentionPanel({ onNavigate }: {
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
          <AttentionRow key={i} item={item} onNavigate={onNavigate} />
        ))
      )}
    </div>
  );
}

/** Backend copy is precise but mechanical ("1 student(s)"). Make it read
 *  like a sentence without changing any number. */
function polishTitle(title: string): string {
  return title
    .replace(/\b1 student\(s\)/g, "1 student")
    .replace(/\b(\d+) student\(s\)/g, "$1 students")
    .replace(/\b1 piece\(s\)/g, "1 piece")
    .replace(/\b(\d+) piece\(s\)/g, "$1 pieces")
    .replace(/\b1 batch\(es\)/g, "1 batch")
    .replace(/\b(\d+) batch\(es\)/g, "$1 batches")
    .replace(/\b1 submission\(s\)/g, "1 submission")
    .replace(/\b(\d+) submission\(s\)/g, "$1 submissions")
    .replace(/^./, (c) => c.toUpperCase());
}

function AttentionRow({ item, onNavigate }: {
  item: AttentionItemData; onNavigate: (hash: string) => void; compact?: boolean;
}) {
  const icon = KNOWN_ICONS.includes(item.icon as IconName)
    ? (item.icon as IconName) : "info";
  // Provider payloads are diagnosis, not conversation: translate the known
  // failures and keep the raw text available on hover.
  const plain = item.detail ? friendlyError(item.detail) : null;
  const detail = plain
    ? [plain.summary, plain.action].filter(Boolean).join(" ")
    : undefined;
  return (
    <AttentionItem
      icon={icon}
      tone={item.tone}
      title={polishTitle(item.title)}
      count={item.count > 1 && !item.title.includes(String(item.count)) ? item.count : undefined}
      detail={detail}
      detailTitle={plain?.raw}
      onAction={() => onNavigate(item.route)}
      action={<Icon name="chevron-right" size={16} style={{ color: "var(--text-faint)" }} />}
    />
  );
}
