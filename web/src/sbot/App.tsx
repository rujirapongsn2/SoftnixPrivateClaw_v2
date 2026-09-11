import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Popover } from "@astryxdesign/core/Popover";
import {
  SideNav,
  SideNavCollapseButton,
  SideNavItem,
  SideNavSection,
  useSideNavCollapse,
} from "@astryxdesign/core/SideNav";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useToast } from "@astryxdesign/core/Toast";
import { AlarmClock, ChevronDown, Loader2, LogOut, Menu, MessageCircle, MessageSquare, MoreVertical, Settings as SettingsIcon, Shield, User as UserIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ADMIN_SECTIONS, AdminPanel, type AdminSection } from "../Admin";
import { BOT_AVATARS, BotAvatar, avatarVariantFor, type BotAvatarVariant } from "./BotAvatar";
import { Chat } from "./Chat";
import { BotGroupEditor, BotGroupHeader, BotGroupNav } from "./BotGroups";
import { BotEditor } from "./BotEditor";
import { ErrorText } from "./ErrorText";
import { Brand, SoftnixLogo, SoftnixMark } from "./Logo";
import { useBranding, useT } from "../branding";
import { PasswordField } from "./PasswordField";
import { botDisplayName } from "./botLabels";
import { SETTINGS_SECTIONS, SettingsPanel, type SettingsSection } from "../Settings";
import { ActiveMission, ApiError, AuthUser, BotGroupInfo, BotInfo, SessionInfo, api, clearToken, getToken, setToken } from "./api";
import { MOBILE_QUERY, useMediaQuery } from "./useMediaQuery";

const PROVIDER_LABELS: Record<string, string> = { google: "Google", microsoft: "Microsoft" };
const PROVIDER_LOGO: Record<string, string> = {
  // Filename intentionally distinct from the original "google.png" — that
  // path was overwritten in place while fixing its transparent background,
  // and browsers cache images by URL, so anyone who'd already loaded the
  // login page kept seeing the old white-background version. A fresh
  // filename guarantees a real fetch regardless of any cache layer.
  google: "/oauth-providers/google-g.png",
  microsoft: "/oauth-providers/microsoft.png",
};

// Per-session "last read" timestamps, persisted so the sidebar's unread dot
// survives a page reload instead of resetting to a fresh (and noisy) guess
// every time. Keyed by session id -> epoch ms of the last time the user had
// it open. A session is unread when its `updated_at` is newer than this.
const LAST_READ_KEY = "claw_last_read";
const LAST_READ_SEEDED_KEY = "claw_last_read_seeded";

function loadLastRead(): Record<string, number> {
  try {
    return JSON.parse(localStorage.getItem(LAST_READ_KEY) ?? "{}");
  } catch {
    return {};
  }
}

function saveLastRead(map: Record<string, number>) {
  try {
    localStorage.setItem(LAST_READ_KEY, JSON.stringify(map));
  } catch {
    // Storage full/unavailable — unread tracking degrades to "no memory",
    // never to a crash.
  }
}

/** Brand lockup that drops the wordmark in the collapsed rail, keeping just the
 * square icon mark — the full wordmark image is too wide for the narrow rail
 * and gets clipped/squeezed there. */
function SidebarBrand() {
  const { isCollapsed } = useSideNavCollapse();
  const [sbotEnabled, setSbotEnabled] = useState<boolean | null>(null);
  useEffect(() => {
    fetch("/api/modes").then(r => r.json()).then(r => {
      const enabled = Boolean(r.sbot);
      setSbotEnabled(enabled);
      if (!enabled) window.location.replace("/chat/privateclaw");
    }).catch(() => setSbotEnabled(false));
  }, []);
  if (isCollapsed) return <div className="claw-sidenav-brand"><SoftnixMark size={22} /></div>;
  const content = (
    <div className="claw-mode-menu">
      <a className="claw-mode-menu-item" href="/chat/privateclaw">
        <span><strong>PrivateClaw</strong><small>Personal AI workspace</small></span>
      </a>
      <a className="claw-mode-menu-item claw-mode-menu-item--active" href="/chat/sbot">
        <span><strong>Bot Mode</strong><small>Work with your bot team</small></span><span aria-hidden="true">✓</span>
      </a>
    </div>
  );
  return <div className="claw-sidenav-brand">
    {sbotEnabled ? <Popover label="Switch mode" placement="below" alignment="start" width={272} hasAutoFocus={false} content={content}>
      <button type="button" className="claw-mode-menu-trigger" aria-label="Switch mode"><Brand height={22} /><Icon icon={ChevronDown} size="sm" /></button>
    </Popover> : <Brand height={22} />}
  </div>;
}

/** Truncate a chat title for the narrow collapsed-rail popover. */
function truncateTitle(title: string, max = 28) {
  return title.length > max ? title.slice(0, max) + "…" : title;
}

const OTHER_INITIAL = 10;
const OTHER_STEP = 25;

/** The single thread a bot converses in, or undefined before its first message.
 *
 * Sessions arrive newest-first, so the first match is the live thread even for
 * older data that predates the one-bot-one-thread rule.
 */
function botThread(sessions: SessionInfo[], botId: string): SessionInfo | undefined {
  return sessions.find((s) => s.bot_id === botId && s.kind !== "group");
}

/** Bots-first navigation: the sidebar lists the team, not the chat history.
 *
 * Each bot row is its one thread, whose history is reached by paging back
 * through the transcript, so listing it again here would be a second,
 * competing way to navigate the same thing. "Other" holds every session no bot
 * row stands for — sessions sbot opened for itself (a scheduled run, an inbound
 * Telegram message), extra per-bot threads left by older data, and sessions
 * whose bot has since been deleted — so nothing is stranded unreachable and
 * undeletable.
 *
 * Expanded: stacked SideNavSections. Collapsed: a flyout with the same data.
 */
