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
import { AlarmClock, ChevronDown, Loader2, LogOut, Menu, MessageCircle, MessageSquare, Plus, Search, Settings as SettingsIcon, Shield, User as UserIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ADMIN_SECTIONS, AdminPanel, type AdminSection } from "./Admin";
import { Chat } from "./Chat";
import { ErrorText } from "./ErrorText";
import { Brand, SoftnixLogo, SoftnixMark } from "./Logo";
import { PasswordField } from "./PasswordField";
import { SETTINGS_SECTIONS, SettingsPanel, type SettingsSection } from "./Settings";
import { ApiError, AuthUser, SessionInfo, api, clearToken, getToken, setToken } from "./api";
import { MOBILE_QUERY, PHONE_QUERY, useMediaQuery } from "./useMediaQuery";

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

/** "New chat" trigger that collapses to an icon-only button in the rail — a
 * plain full-width Button would overflow the collapsed sidenav's narrow rail. */
function NewChatButton({ onClick }: { onClick: () => void }) {
  const { isCollapsed } = useSideNavCollapse();
  if (isCollapsed) {
    return <IconButton label="New chat" icon={<Icon icon={Plus} size="sm" />} clickAction={onClick} />;
  }
  return <Button label="New chat" icon={<Icon icon={Plus} size="sm" />} size="sm" clickAction={onClick} />;
}

/** Brand lockup that drops the wordmark in the collapsed rail, keeping just the
 * square icon mark — the full wordmark image is too wide for the narrow rail
 * and gets clipped/squeezed there. */
function SidebarBrand() {
  const { isCollapsed } = useSideNavCollapse();
  return <div className="claw-sidenav-brand">{isCollapsed ? <SoftnixMark size={22} /> : <Brand height={22} />}</div>;
}

/** Truncate a chat title for the narrow collapsed-rail popover. */
function truncateTitle(title: string, max = 28) {
  return title.length > max ? title.slice(0, max) + "…" : title;
}

const RECENTS_INITIAL = 25;
const RECENTS_STEP = 50;
const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

interface SessionGroup {
  key: string;
  label: string;
  items: SessionInfo[];
}

/** Bucket sessions (already newest→oldest) into Today / Yesterday / Previous 7
 * days / Previous 30 days / then per-month, à la Claude & ChatGPT. Insertion
 * order of the returned groups is newest→oldest since the input is desc-sorted. */
function groupSessions(sessions: SessionInfo[]): SessionGroup[] {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const DAY = 86_400_000;
  const order: SessionGroup[] = [];
  const byKey = new Map<string, SessionGroup>();
  const push = (key: string, label: string, s: SessionInfo) => {
    let g = byKey.get(key);
    if (!g) {
      g = { key, label, items: [] };
      byKey.set(key, g);
      order.push(g);
    }
    g.items.push(s);
  };
  for (const s of sessions) {
    const t = new Date(s.updated_at).getTime();
    if (Number.isNaN(t)) {
      push("older", "Older", s);
    } else if (t >= startOfToday) {
      push("today", "Today", s);
    } else if (t >= startOfToday - DAY) {
      push("yesterday", "Yesterday", s);
    } else if (t >= startOfToday - 7 * DAY) {
      push("7d", "Previous 7 days", s);
    } else if (t >= startOfToday - 30 * DAY) {
      push("30d", "Previous 30 days", s);
    } else {
      const d = new Date(t);
      const label =
        d.getFullYear() === now.getFullYear()
          ? MONTH_NAMES[d.getMonth()]
          : `${MONTH_NAMES[d.getMonth()]} ${d.getFullYear()}`;
      push(`${d.getFullYear()}-${d.getMonth()}`, label, s);
    }
  }
  return order;
}

/** Recent-chats list. Grouped by date, search-filterable, and capped to a
 * window with "Show older" so a long history stays fast and scannable.
 * Expanded: stacked SideNavSections. Collapsed: a flyout with the same data. */
