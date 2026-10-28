import type { ToastOptions } from "@astryxdesign/core/Toast";
import { useT } from "./branding";
import type { ConnectorCallbackStatus } from "./shared-api";

const MCP_OAUTH_MESSAGE: Record<ConnectorCallbackStatus, string> = {
  connected: "connectorCallback.mcp.connected",
  denied: "connectorCallback.mcp.denied",
  expired: "connectorCallback.mcp.expired",
  browser_mismatch: "connectorCallback.mcp.browserMismatch",
  token_failed: "connectorCallback.mcp.tokenFailed",
  error: "connectorCallback.failed",
};

// Rendered inside the toast rather than translated up front: the bounce is
// read on first mount, before the branding fetch has settled the language.
function Message({ messageKey, name }: { messageKey: string; name?: string }) {
  const t = useT();
  return <>{t(messageKey, name === undefined ? undefined : { name })}</>;
}

/** The toast for an OAuth bounce back into the app, or null when the URL is
 * not one. `?connector=<preset_key>` marks the preset flow; anything else is
 * the per-connector MCP flow, which sends `connector_id` when it knows it. */
export function connectorCallbackToast(params: URLSearchParams): ToastOptions | null {
  const status = params.get("connector_status");
  if (!status) return null;
  const preset = params.get("connector");
  if (preset) {
    if (status === "connected") {
      return { body: <Message messageKey="connectorCallback.preset.connected" name={preset} />, type: "info", autoHideDuration: 3000 };
    }
    const key = status === "name_conflict" ? "connectorCallback.preset.nameConflict" : "connectorCallback.failed";
    return { body: <Message messageKey={key} name={preset} />, type: "error" };
  }
  const key = Object.hasOwn(MCP_OAUTH_MESSAGE, status)
    ? MCP_OAUTH_MESSAGE[status as ConnectorCallbackStatus]
    : MCP_OAUTH_MESSAGE.error;
  return status === "connected"
    ? { body: <Message messageKey={key} />, type: "info", autoHideDuration: 3000 }
    : { body: <Message messageKey={key} />, type: "error" };
}