function TeamNav({
  bots,
  sessions,
  active,
  selectedBotId,
  done,
  working,
  onSelectBot,
  onSelectSession,
  onDeleteSession,
  onBotUpdated,
  onEditBot,
  onBotDeleted,
}: {
  bots: BotInfo[];
  sessions: SessionInfo[];
  active: string | null;
  selectedBotId: string | null;
  done: Set<string>;
  working: Set<string>;
  onSelectBot: (bot: BotInfo) => void;
  onSelectSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
  onBotUpdated: () => void;
  onEditBot: (bot: BotInfo) => void;
  onBotDeleted: (bot: BotInfo) => void;
}) {
  const { isCollapsed } = useSideNavCollapse();
  const t = useT();
  const [isOpen, setIsOpen] = useState(false);
  const [visible, setVisible] = useState(OTHER_INITIAL);
  // The bot whose face is being picked, if any.
  const [picking, setPicking] = useState<string | null>(null);

  const pickAvatar = async (bot: BotInfo, variant: BotAvatarVariant) => {
    setPicking(null);
    // Spread the existing avatar rather than replacing it: `color` and `emoji`
    // are what the bot falls back to wherever a variant is not understood.
    await api.updateBot(bot.id, { avatar: { ...(bot.avatar || {}), variant } });
    onBotUpdated();
  };

  const shownBots = bots;

  // Everything the bot rows don't already stand for. Excluded by id rather than
  // by `!s.bot_id` so an extra thread on a bot still shows up somewhere. The
  // The full `bots` list drives the exclusion so a bot thread never appears
  // again below its own team row.
  const others = useMemo(() => {
    const threads = new Set(bots.map((b) => botThread(sessions, b.id)?.id).filter(Boolean));
    return sessions.filter((s) => !threads.has(s.id));
  }, [bots, sessions]);
  const shownOthers = others.slice(0, visible);
  const hasMore = others.length > shownOthers.length;

  // Running spinner (turn processing) or a "new response" dot (finished while
  // you were elsewhere). The dot comes from the bot's own thread only: any
  // extra thread has its own row under "Other", and reading it there is what
  // clears its dot.
  const runningIcon = (
    <span className="claw-recent-status" title="Processing…">
      <Icon icon={Loader2} size="xsm" />
    </span>
  );
  const doneIcon = <span className="claw-recent-status claw-recent-status--done" title="New response" />;
  const botStatus = (id: string) => {
    // Ahead of the thread lookup, because a delegated specialist works inside
    // the leader's turn: its own thread is idle, and a bot that has never been
    // chatted with directly has no thread to look up at all.
    if (working.has(id)) return runningIcon;
    const thread = botThread(sessions, id);
    if (!thread) return null;
    if (thread.running) return runningIcon;
    if (done.has(thread.id)) return doneIcon;
    return null;
  };
  const botIsWorking = (id: string) => working.has(id) || Boolean(botThread(sessions, id)?.running);
  const sessionStatus = (s: SessionInfo) =>
    s.running ? runningIcon : done.has(s.id) ? doneIcon : null;

  const showMore = hasMore && (
    <button type="button" className="claw-recents-more" onClick={() => setVisible((v) => v + OTHER_STEP)}>
      <Icon icon={ChevronDown} size="sm" />
      Show older
    </button>
  );

  if (!isCollapsed) {
    return (
      <>
        {shownBots.length === 0 && shownOthers.length === 0 ? (
          <div className="claw-recents-empty">
            <Text size="sm" color="secondary">
              No bots yet.
            </Text>
          </div>
        ) : null}
        {shownBots.length > 0 && (
          <SideNavSection title="Team Bots">
            {shownBots.map((b) => {
              const displayName = botDisplayName(b, t);
              return (
                <div
                  key={b.id}
                  className={`sbot-bot-row${b.id === selectedBotId ? " sbot-bot-row--selected" : ""}`}
                  onClick={() => onSelectBot(b)}
                >
                  <div className="sbot-bot-info">
                    <button
                      type="button"
                      className="sbot-avatar-pick"
                      aria-label={t("nav.changeAvatar", { name: displayName })}
                      onClick={(e) => {
                        // The row opens the bot's chat. Clicking the picture is
                        // the one place that should mean "change the picture".
                        e.stopPropagation();
                        setPicking(picking === b.id ? null : b.id);
                      }}
                    >
                      <BotAvatar variant={avatarVariantFor(b)} size={28} working={botIsWorking(b.id)} />
                    </button>
                    {picking === b.id && (
                      <>
                        <div
                          className="sbot-avatar-scrim"
                          onClick={(e) => {
                            e.stopPropagation();
                            setPicking(null);
                          }}
                        />
                        <div className="sbot-avatar-menu" onClick={(e) => e.stopPropagation()}>
                          {BOT_AVATARS.map((v) => (
                            <button
                              key={v}
                              type="button"
                              className={`sbot-avatar-opt${
                                avatarVariantFor(b) === v ? " sbot-avatar-opt--on" : ""
                              }`}
                              onClick={() => void pickAvatar(b, v)}
                            >
                              <BotAvatar variant={v} size={34} />
                            </button>
                          ))}
                        </div>
                      </>
                    )}
                    <div className="sbot-bot-meta">
                      <span className="sbot-bot-name">{displayName}</span>
                      <span className="sbot-bot-role">{b.role_title}</span>
                    </div>
                  </div>
                  {botStatus(b.id)}
                  {b.kind === "chief_of_staff" && <span className="sbot-badge-cos">Leader</span>}
                  <span className="sbot-bot-menu">
                    <IconButton
                      label={`Manage ${displayName}`}
                      icon={<Icon icon={MoreVertical} size="sm" />}
                      variant="ghost"
                      size="sm"
                      clickAction={(e) => {
                        e.stopPropagation();
                        onEditBot(b);
                      }}
                    />
                  </span>
                </div>
              );
            })}
          </SideNavSection>
        )}
        {shownOthers.length > 0 && (
          <SideNavSection title="Other">
            {shownOthers.map((s) => (
              <div
                key={s.id}
                className={`claw-recent-row${done.has(s.id) ? " claw-recent-row--unread" : ""}`}
              >
                <SideNavItem
                  label={s.title}
                  icon={s.channel === "schedule" ? AlarmClock : MessageSquare}
                  isSelected={s.id === active}
                  onClick={() => onSelectSession(s.id)}
                />
                {sessionStatus(s)}
                <span className="claw-recent-delete">
                  <IconButton
                    label="Delete chat"
                    icon={<Icon icon="close" size="xsm" />}
                    variant="ghost"
                    size="sm"
                    clickAction={(e) => {
                      e.stopPropagation();
                      onDeleteSession(s.id);
                    }}
                  />
                </span>
              </div>
            ))}
          </SideNavSection>
        )}
        {showMore}
      </>
    );
  }

  return (
    <div className="claw-recents-collapsed">
      <Popover
        label="Team"
        placement="end"
        alignment="start"
        isOpen={isOpen}
        onOpenChange={setIsOpen}
        width={280}
        // This flyout is opened by a mouse click far more often than by
        // keyboard, so don't steal focus onto the first row automatically —
        // on macOS Safari with "Full Keyboard Access" on, that paints a
        // heavy native focus ring around whichever row gets auto-focused
        // and (via :focus-within) permanently reveals its delete button,
        // making the very first item look stuck in a bogus selected state.
        hasAutoFocus={false}
        content={
          <div className="claw-recents-popover">
            {shownBots.length === 0 && shownOthers.length === 0 && (
              <Text size="sm" color="secondary">
                No bots yet.
              </Text>
            )}
            {shownBots.length > 0 && (
              <div className="claw-recents-popover-group">
                <Text size="sm" weight="semibold" color="secondary" className="claw-recents-popover-title">
                  Team Bots
                </Text>
                {shownBots.map((b) => {
                  const displayName = botDisplayName(b, t);
                  return <div key={b.id} className="claw-recents-popover-row">
                    <Button
                      label={truncateTitle(displayName)}
                      icon={
                        <BotAvatar variant={avatarVariantFor(b)} size={18} working={botIsWorking(b.id)} />
                      }
                      variant={b.id === selectedBotId ? "secondary" : "ghost"}
                      size="sm"
                      className="claw-recents-popover-item"
                      clickAction={() => {
                        onSelectBot(b);
                        setIsOpen(false);
                      }}
                    />
                    {botStatus(b.id)}
                    <IconButton
                      label={`Manage ${displayName}`}
                      icon={<Icon icon={MoreVertical} size="sm" />}
                      variant="ghost"
                      size="sm"
                      clickAction={() => {
                        onEditBot(b);
                        setIsOpen(false);
                      }}
                    />
                  </div>;
                })}
              </div>
            )}
            {shownOthers.length > 0 && (
              <div className="claw-recents-popover-group">
                <Text size="sm" weight="semibold" color="secondary" className="claw-recents-popover-title">
                  Other
                </Text>
                {shownOthers.map((s) => (
                  <div
                    key={s.id}
                    className={`claw-recents-popover-row${done.has(s.id) ? " claw-recents-popover-row--unread" : ""}`}
                  >
                    <Button
                      label={truncateTitle(s.title)}
                      icon={<Icon icon={s.channel === "schedule" ? AlarmClock : MessageSquare} size="sm" />}
                      variant={s.id === active ? "secondary" : "ghost"}
                      size="sm"
                      className="claw-recents-popover-item"
                      clickAction={() => {
                        onSelectSession(s.id);
                        setIsOpen(false);
                      }}
                    />
                    <IconButton
                      label="Delete chat"
                      icon={<Icon icon="close" size="xsm" />}
                      variant="ghost"
                      size="sm"
                      clickAction={() => onDeleteSession(s.id)}
                    />
                  </div>
                ))}
              </div>
            )}
            {showMore}
          </div>
        }
      >
        <IconButton label="Team" icon={<Icon icon={MessageCircle} size="sm" />} variant="ghost" />
      </Popover>
    </div>
  );
}