function RecentsNav({
  sessions,
  active,
  done,
  onSelect,
  onDelete,
}: {
  sessions: SessionInfo[];
  active: string | null;
  done: Set<string>;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const { isCollapsed } = useSideNavCollapse();
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [visible, setVisible] = useState(RECENTS_INITIAL);

  // A new search starts from the top of its (usually short) result set.
  useEffect(() => setVisible(RECENTS_INITIAL), [query]);

  const q = query.trim().toLowerCase();
  const filtered = q ? sessions.filter((s) => s.title.toLowerCase().includes(q)) : sessions;
  const shown = filtered.slice(0, visible);
  const groups = groupSessions(shown);
  const hasMore = filtered.length > shown.length;

  // Running spinner (turn processing) or a "new response" dot (finished while
  // you were elsewhere) — shown until the row is hovered (which reveals delete).
  const statusFor = (s: SessionInfo) =>
    s.running ? (
      <span className="claw-recent-status" title="Processing…">
        <Icon icon={Loader2} size="xsm" />
      </span>
    ) : done.has(s.id) ? (
      <span className="claw-recent-status claw-recent-status--done" title="New response" />
    ) : null;

  const search = (
    <div className="claw-recents-search">
      <TextInput
        label="Search chats"
        isLabelHidden
        size="sm"
        startIcon={<Icon icon={Search} size="sm" color="secondary" />}
        placeholder="Search chats…"
        value={query}
        onChange={setQuery}
        hasClear
      />
    </div>
  );

  if (!isCollapsed) {
    return (
      <>
        {search}
        {groups.length === 0 ? (
          <div className="claw-recents-empty">
            <Text size="sm" color="secondary">
              {q ? "No chats match your search." : "No conversations yet."}
            </Text>
          </div>
        ) : (
          groups.map((g) => (
            <SideNavSection key={g.key} title={g.label}>
              {g.items.map((s) => (
                <div
                  key={s.id}
                  className={`claw-recent-row${done.has(s.id) ? " claw-recent-row--unread" : ""}`}
                >
                  <SideNavItem
                    label={s.title}
                    icon={s.channel === "schedule" ? AlarmClock : MessageSquare}
                    isSelected={s.id === active}
                    onClick={() => onSelect(s.id)}
                  />
                  {statusFor(s)}
                  <span className="claw-recent-delete">
                    <IconButton
                      label="Delete chat"
                      icon={<Icon icon="close" size="xsm" />}
                      variant="ghost"
                      size="sm"
                      clickAction={(e) => {
                        e.stopPropagation();
                        onDelete(s.id);
                      }}
                    />
                  </span>
                </div>
              ))}
            </SideNavSection>
          ))
        )}
        {hasMore && (
          <button
            type="button"
            className="claw-recents-more"
            onClick={() => setVisible((v) => v + RECENTS_STEP)}
          >
            <Icon icon={ChevronDown} size="sm" />
            Show older
          </button>
        )}
      </>
    );
  }

  return (
    <div className="claw-recents-collapsed">
      <Popover
        label="Recents"
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
            {search}
            {groups.length === 0 ? (
              <Text size="sm" color="secondary">
                {q ? "No chats match your search." : "No conversations yet."}
              </Text>
            ) : (
              groups.map((g) => (
                <div key={g.key} className="claw-recents-popover-group">
                  <Text size="sm" weight="semibold" color="secondary" className="claw-recents-popover-title">
                    {g.label}
                  </Text>
                  {g.items.map((s) => (
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
                          onSelect(s.id);
                          setIsOpen(false);
                        }}
                      />
                      <IconButton
                        label="Delete chat"
                        icon={<Icon icon="close" size="xsm" />}
                        variant="ghost"
                        size="sm"
                        clickAction={() => onDelete(s.id)}
                      />
                    </div>
                  ))}
                </div>
              ))
            )}
            {hasMore && (
              <button
                type="button"
                className="claw-recents-more"
                onClick={() => setVisible((v) => v + RECENTS_STEP)}
              >
                <Icon icon={ChevronDown} size="sm" />
                Show older
              </button>
            )}
          </div>
        }
      >
        <IconButton label="Recents" icon={<Icon icon={MessageCircle} size="sm" />} variant="ghost" />
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
            "Invalid email or password. New here or recently added by an administrator? Check your email for an activation link."
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
        <SoftnixLogo height={44} />
        <Text type="display-3">PrivateClaw</Text>
        <Text color="secondary">Your personal AI agent</Text>
        <Text size="sm" color="secondary">
          If an account with a password exists for {email || "that email"}, we've sent a link to reset it. Check
          your inbox (and spam folder).
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
      <SoftnixLogo height={44} />
      <Text type="display-3">PrivateClaw</Text>
      <Text color="secondary">Your personal AI agent</Text>
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
                    ? "Send reset link"
                    : "Reset password"
        }
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
  const [user, setUser] = useState<AuthUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(null);
  const [adminSection, setAdminSection] = useState<AdminSection | null>(null);
  const [authError, setAuthError] = useState("");
  const [activationToken, setActivationToken] = useState("");
  const [resetToken, setResetToken] = useState("");
  // Responsive shell. Below the tablet width the sidebar becomes an off-canvas
  // drawer (navOpen); on desktop `collapsed` drives the rail. Control Plane is
  // hidden on phones (see the trade-off note in the render).
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const isPhone = useMediaQuery(PHONE_QUERY);
  const [navOpen, setNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  // Per-session "last read" timestamps (id -> epoch ms), persisted to
  // localStorage — see LAST_READ_KEY above for why this replaced an
  // in-memory-only heuristic.
  const [lastRead, setLastRead] = useState<Record<string, number>>(loadLastRead);
  const activeRef = useRef<string | null>(null);
  const toast = useToast();

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
    () => api.listSessions().then(setSessions).catch(() => undefined),
    [],
  );

  useEffect(() => {
    if (user) void refresh();
  }, [user, refresh]);

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

  // "New chat" just clears the view to the draft landing — no session is
  // created until the user actually sends something (see requireSession),
  // matching claude.ai and avoiding a pile-up of empty "New chat" sessions.
  const newChat = useCallback(() => {
    setActive(null);
    setSettingsSection(null);
    setAdminSection(null);
  }, []);

  // Materialize a session on demand (first message / attachment from the draft
  // landing) and make it active. Returns the new id for immediate use.
  const requireSession = useCallback(async () => {
    const created = await api.createSession();
    await refresh();
    setActive(created.id);
    setSettingsSection(null);
    setAdminSection(null);
    return created.id;
  }, [refresh]);

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
    clearToken();
    setUser(null);
    setSessions([]);
    setActive(null);
    setSettingsSection(null);
    setAdminSection(null);
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
  const showAdmin = user.is_admin && !isPhone; // aggressive: no admin console on phones
  const currentTitle = adminSection
    ? ADMIN_SECTIONS.find((s) => s.key === adminSection)?.label
    : settingsSection
      ? SETTINGS_SECTIONS.find((s) => s.key === settingsSection)?.label
      : "Chat";

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
        topContent={
          <div className="claw-sidenav-top">
            <NewChatButton onClick={() => { newChat(); closeDrawer(); }} />
          </div>
        }
        footer={
          <div className="claw-sidenav-footer">
            <SideNavItem label={user.display_name || user.email} icon={UserIcon}>
              <SideNavItem
                label="Settings"
                icon={SettingsIcon}
                collapsible={{ defaultIsCollapsed: true }}
              >
                {SETTINGS_SECTIONS.map((s) => (
                  <SideNavItem
                    key={s.key}
                    label={s.label}
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
                    label="Control Plane"
                    icon={Shield}
                    collapsible={{ defaultIsCollapsed: true }}
                  >
                    {ADMIN_SECTIONS.map((s) => (
                      <SideNavItem
                        key={s.key}
                        label={s.label}
                        icon={s.icon}
                        isSelected={adminSection === s.key}
                        onClick={() => {
                          setAdminSection(s.key);
                          setSettingsSection(null);
                          setActive(null);
                          closeDrawer();
                        }}
                      />
                    ))}
                  </SideNavItem>
                </div>
              )}
              <SideNavItem label="Log out" icon={LogOut} onClick={logout} />
            </SideNavItem>
          </div>
        }
        footerIcons={isMobile ? undefined : <SideNavCollapseButton />}
      >
        <RecentsNav
          sessions={sessions}
          active={active}
          done={doneSessions}
          onSelect={(id) => {
            setActive(id);
            setSettingsSection(null);
            setAdminSection(null);
            closeDrawer();
          }}
          onDelete={(id) => {
            void api.deleteSession(id).then(() => {
              if (active === id) setActive(null);
              void refresh();
            });
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
          isPhone ? (
            <div className="claw-mobile-blocked">
              <Text weight="semibold">Control Plane isn't available on phones</Text>
              <Text color="secondary" as="p">
                It's built for wide screens (charts, tables, audit logs). Please open it on a
                tablet in landscape or a desktop.
              </Text>
            </div>
          ) : (
            <AdminPanel section={adminSection} selfId={user.id} />
          )
        ) : settingsSection ? (
          <SettingsPanel section={settingsSection} />
        ) : (
          <Chat
            sessionId={active}
            userName={user.display_name}
            onFirstMessage={active ? autoTitle(active) : undefined}
            onRequireSession={requireSession}
            onActivity={refresh}
            running={active ? (sessions.find((s) => s.id === active)?.running ?? false) : false}
            initialModel={active ? sessions.find((s) => s.id === active)?.model ?? null : null}
            onOpenSettings={(section) => {
              setSettingsSection(section);
              setAdminSection(null);
            }}
          />
        )}
      </main>
    </div>
  );
}
