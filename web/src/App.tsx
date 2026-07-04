import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { IconButton } from "@astryxdesign/core/IconButton";
import {
  SideNav,
  SideNavCollapseButton,
  SideNavItem,
  SideNavSection,
  useSideNavCollapse,
} from "@astryxdesign/core/SideNav";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { LogOut, MessageSquare, Plus, Settings as SettingsIcon, Shield, User as UserIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { AdminDialog } from "./Admin";
import { Chat } from "./Chat";
import { ErrorText } from "./ErrorText";
import { Brand, SoftnixLogo } from "./Logo";
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

/** Brand lockup that drops the wordmark in the collapsed rail, keeping just the logo mark. */
function SidebarBrand() {
  const { isCollapsed } = useSideNavCollapse();
  return <div className="claw-sidenav-brand">{isCollapsed ? <SoftnixLogo height={22} /> : <Brand height={22} />}</div>;
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
  const [adminOpen, setAdminOpen] = useState(false);
  const [authError, setAuthError] = useState("");

  // Capture a JWT (or error) returned by the OIDC callback, then restore session.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlToken = params.get("token");
    const urlError = params.get("auth_error");
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

  const newChat = useCallback(async () => {
    const created = await api.createSession();
    await refresh();
    setActive(created.id);
    setSettingsSection(null);
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
              <SideNavItem label="Settings" icon={SettingsIcon} collapsible>
                {SETTINGS_SECTIONS.map((s) => (
                  <SideNavItem
                    key={s.key}
                    label={s.label}
                    icon={s.icon}
                    isSelected={settingsSection === s.key}
                    onClick={() => {
                      setSettingsSection(s.key);
                      setActive(null);
                    }}
                  />
                ))}
              </SideNavItem>
              {user.is_admin && (
                <SideNavItem
                  label="Admin console"
                  icon={Shield}
                  onClick={() => setAdminOpen(true)}
                />
              )}
              <SideNavItem label="Log out" icon={LogOut} onClick={logout} />
            </SideNavItem>
          </div>
        }
        footerIcons={<SideNavCollapseButton />}
        collapsible={{ hasButton: false }}
      >
        <SideNavSection title="Recents">
          {sessions.map((s) => (
            <div key={s.id} className="claw-recent-row">
              <SideNavItem
                label={s.title}
                icon={MessageSquare}
                isSelected={s.id === active}
                onClick={() => {
                  setActive(s.id);
                  setSettingsSection(null);
                }}
              />
              <span className="claw-recent-delete">
                <IconButton
                  label="Delete chat"
                  icon={<Icon icon="close" size="xsm" />}
                  variant="ghost"
                  size="sm"
                  clickAction={(e) => {
                    e.stopPropagation();
                    void api.deleteSession(s.id).then(() => {
                      if (active === s.id) setActive(null);
                      void refresh();
                    });
                  }}
                />
              </span>
            </div>
          ))}
        </SideNavSection>
      </SideNav>

      <main className="claw-main">
        {settingsSection ? (
          <SettingsPanel section={settingsSection} />
        ) : active ? (
          <Chat sessionId={active} userName={user.display_name} onFirstMessage={autoTitle(active)} />
        ) : (
          <div className="claw-welcome">
            <SoftnixLogo height={40} />
            <Text type="display-3">How can Claw help you today?</Text>
            <Button label="Start a new chat" icon={<Icon icon={Plus} size="sm" />} clickAction={newChat} />
          </div>
        )}
      </main>

      {user.is_admin && (
        <AdminDialog isOpen={adminOpen} onOpenChange={setAdminOpen} selfId={user.id} />
      )}
    </div>
  );
}