function Auth({
  onDone,
  initialError,
  activationToken,
  resetToken,
}: {
  onDone: (user: AuthUser) => void;
  initialError?: string;
  activationToken?: string;
  resetToken?: string;
}) {
  const t = useT();
  const [mode, setMode] = useState<"login" | "register" | "complete-setup" | "forgot-password" | "reset-password">(
    activationToken ? "complete-setup" : resetToken ? "reset-password" : "login",
  );
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(initialError ?? "");
  const [busy, setBusy] = useState(false);
  const [providers, setProviders] = useState<string[]>([]);
  // Only used to display which account an activation link belongs to — the
  // request itself is authenticated by the token, not this email.
  const [activationEmail, setActivationEmail] = useState("");
  // Whether the "forgot password" form has been submitted — flips the view
  // to a static confirmation instead of the email form.
  const [forgotSent, setForgotSent] = useState(false);

  useEffect(() => {
    api.providers().then((r) => setProviders(r.providers)).catch(() => setProviders([]));
  }, []);

  // Decode the emailed activation link so the form can show which account
  // it's for and prefill the display name — same info the old (removed)
  // registration_incomplete signal used to carry, now sourced from a token
  // that proves the visitor actually received this link by email.
  useEffect(() => {
    if (!activationToken) return;
    api
      .activationInfo(activationToken)
      .then((info) => {
        setActivationEmail(info.email);
        setDisplayName(info.display_name);
      })
      .catch(() => setError("This activation link is invalid or has expired."));
  }, [activationToken]);

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      if (mode === "forgot-password") {
        // api.forgotPassword always resolves the same way whether or not the
        // email matches an account with a password — the backend never
        // reveals that distinction, so neither does this branch.
        await api.forgotPassword(email);
        setForgotSent(true);
        return;
      }
      const res =
        mode === "complete-setup"
          ? await api.completeRegistration(activationToken ?? "", password, displayName.trim())
          : mode === "reset-password"
            ? await api.resetPassword(resetToken ?? "", password)
            : mode === "login"
              ? await api.login(email, password)
              : await api.register(email, password, displayName.trim());
      setToken(res.access_token);
      onDone(res.user);
    } catch (e) {
      setError(
        mode === "login"
          ? // Shown identically for every login failure — wrong password, no
            // such account, or a pending-imported account — so the message
            // itself never reveals which case applies (that distinction is
            // exactly the account-enumeration oracle closed on the backend).
            // Deliberately does NOT mention "activation link": that reads as
            // confusing/wrong to the much more common case of an already-
            // activated user who simply forgot their password. The "Forgot
            // password?" link below covers BOTH cases (see forgot_password()
            // in claw/api/auth.py) without needing a hint in this message.
            "Invalid email or password."
          : mode === "complete-setup"
            ? "Couldn't activate this account. The link may have expired — ask an administrator to resend it."
            : mode === "reset-password"
              ? // A 403 here means the token WAS valid (the password may
                // already have changed) but the account is suspended — a
                // distinct, accurate message, not the generic expired-link
                // one that would otherwise tell the user to just retry.
                e instanceof ApiError && e.status === 403
                ? "This account has been suspended. Contact your administrator."
                : "This reset link is invalid or has expired. Request a new one from the login page."
              : mode === "forgot-password"
                ? "Something went wrong. Please try again."
                : String(e).replace(/^Error:\s*/, ""),
      );
    } finally {
      setBusy(false);
    }
  };

  if (mode === "forgot-password" && forgotSent) {
    return (
      <div className="claw-login">
        <SoftnixLogo height={44} slot="login" />
        <Text type="display-3">Sbot</Text>
        <Text color="secondary">{t("auth.tagline")}</Text>
        <Text size="sm" color="secondary">
          If an account exists for {email || "that email"}, we've sent instructions to access it. Check your
          inbox (and spam folder).
        </Text>
        <Button
          label="Back to login"
          variant="ghost"
          clickAction={() => {
            setMode("login");
            setForgotSent(false);
            setError("");
          }}
        />
      </div>
    );
  }

  return (
    <div className="claw-login">
      <SoftnixLogo height={44} slot="login" />
      <Text type="display-3">Sbot</Text>
      <Text color="secondary">{t("auth.tagline")}</Text>
      {mode === "complete-setup" && (
        <Text size="sm" color="secondary">
          {activationEmail
            ? `An account for ${activationEmail} is waiting for you — set a password to finish activating it.`
            : "Checking your activation link…"}
        </Text>
      )}
      {mode === "reset-password" && (
        <Text size="sm" color="secondary">
          Choose a new password for your account.
        </Text>
      )}
      {(mode === "register" || mode === "complete-setup") && (
        <TextInput label="Full name" placeholder="Jane Doe" value={displayName} onChange={setDisplayName} />
      )}
      {(mode === "login" || mode === "register" || mode === "forgot-password") && (
        <TextInput label="Email" type="email" placeholder="jane@company.com" value={email} onChange={setEmail} />
      )}
      {mode !== "forgot-password" &&
        (mode === "register" || mode === "complete-setup" || mode === "reset-password" ? (
          <PasswordField
            label="Password"
            description="At least 8 characters."
            value={password}
            onChange={setPassword}
          />
        ) : (
          <TextInput label="Password" type="password" value={password} onChange={setPassword} />
        ))}
      {error && <ErrorText>{error}</ErrorText>}
      <Button
        label={
          busy
            ? "…"
            : mode === "login"
              ? "Log in"
              : mode === "register"
                ? "Create account"
                : mode === "complete-setup"
                  ? "Activate account"
                  : mode === "forgot-password"
                    ? "Send recovery email"
                    : "Reset password"
        }
        variant="primary"
        isDisabled={
          busy ||
          (mode === "forgot-password"
            ? !email
            : password.length < 8 || (mode === "complete-setup" ? !activationEmail : mode === "reset-password" ? false : !email))
        }
        clickAction={submit}
      />
      {mode === "login" && (
        <Button
          label="Forgot password?"
          variant="ghost"
          size="sm"
          clickAction={() => {
            setMode("forgot-password");
            setPassword("");
            setError("");
          }}
        />
      )}
      {mode === "login" || mode === "register" ? (
        <Button
          label={mode === "login" ? "Need an account? Register" : "Have an account? Log in"}
          variant="ghost"
          clickAction={() => {
            setMode(mode === "login" ? "register" : "login");
            setError("");
          }}
        />
      ) : (
        <Button
          label="Back to login"
          variant="ghost"
          clickAction={() => {
            setMode("login");
            setPassword("");
            setError("");
          }}
        />
      )}
      {(mode === "login" || mode === "register") && providers.length > 0 && (
        <>
          <Text size="sm" color="secondary">or continue with</Text>
          {providers.map((p) => (
            <Button
              key={p}
              label={PROVIDER_LABELS[p] ?? p}
              icon={
                PROVIDER_LOGO[p] ? (
                  <img src={PROVIDER_LOGO[p]} alt="" aria-hidden="true" className="claw-oauth-logo" />
                ) : undefined
              }
              variant="secondary"
              clickAction={() => {
                window.location.href = `/api/auth/oidc/${p}/login`;
              }}
            />
          ))}
        </>
      )}
    </div>
  );
}

