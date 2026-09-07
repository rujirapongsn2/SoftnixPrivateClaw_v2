/** Illustrated robot faces, one per bot.
 *
 * A bot used to be a coloured square with an emoji in it, and every specialist
 * the Chief of Staff created got the same default 🤖 on the same blue — so a
 * team of six read as six copies of one thing. These are six distinct faces
 * instead, picked from the bot's id so an existing team gets them without a
 * migration and a bot keeps the same face for good.
 *
 * Each variant carries its own full palette rather than being tinted by
 * `avatar.color`: the head, the face plate and the background are chosen
 * together, and recolouring only the background is what makes an illustrated
 * set look like a clip-art grab bag.
 */

import { useId } from "react";

export const BOT_AVATARS = ["buddy", "chip", "visor", "antenna", "frame", "grille"] as const;

export type BotAvatarVariant = (typeof BOT_AVATARS)[number];

/** Below this the accessories stop being shapes and start being noise. */
const SIMPLE_BELOW_PX = 24;

const BACKGROUNDS: Record<BotAvatarVariant, string> = {
  buddy: "#ee7b2f",
  chip: "#35a46b",
  visor: "#f2766b",
  antenna: "#d6453f",
  frame: "#e3a63c",
  grille: "#3e6fd1",
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

type FaceProps = { simple: boolean };

const INK = "#2f3340";

function Buddy({ simple }: FaceProps) {
  if (simple) {
    return (
      <>
        <rect x="12" y="14" width="40" height="36" rx="15" fill="#fff" />
        <circle cx="24" cy="31" r="4" fill={INK} />
        <circle cx="40" cy="31" r="4" fill={INK} />
        <path d="M24 39q8 7 16 0" stroke={INK} strokeWidth="3" strokeLinecap="round" fill="none" />
      </>
    );
  }
  return (
    <>
      <rect x="18" y="50" width="28" height="16" rx="6" fill="#e8eaf2" />
      <rect x="8" y="24" width="9" height="17" rx="4.5" fill="#7c4dff" />
      <rect x="47" y="24" width="9" height="17" rx="4.5" fill="#7c4dff" />
      <rect x="15" y="16" width="34" height="32" rx="13" fill="#fff" />
      <circle cx="25" cy="31" r="3" fill={INK} />
      <circle cx="39" cy="31" r="3" fill={INK} />
      <path d="M25 38q7 6 14 0" stroke={INK} strokeWidth="2.6" strokeLinecap="round" fill="none" />
    </>
  );
}

function Chip({ simple }: FaceProps) {
  if (simple) {
    return (
      <>
        <rect x="11" y="14" width="42" height="36" rx="13" fill="#17414f" />
        <rect x="17" y="22" width="30" height="17" rx="7" fill="#8be0a8" />
        <circle cx="25" cy="30.5" r="2.8" fill="#17414f" />
        <circle cx="32" cy="30.5" r="2.8" fill="#17414f" />
        <circle cx="39" cy="30.5" r="2.8" fill="#17414f" />
      </>
    );
  }
  return (
    <>
      <rect x="22" y="46" width="20" height="20" rx="7" fill="#1e5566" />
      <rect x="8" y="25" width="5" height="12" rx="2.5" fill="#0f2e38" />
      <rect x="51" y="25" width="5" height="12" rx="2.5" fill="#0f2e38" />
      <rect x="13" y="15" width="38" height="32" rx="11" fill="#17414f" />
      <rect x="18" y="21" width="28" height="15" rx="6" fill="#8be0a8" />
      <circle cx="25" cy="28.5" r="2.2" fill="#17414f" />
      <circle cx="32" cy="28.5" r="2.2" fill="#17414f" />
      <circle cx="39" cy="28.5" r="2.2" fill="#17414f" />
    </>
  );
}

function Visor({ simple }: FaceProps) {
  if (simple) {
    return (
      <>
        <rect x="10" y="13" width="44" height="38" rx="18" fill="#fff" />
        <rect x="16" y="26" width="32" height="14" rx="7" fill="#2b2b3c" />
        <rect x="23" y="31.5" width="18" height="3.5" rx="1.75" fill="#7de2d1" />
      </>
    );
  }
  return (
    <>
      <rect x="20" y="48" width="24" height="18" rx="7" fill="#f7c9c3" />
      <path d="M32 14V9" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" />
      <circle cx="32" cy="7" r="3" fill="#fff" />
      <rect x="14" y="13" width="36" height="34" rx="16" fill="#fff" />
      <rect x="18" y="24" width="28" height="12" rx="6" fill="#2b2b3c" />
      <rect x="24" y="28.5" width="16" height="3" rx="1.5" fill="#7de2d1" />
    </>
  );
}

function Antenna({ simple }: FaceProps) {
  if (simple) {
    return (
      <>
        <rect x="12" y="14" width="40" height="36" rx="14" fill="#9bd9a0" />
        <circle cx="24" cy="30" r="4" fill="#2f3e46" />
        <circle cx="40" cy="30" r="4" fill="#2f3e46" />
        <path
          d="M24 38q8 7 16 0"
          stroke="#2f3e46"
          strokeWidth="3"
          strokeLinecap="round"
          fill="none"
        />
      </>
    );
  }
  return (
    <>
      <rect x="19" y="46" width="26" height="20" rx="7" fill="#efe9dc" />
      <path d="M23 19 17 9M41 19l6-10" stroke="#2f3e46" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="16" cy="7" r="3.2" fill="#2f3e46" />
      <circle cx="48" cy="7" r="3.2" fill="#2f3e46" />
      <rect x="16" y="16" width="32" height="30" rx="12" fill="#9bd9a0" />
      <circle cx="25" cy="29" r="2.8" fill="#2f3e46" />
      <circle cx="39" cy="29" r="2.8" fill="#2f3e46" />
      <path
        d="M25 36q7 5.5 14 0"
        stroke="#2f3e46"
        strokeWidth="2.4"
        strokeLinecap="round"
        fill="none"
      />
    </>
  );
}

function Frame({ simple }: FaceProps) {
  if (simple) {
    return (
      <>
        <rect x="11" y="13" width="42" height="38" rx="12" fill="#2a3a42" />
        <rect x="16" y="20" width="32" height="24" rx="9" fill="#7ed957" />
        <circle cx="25" cy="29" r="3.4" fill="#2a3a42" />
        <circle cx="39" cy="29" r="3.4" fill="#2a3a42" />
        <path
          d="M26 36q6 5 12 0"
          stroke="#2a3a42"
          strokeWidth="2.8"
          strokeLinecap="round"
          fill="none"
        />
      </>
    );
  }
  return (
    <>
      <rect x="20" y="48" width="24" height="18" rx="6" fill="#c98f2e" />
      <rect x="8" y="24" width="6" height="13" rx="3" fill="#2a3a42" />
      <rect x="50" y="24" width="6" height="13" rx="3" fill="#2a3a42" />
      <rect x="13" y="14" width="38" height="34" rx="10" fill="#2a3a42" />
      <rect x="18" y="20" width="28" height="20" rx="7" fill="#7ed957" />
      <circle cx="26" cy="27" r="2.8" fill="#2a3a42" />
      <circle cx="38" cy="27" r="2.8" fill="#2a3a42" />
      <path
        d="M26 33.5q6 4.5 12 0"
        stroke="#2a3a42"
        strokeWidth="2.4"
        strokeLinecap="round"
        fill="none"
      />
    </>
  );
}

function Grille({ simple }: FaceProps) {
  if (simple) {
    return (
      <>
        <rect x="12" y="12" width="40" height="40" rx="16" fill="#fff" />
        <rect x="18" y="18" width="28" height="28" rx="12" fill="#2b2b3c" />
        <circle cx="26" cy="29" r="3.2" fill="#fff" />
        <circle cx="38" cy="29" r="3.2" fill="#fff" />
      </>
    );
  }
  return (
    <>
      <rect x="6" y="30" width="8" height="22" rx="4" fill="#fff" />
      <rect x="50" y="30" width="8" height="22" rx="4" fill="#fff" />
      <rect x="16" y="12" width="32" height="40" rx="14" fill="#fff" />
      <rect x="21" y="17" width="22" height="28" rx="10" fill="#2b2b3c" />
      <circle cx="27" cy="27" r="2.6" fill="#fff" />
      <circle cx="37" cy="27" r="2.6" fill="#fff" />
      <rect x="25" y="34" width="14" height="6" rx="3" fill="#fff" />
      <path d="M29.5 34v6M34.5 34v6" stroke="#2b2b3c" strokeWidth="1.6" />
    </>
  );
}

const FACES: Record<BotAvatarVariant, (p: FaceProps) => React.ReactElement> = {
  buddy: Buddy,
  chip: Chip,
  visor: Visor,
  antenna: Antenna,
  frame: Frame,
  grille: Grille,
};

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
  // The heads are drawn past the circle on purpose — a body cropped by the
  // badge is what stops them reading as stickers floating on a dot.
  const clip = useId();
  const Face = FACES[variant];
  return (
    <span
      className={`sbot-avatar-shell${working ? " sbot-avatar-shell--working" : ""}`}
      style={{ width: size, height: size }}
      title={working ? `${title ?? "Bot"} is working` : undefined}
    >
      <svg
        className={className}
        width={size}
        height={size}
        viewBox="0 0 64 64"
        role={title ? "img" : "presentation"}
        aria-label={title}
        aria-hidden={title ? undefined : true}
      >
        <defs>
          <clipPath id={clip}>
            <circle cx="32" cy="32" r="32" />
          </clipPath>
        </defs>
        <g clipPath={`url(#${clip})`}>
          <circle cx="32" cy="32" r="32" fill={BACKGROUNDS[variant]} />
          <Face simple={size < SIMPLE_BELOW_PX} />
        </g>
      </svg>
      {working && <span className="sbot-avatar-working-dot" aria-label="Working" />}
    </span>
  );
}
