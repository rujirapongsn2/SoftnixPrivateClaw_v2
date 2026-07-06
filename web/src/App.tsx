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
import { ChevronDown, Loader2, LogOut, MessageCircle, MessageSquare, Plus, Search, Settings as SettingsIcon, Shield, User as UserIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ADMIN_SECTIONS, AdminPanel, type AdminSection } from "./Admin";
import { Chat } from "./Chat";
import { ErrorText } from "./ErrorText";
import { Brand, SoftnixLogo, SoftnixMark } from "./Logo";
import { SETTINGS_SECTIONS, SettingsPanel, type SettingsSection } from "./Settings";
import { AuthUser, SessionInfo, api, clearToken, getToken, setToken } from "./api";

const PROVIDER_LABELS: Record<string, string> = { google: "Google", microsoft: "Microsoft" };

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
                <div key={s.id} className="claw-recent-row">
                  <SideNavItem
                    label={s.title}
                    icon={MessageSquare}
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
                    <div key={s.id} className="claw-recents-popover-row">
                      <Button
                        label={truncateTitle(s.title)}
                        icon={<Icon icon={MessageSquare} size="sm" />}
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

function Auth({ onDone, initialError }: { onDone: (user: AuthUser) => void; initialError?: string }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(initialError ?? "");
  const [busy, setBusy] = useState(false);
  const [providers, setProviders] = useState<string[]>([]);

  useEffect(() => {
    api.providers().then((r) => setProviders(r.providers)).catch(() => setProviders([]));
  }, []);

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const res = mode === "login" ? await api.login(email, password) : await api.register(email, password);
      setToken(res.access_token);
      onDone(res.user);
    } catch (e) {
      setError(mode === "login" ? "Invalid email or password." : String(e).replace(/^Error:\s*/, ""));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="claw-login">
      <SoftnixLogo height={44} />
      <Text type="display-3">PrivateClaw</Text>
      <Text color="secondary">Your personal AI agent</Text>
      <TextInput label="Email" type="email" value={email} onChange={setEmail} />
      <TextInput label="Password" type="password" value={password} onChange={setPassword} />
      {error && <ErrorText>{error}</ErrorText>}
      <Button
        label={busy ? "…" : mode === "login" ? "Log in" : "Create account"}
        isDisabled={busy || !email || password.length < 8}
        clickAction={submit}
      />
      <Button
        label={mode === "login" ? "Need an account? Register" : "Have an account? Log in"}
        variant="ghost"
        clickAction={() => {
          setMode(mode === "login" ? "register" : "login");
          setError("");
        }}
      />
      {providers.length > 0 && (
        <>
          <Text size="sm" color="secondary">or continue with</Text>
          {providers.map((p) => (
            <Button
              key={p}
              label={PROVIDER_LABELS[p] ?? p}
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
  // Sessions that finished a turn while the user wasn't viewing them — shown
  // with a "new response" dot in the sidebar until opened.
  const [doneSessions, setDoneSessions] = useState<Set<string>>(new Set());
  const prevRunningRef = useRef<Set<string>>(new Set());
  const activeRef = useRef<string | null>(null);
  const toast = useToast();

  // Capture a JWT (or error) returned by the OIDC callback, then restore session.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlToken = params.get("token");
    const urlError = params.get("auth_error");
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

  // Keep `activeRef` in sync so the polling reconciler can read it without
  // re-subscribing the interval on every navigation.
  useEffect(() => {
    activeRef.current = active;
    // Opening a session clears its "new response" marker.
    if (active) setDoneSessions((prev) => (prev.has(active) ? new Set([...prev].filter((id) => id !== active)) : prev));
  }, [active]);

  // Poll the session list so the sidebar reflects background turns (running →
  // done) even when the user has navigated away from the processing chat.
  useEffect(() => {
    if (!user) return;
    const id = setInterval(() => void refresh(), 3000);
    return () => clearInterval(id);
  }, [user, refresh]);

  // Detect running→done transitions to flag sessions with a fresh response the
  // user hasn't seen yet (skip the one they're currently viewing).
  useEffect(() => {
    const nowRunning = new Set(sessions.filter((s) => s.running).map((s) => s.id));
    const prev = prevRunningRef.current;
    const finished = [...prev].filter((idv) => !nowRunning.has(idv) && idv !== activeRef.current);
    if (finished.length) {
      setDoneSessions((cur) => {
        const next = new Set(cur);
        finished.forEach((idv) => next.add(idv));
        return next;
      });
    }
    prevRunningRef.current = nowRunning;
  }, [sessions]);

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
    clearToken();
    setUser(null);
    setSessions([]);
    setActive(null);
    setSettingsSection(null);
    setAdminSection(null);
  };

  if (checking) return <div className="claw-login"><Text color="secondary">Loading…</Text></div>;
  if (!user) return <Auth onDone={setUser} initialError={authError} />;

  return (
    <div className="claw-app">
      <SideNav
        header={<SidebarBrand />}
        topContent={
          <div className="claw-sidenav-top">
            <NewChatButton onClick={newChat} />
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
                    }}
                  />
                ))}
              </SideNavItem>
              {user.is_admin && (
                <div className="claw-nav-admin">
                  <SideNavItem
                    label="Admin console"
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
        footerIcons={<SideNavCollapseButton />}
        collapsible={{ hasButton: false }}
      >
        <RecentsNav
          sessions={sessions}
          active={active}
          done={doneSessions}
          onSelect={(id) => {
            setActive(id);
            setSettingsSection(null);
            setAdminSection(null);
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
        {adminSection ? (
          <AdminPanel section={adminSection} selfId={user.id} />
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
          />
        )}
      </main>
    </div>
  );
}
