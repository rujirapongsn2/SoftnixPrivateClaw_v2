import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Icon } from "@astryxdesign/core/Icon";
import { Switch } from "@astryxdesign/core/Switch";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Ban, Globe, MessageSquare, Plus, Send, Shield, ShieldOff, Users } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ErrorText } from "./ErrorText";
import { AdminUser, api } from "./api";

export function AdminDialog({
  isOpen,
  onOpenChange,
  selfId,
}: {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  selfId: string;
}) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [stats, setStats] = useState<Record<string, number | boolean>>({});
  const [monitorOnly, setMonitorOnly] = useState(false);
  const [creating, setCreating] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      setError("");
      setUsers(await api.adminListUsers());
      setStats(await api.adminStats());
      setMonitorOnly((await api.getPolicy()).monitor_only);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    if (isOpen) void reload();
  }, [isOpen, reload]);

  const guard = async (fn: () => Promise<void>) => {
    try {
      setError("");
      await fn();
      await reload();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <Dialog isOpen={isOpen} onOpenChange={onOpenChange} width={960}>
      <div className="claw-settings">
        <DialogHeader title="Admin console" onOpenChange={onOpenChange} />
        <div className="claw-settings-body">
          <div className="claw-panel">
            <div className="claw-row">
              <Badge variant="neutral" icon={<Icon icon={Users} size="xsm" />} label={`${stats.users ?? 0} users`} />
              <Badge variant="neutral" icon={<Icon icon={Shield} size="xsm" />} label={`${stats.admins ?? 0} admins`} />
              <Badge variant="neutral" icon={<Icon icon={Ban} size="xsm" />} label={`${stats.suspended ?? 0} suspended`} />
              <Badge
                variant="neutral"
                icon={<Icon icon={MessageSquare} size="xsm" />}
                label={`${stats.sessions ?? 0} sessions`}
              />
              <Badge
                variant={stats.browser_enabled ? "success" : "neutral"}
                icon={<Icon icon={Globe} size="xsm" />}
                label={stats.browser_enabled ? "browser on" : "browser off"}
              />
              <Badge
                variant={stats.telegram_enabled ? "success" : "neutral"}
                icon={<Icon icon={Send} size="xsm" />}
                label={stats.telegram_enabled ? "telegram on" : "telegram off"}
              />
            </div>
            {error && <ErrorText>{error}</ErrorText>}

            <Card padding={2} variant="muted">
              <div className="claw-row claw-row-between">
                <div>
                  <Text weight="semibold">Control policy</Text>
                  <Text size="sm" color="secondary" as="p">
                    {monitorOnly
                      ? "Monitor-only: sensitive data is logged but not masked or blocked."
                      : "Enforcing: PII/secrets are masked and blocked across all users."}
                  </Text>
                </div>
                <label className="claw-toggle">
                  <Text size="sm" color="secondary">Enforce</Text>
                  <Switch
                    value={!monitorOnly}
                    label="Enforce control policy"
                    isLabelHidden
                    changeAction={(checked) =>
                      guard(async () => {
                        await api.setPolicy(!checked);
                      })
                    }
                  />
                </label>
              </div>
            </Card>

            {creating ? (
              <Card padding={2}>
                <div className="claw-panel">
                  <TextInput label="Email" type="email" value={email} onChange={setEmail} />
                  <TextInput label="Password" type="password" value={password} onChange={setPassword} />
                  <div className="claw-row">
                    <Button
                      label="Create user"
                      icon={<Icon icon={Plus} size="sm" />}
                      clickAction={() =>
                        guard(async () => {
                          await api.adminCreateUser(email, password, false);
                          setCreating(false);
                          setEmail("");
                          setPassword("");
                        })
                      }
                    />
                    <Button label="Cancel" variant="ghost" clickAction={() => setCreating(false)} />
                  </div>
                </div>
              </Card>
            ) : (
              <div className="claw-row claw-row-between">
                <Text color="secondary">Manage everyone with access to this Softnix PrivateClaw deployment.</Text>
                <Button
                  label="Add user"
                  icon={<Icon icon={Plus} size="sm" />}
                  size="sm"
                  clickAction={() => setCreating(true)}
                />
              </div>
            )}

            {users.map((u) => (
              <Card key={u.id} padding={2} variant={u.is_active ? "default" : "muted"}>
                <div className="claw-row claw-row-between">
                  <div>
                    <div className="claw-row">
                      <Text weight="semibold">{u.email}</Text>
                      {u.is_admin && <Badge variant="purple" icon={<Icon icon={Shield} size="xsm" />} label="admin" />}
                      {!u.is_active && (
                        <Badge variant="error" icon={<Icon icon={ShieldOff} size="xsm" />} label="suspended" />
                      )}
                    </div>
                    <Text size="sm" color="secondary" as="p">
                      {u.sessions} sessions · joined {new Date(u.created_at).toLocaleDateString()}
                    </Text>
                  </div>
                  <div className="claw-row">
                    <label className="claw-toggle">
                      <Text size="sm" color="secondary">Admin</Text>
                      <Switch
                        value={u.is_admin}
                        label={`Admin ${u.email}`}
                        isLabelHidden
                        isDisabled={u.id === selfId}
                        changeAction={(checked) =>
                          guard(() => api.adminUpdateUser(u.id, { is_admin: checked }).then(() => {}))
                        }
                      />
                    </label>
                    <label className="claw-toggle">
                      <Text size="sm" color="secondary">Active</Text>
                      <Switch
                        value={u.is_active}
                        label={`Active ${u.email}`}
                        isLabelHidden
                        isDisabled={u.id === selfId}
                        changeAction={(checked) =>
                          guard(() => api.adminUpdateUser(u.id, { is_active: checked }).then(() => {}))
                        }
                      />
                    </label>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </div>
      </div>
    </Dialog>
  );
}
