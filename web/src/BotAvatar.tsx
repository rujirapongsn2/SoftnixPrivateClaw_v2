/** Softnix Ele fluffy mascot avatars, one accessory variant per bot.
 *
 * Storage/compat ids stay: buddy, chip, visor, antenna, frame, grille.
 * Each maps to an Ele accessory PNG under /ele/{id}-128.png and -256.png.
 * Chief of Staff defaults to "frame" (blue headphones).
 *
 * Keep in sync with web/src/sbot/BotAvatar.tsx (Sbot primary).
 */

export const BOT_AVATARS = ["buddy", "chip", "visor", "antenna", "frame", "grille"] as const;

export type BotAvatarVariant = (typeof BOT_AVATARS)[number];

/** Softnix brand body colors approximating each Ele variant. */
const BACKGROUNDS: Record<BotAvatarVariant, string> = {
  buddy: "#FDC70C", // yellow headphones
  chip: "#A9CB2E", // lime headphones
  visor: "#2786C2", // blue glasses
  antenna: "#2786C2", // blue cap
  frame: "#2786C2", // blue headphones (Chief / default)
  grille: "#2786C2", // blue hardhat
};

/** Ele accessory label (comments / a11y); ids remain storage keys. */
const ELE_LABEL: Record<BotAvatarVariant, string> = {
  buddy: "yellow headphones",
  chip: "lime headphones",
  visor: "blue glasses",
  antenna: "blue cap",
  frame: "blue headphones",
  grille: "blue hardhat",
};

export function backgroundOf(variant: BotAvatarVariant): string {
  return BACKGROUNDS[variant];
}

/** The face a bot wears when nobody has picked one.
 *
 * Hashed from the id rather than from the name, so renaming a bot does not
 * change its face — the face is how you find it in the sidebar.
 */
export function avatarVariantFor(bot: {
  id?: string;
  kind?: string;
  avatar?: { variant?: string } | null;
}): BotAvatarVariant {
  const chosen = bot.avatar?.variant;
  if (chosen && (BOT_AVATARS as readonly string[]).includes(chosen)) {
    return chosen as BotAvatarVariant;
  }
  // The leader is the one bot a user has exactly one of, and it reads as the
  // team's face — so it is always the same one rather than luck of the hash.
  if (bot.kind === "chief_of_staff") return "frame";
  const id = bot.id || "";
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return BOT_AVATARS[hash % BOT_AVATARS.length];
}

function eleSrc(variant: BotAvatarVariant, size: number): string {
  const ladder = size <= 40 ? 128 : 256;
  return `/ele/${variant}-${ladder}.png`;
}

export function BotAvatar({
  variant,
  size = 28,
  className,
  title,
  working = false,
}: {
  variant: BotAvatarVariant;
  size?: number;
  className?: string;
  title?: string;
  /** Shows a subtle live-work indicator while the bot is processing a turn. */
  working?: boolean;
}) {
  const label = title ?? ELE_LABEL[variant];
  return (
    <span
      className={`sbot-avatar-shell${working ? " sbot-avatar-shell--working" : ""}`}
      style={{ width: size, height: size }}
      title={working ? `${title ?? "Bot"} is working` : undefined}
    >
      <img
        className={className}
        src={eleSrc(variant, size)}
        width={size}
        height={size}
        alt={title ? label : ""}
        role={title ? "img" : undefined}
        aria-hidden={title ? undefined : true}
        draggable={false}
        style={{
          width: size,
          height: size,
          borderRadius: "50%",
          objectFit: "contain",
          background: "transparent",
          display: "block",
        }}
      />
      {working && <span className="sbot-avatar-working-dot" aria-label="Working" />}
    </span>
  );
}
