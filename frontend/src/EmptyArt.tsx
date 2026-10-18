/**
 * EmptyArt — small line-art scenes for empty states (§ empty states).
 *
 * Decorative only (`aria-hidden`); the title/hint next to it carry the
 * meaning. Deliberately hand-drawn-simple, single-colour (`currentColor`),
 * and inert — there is nothing to animate, so reduced motion needs no rule.
 */
export type EmptyArtVariant =
  | "paper" | "search" | "check" | "chart" | "inbox" | "cloud" | "key" | "generic";

const S = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

function Scene({ variant }: { variant: EmptyArtVariant }) {
  switch (variant) {
    case "paper":
      return (
        <>
          <path {...S} d="M30 12 h26 l12 12 v40 a4 4 0 0 1 -4 4 H30 a4 4 0 0 1 -4 -4 V16 a4 4 0 0 1 4 -4 z" />
          <path {...S} d="M56 12 v12 h12" />
          <path {...S} d="M35 36 h22 M35 45 h22 M35 54 h14" />
        </>
      );
    case "search":
      return (
        <>
          <circle {...S} cx="43" cy="36" r="16" />
          <path {...S} d="M55 48 L68 61" />
          <path {...S} d="M22 20 l3 6 6 3 -6 3 -3 6 -3 -6 -6 -3 6 -3 z" />
        </>
      );
    case "check":
      return (
        <>
          <circle {...S} cx="44" cy="40" r="19" />
          <path {...S} d="M35 40 l6 7 13 -14" />
          <path {...S} d="M70 18 l2 5 5 2 -5 2 -2 5 -2 -5 -5 -2 5 -2 z" />
        </>
      );
    case "chart":
      return (
        <>
          <path {...S} d="M22 62 h52" />
          <path {...S} d="M32 62 V44 M46 62 V28 M60 62 V38" />
          <path {...S} d="M28 24 h10" opacity="0.55" />
        </>
      );
    case "inbox":
      return (
        <>
          <path {...S} d="M22 40 h14 l4 8 h12 l4 -8 h14 v22 a4 4 0 0 1 -4 4 H26 a4 4 0 0 1 -4 -4 z" />
          <path {...S} d="M44 14 v22 M36 28 l8 8 8 -8" />
        </>
      );
    case "cloud":
      return (
        <>
          <path {...S} d="M32 54 a12 12 0 0 1 1 -23 a15 15 0 0 1 28 4 a10 10 0 0 1 1 19 z" />
          <path {...S} d="M44 46 v14 M38 54 l6 6 6 -6" opacity="0.55" />
        </>
      );
    case "key":
      return (
        <>
          <circle {...S} cx="32" cy="38" r="11" />
          <path {...S} d="M43 38 h26 M62 38 v9 M69 38 v6" />
        </>
      );
    case "generic":
    default:
      return (
        <>
          <rect {...S} x="24" y="22" width="44" height="34" rx="6" />
          <path {...S} d="M44 31 l2 6 6 2 -6 2 -2 6 -2 -6 -6 -2 6 -2 z" />
        </>
      );
  }
}

/**
 * Line-art for an empty state. The parent `.empty-art` styles it with the
 * muted text colour; both themes get the same drawing.
 */
export function EmptyArt({ variant = "generic", size = 88 }: {
  variant?: EmptyArtVariant;
  size?: number;
}) {
  return (
    <svg
      className="empty-art"
      width={size}
      height={Math.round((size * 72) / 96)}
      viewBox="0 0 96 72"
      aria-hidden="true"
      focusable="false"
    >
      <Scene variant={variant} />
    </svg>
  );
}