export default function App() {
  const t = useT();
  const { setUserOverride } = useBranding();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [bots, setBots] = useState<BotInfo[]>([]);
  const [botEditor, setBotEditor] = useState<BotInfo | null>(null);
  const [botGroups, setBotGroups] = useState<BotGroupInfo[]>([]);
  const [groupEditor, setGroupEditor] = useState<BotGroupInfo | null | undefined>(undefined);
  const [selectedBotId, setSelectedBotId] = useState<string | null>(null);
  // A bot row can arrive before the session list on first load. Keep that
  // short reconciliation state distinct from a real draft so users never see
  // the chat landing while an existing transcript is being located.
  const [openingBotId, setOpeningBotId] = useState<string | null>(null);
  const botSelectionRequestRef = useRef(0);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [active, setActive] = useState<string | null>(() => {
    const parts = window.location.pathname.split("/");
    return parts[3] || sessionStorage.getItem("claw:last:sbot");
  });
  useEffect(() => {
    if (active) sessionStorage.setItem("claw:last:sbot", active);
    else sessionStorage.removeItem("claw:last:sbot");
    if (window.location.pathname.startsWith("/chat"))
      window.history.replaceState(null, "", "/chat/sbot" + (active ? "/" + encodeURIComponent(active) : ""));
  }, [active]);
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(null);
  const [adminSection, setAdminSection] = useState<AdminSection | null>(null);
  const [authError, setAuthError] = useState("");
  const [activationToken, setActivationToken] = useState("");
  const [resetToken, setResetToken] = useState("");
  // Responsive shell. Below the tablet width the sidebar becomes an off-canvas
  // drawer (navOpen); on desktop `collapsed` drives the rail.
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const [navOpen, setNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  // Per-session "last read" timestamps (id -> epoch ms), persisted to
  // localStorage — see LAST_READ_KEY above for why this replaced an
  // in-memory-only heuristic.
  const [lastRead, setLastRead] = useState<Record<string, number>>(loadLastRead);
  // Teammates the open transcript reports as mid-delegation. Sorted by <Chat>,
  // and replaced only on a real change so passing it back down cannot bounce
  // between renders.
  const [workingBots, setWorkingBots] = useState<string[]>([]);
  // Missions still in flight, refreshed by `refresh` below. The other half of
  // "who is busy": `workingBots` only knows what the open transcript reports.
  const [activeMissions, setActiveMissions] = useState<ActiveMission[]>([]);
  const activeRef = useRef<string | null>(null);
  const toast = useToast();

  const onWorkingBotsChange = useCallback((ids: string[]) => {
    setWorkingBots((prev) =>
      prev.length === ids.length && prev.every((id, i) => id === ids[i]) ? prev : ids,
    );
  }, []);
  // Union of both sources. A bot can be busy for either reason and the sidebar
  // draws one spinner, so neither source may mask the other: a delegation is
  // only visible while its transcript is open, and mission work is only visible
  // here.
  const workingBotIds = useMemo(() => {
    const ids = new Set(workingBots);
    for (const m of activeMissions) {
      for (const step of m.running) if (step.bot_id) ids.add(step.bot_id);
    }
    return ids;
  }, [workingBots, activeMissions]);
  // Progress belongs in the thread the mission was planned in, not in whichever
  // one happens to be open — the sidebar spinner is what tells the user
  // elsewhere that something is running.
  const sessionMissions = useMemo(
    () => (active ? activeMissions.filter((m) => m.session_id === active) : []),
    [activeMissions, active],
  );

  const markRead = useCallback((id: string, when: number) => {
    setLastRead((prev) => {
      if ((prev[id] ?? 0) >= when) return prev; // never move a timestamp backwards
      const next = { ...prev, [id]: when };
      saveLastRead(next);
      return next;
    });
  }, []);

  // Capture a JWT (or error) returned by the OIDC callback, then restore session.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlToken = params.get("token");
    const urlError = params.get("auth_error");
    // Imported-user activation link from an emailed "set your password" link.
    // Uses a URL FRAGMENT (#activate=...), not a query string — fragments
    // are never sent to the server, so this login-granting token never
    // appears in any reverse-proxy/CDN/server access log.
    const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const activate = hashParams.get("activate");
    if (activate) {
      setActivationToken(activate);
      window.history.replaceState({}, "", window.location.pathname);
    }
    // "Forgot password" reset link — same fragment-not-query-string
    // reasoning as the activation link above.
    const resetPassword = hashParams.get("reset-password");
    if (resetPassword) {
      setResetToken(resetPassword);
      window.history.replaceState({}, "", window.location.pathname);
    }
    // Connector OAuth callback lands here with ?connector=<key>&connector_status=…
    const connector = params.get("connector");
    const connectorStatus = params.get("connector_status");
    if (connectorStatus) {
      toast(
        connectorStatus === "connected"
          ? { body: `${connector || "Connector"} connected`, type: "info", autoHideDuration: 3000 }
          : connectorStatus === "name_conflict"
            ? {
                body: `You already have your own connector named "${connector || ""}". Rename it first, then connect.`,
                type: "error",
              }
            : { body: "Couldn't connect. Please try again.", type: "error" },
      );
      window.history.replaceState({}, "", window.location.pathname);
    }
    if (urlToken || urlError) {
      if (urlToken) setToken(urlToken);
      if (urlError) setAuthError("Social sign-in failed. Please try again.");
      window.history.replaceState({}, "", window.location.pathname);
    }
    if (!getToken()) {
      setChecking(false);
      return;
    }
    api
      .me()
      .then(setUser)
      .catch(() => clearToken())
      .finally(() => setChecking(false));
  }, []);

  const refresh = useCallback(
    () => {
      api.listSessions().then(setSessions).catch(() => undefined);
      api.listBotGroups().then(setBotGroups).catch(() => undefined);
      api.listBots().then((b) => {
        setBots(b);
        if (!selectedBotId && !activeRef.current && b.length > 0) {
          const cos = b.find((x) => x.kind === "chief_of_staff") || b[0];
          setSelectedBotId(cos.id);
        }
      }).catch(() => undefined);
      // Rides the same tick as the session poll rather than adding a cadence:
      // a mission runs detached from any turn, so nothing on the socket says a
      // specialist is busy inside one.
      api.activeMissions().then(setActiveMissions).catch(() => undefined);
    },
    [selectedBotId],
  );

  useEffect(() => {
    if (user) void refresh();
  }, [user, refresh]);

  // Personal appearance override (Settings > Profile > Preferences) follows
  // whichever user is currently signed in — cleared on logout so the next
  // login (possibly a different account) doesn't inherit a stale override.
  useEffect(() => {
    setUserOverride(
      user
        ? {
            language: user.language,
            font_size: user.font_size,
            chat_background: user.chat_background,
            execution_panel_enabled: user.execution_panel_enabled,
          }
        : null,
    );
  }, [user, setUserOverride]);

  // One-time migration: the very first time a real session list loads on
  // this browser, treat everything already there as "read" — otherwise
  // switching to persisted read-tracking would instantly mark the user's
  // entire existing history as unread (the opposite of what "already read"
  // should feel like). Real, unread-worthy activity is tracked from here on.
  useEffect(() => {
    if (sessions.length === 0) return;
    if (localStorage.getItem(LAST_READ_SEEDED_KEY) === "1") return;
    const now = Date.now();
    setLastRead((prev) => {
      const next = { ...prev };
      for (const s of sessions) if (!(s.id in next)) next[s.id] = now;
      saveLastRead(next);
      return next;
    });
    localStorage.setItem(LAST_READ_SEEDED_KEY, "1");
  }, [sessions]);

  // Keep `activeRef` in sync, and mark the active session's read watermark up
  // to its latest known `updated_at` — both the instant it becomes active and
  // again on every poll tick while it stays open. Anchoring to the session's
  // own server timestamp (not client wall-clock) matters: a reply that
  // finishes while the user is watching must be captured as "seen" with no
  // race against polling cadence. A brand-new draft session isn't in
  // `sessions` yet (it was created via a separate API call, not the next
  // poll) — skip marking until a poll resolves it rather than guessing with
  // `Date.now()`, which could stamp a time later than the server's own
  // `updated_at` and then get stuck (markRead never moves backwards),
  // masking a reply that actually arrived after the user left.
  useEffect(() => {
    activeRef.current = active;
    if (!active) return;
    const current = sessions.find((s) => s.id === active);
    if (current) markRead(active, new Date(current.updated_at).getTime());
  }, [active, sessions, markRead]);

  // Poll the session list so the sidebar reflects background turns (running →
  // done) even when the user has navigated away from the processing chat.
  useEffect(() => {
    if (!user) return;
    const id = setInterval(() => void refresh(), 3000);
    return () => clearInterval(id);
  }, [user, refresh]);

  // A session is unread when it has changed more recently than the last time
  // the user had it open — persisted, so this survives reloads and doesn't
  // depend on having observed every intermediate "running" transition live.
  const doneSessions = useMemo(
    () =>
      new Set(
        sessions
          .filter((s) => s.id !== active && new Date(s.updated_at).getTime() > (lastRead[s.id] ?? 0))
          .map((s) => s.id),
      ),
    [sessions, active, lastRead],
  );

  // The draft landing exists only for a bot that hasn't been messaged yet, so
  // whenever a bot with a thread is selected and nothing is open — on first
  // load, where the bot is picked before its sessions arrive — show that
  // thread rather than an empty composer.
  useEffect(() => {
    if (active !== null || !selectedBotId) return;
    const thread = botThread(sessions, selectedBotId);
    if (thread) {
      setActive(thread.id);
      setOpeningBotId(null);
    }
  }, [active, selectedBotId, sessions]);

  // One bot, one thread: a bot's chat is a single continuous conversation, so
  // sending from the draft landing reopens the bot's existing thread instead of
  // starting a second one. Only a bot-less session (schedule, Telegram) or no
  // selection at all leads to a fresh session, and the bot_id null-check keeps
  // this from ever adopting one of those. Returns the id for immediate use.
  const requireSession = useCallback(async () => {
    const existing = selectedBotId ? botThread(sessions, selectedBotId) : undefined;
    if (existing) {
      setActive(existing.id);
      setSettingsSection(null);
      setAdminSection(null);
      return { id: existing.id, isNew: false };
    }
    if (selectedBotId) {
      // `sessions` is still empty for the first moments after login (refresh
      // fires listSessions alongside listBots, and the bot list renders as soon
      // as it lands), so an early send would read "no thread yet" and give the
      // bot a second one — demoting its real history into "Other". Confirm
      // against the server before creating anything.
      const known = await api.listSessions().catch(() => null);
      if (known) {
        setSessions(known);
        const found = botThread(known, selectedBotId);
        if (found) {
          setActive(found.id);
          setSettingsSection(null);
          setAdminSection(null);
          return { id: found.id, isNew: false };
        }
      }
    }
    const created = await api.createSession("New chat", selectedBotId, "direct");
    await refresh();
    setActive(created.id);
    setSettingsSection(null);
    setAdminSection(null);
    return { id: created.id, isNew: true };
  }, [refresh, selectedBotId, sessions]);

  const autoTitle = useCallback(
    (sessionId: string) => (text: string) => {
      const title = text.length > 42 ? text.slice(0, 42) + "…" : text;
      void api.renameSession(sessionId, title).then(refresh);
    },
    [refresh],
  );

  const logout = () => {
    // Audit the logout while the token is still valid — clearing it first
    // would make this call 401. A failure here (e.g. offline) shouldn't block
    // signing out locally, so it's fire-and-forget.
    void api.logout().catch(() => undefined);
    // Ignore an in-flight bot-thread lookup after leaving the workspace.
    botSelectionRequestRef.current += 1;
    clearToken();
    sessionStorage.removeItem("claw:last:privateclaw");
    sessionStorage.removeItem("claw:last:sbot");
    setUser(null);
    setSessions([]);
    setBotGroups([]);
    setBotEditor(null);
    setGroupEditor(undefined);
    setSelectedBotId(null);
    setOpeningBotId(null);
    setActiveMissions([]);
    setActive(null);
    setSettingsSection(null);
    setAdminSection(null);
  };

  const activeGroup = botGroups.find(g => g.session_id === active);
  const openGroup = (group: BotGroupInfo) => {
    botSelectionRequestRef.current += 1;
    setOpeningBotId(null);
    setSelectedBotId(null);
    setActive(group.session_id);
    setSettingsSection(null);
    setAdminSection(null);
    setNavOpen(false);
  };

  const openBot = (bot: BotInfo) => {
    const request = ++botSelectionRequestRef.current;
    const thread = botThread(sessions, bot.id);
    setSelectedBotId(bot.id);
    setSettingsSection(null);
    setAdminSection(null);
    setNavOpen(false);
    if (thread) {
      setOpeningBotId(null);
      setActive(thread.id);
      return;
    }

    // Do not show the draft landing until the server confirms this bot has no
    // existing thread. This handles a click during the initial parallel bot /
    // session requests, and avoids a visible landing-to-transcript jump.
    setOpeningBotId(bot.id);
    setActive(null);
    void api.listSessions().then(async (known) => {
      if (botSelectionRequestRef.current !== request) return;
      setSessions(known);
      const existing = botThread(known, bot.id);
      if (existing) {
        setActive(existing.id);
        return;
      }
      if (bot.kind === "chief_of_staff") {
        const created = await api.createSession("Team Lead", bot.id, "direct");
        if (botSelectionRequestRef.current !== request) return;
        setActive(created.id);
        void refresh();
        return;
      }
      setActive(null);
    }).catch(() => {
      if (botSelectionRequestRef.current === request) setActive(null);
    }).finally(() => {
      if (botSelectionRequestRef.current === request) setOpeningBotId(null);
    });
  };

  if (checking) return <div className="claw-login"><Text color="secondary">Loading…</Text></div>;
  if (!user)
    return (
      <Auth
        onDone={setUser}
        initialError={authError}
        activationToken={activationToken || undefined}
        resetToken={resetToken || undefined}
      />
    );

  // Selecting anything in the drawer should close it on mobile.
  const closeDrawer = () => setNavOpen(false);
  const showAdmin = user.is_admin;
  const settingsSectionMeta = settingsSection
    ? SETTINGS_SECTIONS.find((s) => s.key === settingsSection)
    : undefined;
  const adminSectionMeta = adminSection ? ADMIN_SECTIONS.find((s) => s.key === adminSection) : undefined;
  const currentTitle = adminSectionMeta
    ? t(adminSectionMeta.labelKey)
    : settingsSectionMeta
      ? t(settingsSectionMeta.labelKey)
      : t("nav.chat");

  return (
    <div className={`claw-app${isMobile && navOpen ? " claw-app--nav-open" : ""}`}>
      {isMobile && navOpen && (
        <div className="claw-nav-backdrop" onClick={closeDrawer} aria-hidden="true" />
      )}
      <SideNav
        className="claw-sidenav"
        // Force full-width (never rail) while in drawer mode; use the rail
        // toggle only on desktop.
        collapsible={{
          isCollapsed: isMobile ? false : collapsed,
          onCollapsedChange: setCollapsed,
          hasButton: false,
        }}
        header={<SidebarBrand />}
        footer={
          <div className="claw-sidenav-footer">
            <SideNavItem label={user.display_name || user.email} icon={UserIcon}>
              <SideNavItem
                label={t("nav.settings")}
                icon={SettingsIcon}
                collapsible={{ defaultIsCollapsed: true }}
              >
                {SETTINGS_SECTIONS.map((s) => (
                  <SideNavItem
                    key={s.key}
                    label={t(s.labelKey)}
                    icon={s.icon}
                    isSelected={settingsSection === s.key}
                    onClick={() => {
                      setSettingsSection(s.key);
                      setAdminSection(null);
                      setActive(null);
                      closeDrawer();
                    }}
                  />
                ))}
              </SideNavItem>
              {showAdmin && (
                <div className="claw-nav-admin">
                  <SideNavItem
                    label={t("nav.controlPlane")}
                    icon={Shield}
                    isSelected={adminSection !== null}
                    onClick={() => {
                      setAdminSection("overview");
                      setSettingsSection(null);
                      setActive(null);
                      closeDrawer();
                    }}
                  />
                </div>
              )}
              <SideNavItem label={t("nav.logout")} icon={LogOut} onClick={logout} />
            </SideNavItem>
          </div>
        }
        footerIcons={isMobile ? undefined : <SideNavCollapseButton />}
      >
        <BotGroupNav groups={botGroups} sessions={sessions} active={active} done={doneSessions}
          onSelect={openGroup} onCreate={() => { setGroupEditor(null); setNavOpen(false); }} />
        <TeamNav
          bots={bots}
          sessions={sessions.filter(s => !botGroups.some(g => g.session_id === s.id))}
          active={active}
          selectedBotId={selectedBotId}
          done={doneSessions}
          working={workingBotIds}
          onSelectBot={(b) => {
            openBot(b);
          }}
          onSelectSession={(id) => {
            setActive(id);
            setSettingsSection(null);
            setAdminSection(null);
            closeDrawer();
          }}
          onDeleteSession={(id) => {
            void api.deleteSession(id).then(() => {
              // Dropped locally before refetching: the auto-open effect reopens
              // a bot's thread whenever nothing is active, and from the stale
              // list it would reopen this one in the gap before refresh lands —
              // leaving `active` pointing at a session the server no longer has.
              setSessions((prev) => prev.filter((s) => s.id !== id));
              if (active === id) setActive(null);
              void refresh();
            });
          }}
          onBotUpdated={() => void refresh()}
          onEditBot={(bot) => setBotEditor(bot)}
          onBotDeleted={(bot) => {
            setBots(previous => previous.filter(item => item.id !== bot.id));
            if (selectedBotId === bot.id) {
              setSelectedBotId(null);
              setActive(null);
            }
            void refresh();
          }}
        />
      </SideNav>

      <main className="claw-main">
        {/* Mobile top bar: only shown ≤1024px (CSS), gives a way to open the
            drawer since the sidebar is off-canvas there. */}
        <div className="claw-topbar">
          <IconButton
            label="Open menu"
            icon={<Icon icon={Menu} size="sm" />}
            variant="ghost"
            clickAction={() => setNavOpen(true)}
          />
          <Text weight="semibold">{currentTitle}</Text>
          <SoftnixMark size={20} />
        </div>
        {adminSection ? (
          <AdminPanel
            section={adminSection}
            selfId={user.id}
            onSectionChange={(section) => setAdminSection(section)}
          />
        ) : settingsSection ? (
          <SettingsPanel section={settingsSection} />
        ) : openingBotId ? (
          <div className="sbot-transcript-opening" role="status" aria-live="polite">
            <Icon icon={Loader2} size="sm" className="sbot-bot-spin" />
            <Text color="secondary">{t("chat.loadingTranscript")}</Text>
          </div>
        ) : (
          <>
          {activeGroup && (
            <BotGroupHeader
              group={activeGroup}
              bots={bots}
              working={workingBotIds}
              groupRunning={Boolean(active && sessions.find((s) => s.id === active)?.running)}
              onEdit={() => setGroupEditor(activeGroup)}
            />
          )}
          <Chat
            sessionId={active}
            groupName={activeGroup?.name}
            bot={bots.find((b) => b.id === (activeGroup?.leader_id || sessions.find((s) => s.id === active)?.bot_id || selectedBotId)) || bots[0] || null}
            bots={bots}
            userName={user.display_name}
            onFirstMessage={active && !activeGroup ? autoTitle(active) : undefined}
            onRequireSession={requireSession}
            onActivity={refresh}
            onWorkingBotsChange={onWorkingBotsChange}
            running={active ? (sessions.find((s) => s.id === active)?.running ?? false) : false}
            initialModel={active ? sessions.find((s) => s.id === active)?.model ?? null : null}
            missions={sessionMissions}
            onOpenSettings={(section) => {
              setSettingsSection(section);
              setAdminSection(null);
            }}
          />
          </>
        )}
      </main>
      {groupEditor !== undefined && <BotGroupEditor key={groupEditor?.id ?? "new"} group={groupEditor} bots={bots}
        onClose={() => setGroupEditor(undefined)}
        onSaved={group => {
          setBotGroups(previous => [...previous.filter(g => g.id !== group.id), group]);
          setGroupEditor(undefined);
          openGroup(group);
          void refresh();
        }}
        onDeleted={group => {
          setBotGroups(previous => previous.filter(g => g.id !== group.id));
          setSessions(previous => previous.filter(s => s.id !== group.session_id));
          setGroupEditor(undefined);
          if (active === group.session_id) { setActive(null); setSelectedBotId(bots[0]?.id ?? null); }
          void refresh();
        }} />}
      {botEditor && <BotEditor key={botEditor.id} bot={botEditor}
        onClose={() => setBotEditor(null)}
        onSaved={(updated) => {
          setBots(previous => previous.map(item => item.id === updated.id ? updated : item));
          setBotEditor(null);
          void refresh();
        }}
        onDeleted={(deleted) => {
          setBotEditor(null);
          setBots(previous => previous.filter(item => item.id !== deleted.id));
          if (selectedBotId === deleted.id) {
            setSelectedBotId(null);
            setActive(null);
          }
          void refresh();
        }} />}
    </div>
  );
}
