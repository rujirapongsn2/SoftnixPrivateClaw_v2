import {
  ChatComposer,
  ChatLayout,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageList,
  ChatMessageMetadata,
  ChatToolCalls,
} from "@astryxdesign/core/Chat";
import { Markdown } from "@astryxdesign/core/Markdown";
import { Button } from "@astryxdesign/core/Button";
import { IconButton } from "@astryxdesign/core/IconButton";
import { Icon } from "@astryxdesign/core/Icon";
import { Popover } from "@astryxdesign/core/Popover";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { useToast } from "@astryxdesign/core/Toast";
import {
  ArrowLeft,
  BookOpen,
  Box,
  Check,
  ChevronDown,
  ChevronRight,
  Code,
  Copy,
  ExternalLink,
  File as FileIcon,
  GraduationCap,
  Image as ImageIcon,
  Lightbulb,
  PanelRight,
  Paperclip,
  PenLine,
  Plug,
  Plus,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Terminal,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorText } from "./ErrorText";
import { ExecutionPanel } from "./ExecutionPanel";
import {
  AgentEvent,
  AttachmentRef,
  ConnectorInfo,
  ModelOption,
  SkillInfo,
  api,
  fileUrl,
  openChatSocket,
} from "./api";
import { SoftnixLogo } from "./Logo";

const COST_LABEL: Record<string, string> = {
  low: "Low cost",
  medium: "Medium cost",
  high: "High cost",
  very_high: "Very high cost",
};

// Starter chips shown on the empty landing screen, à la claude.ai. Clicking one
// seeds the composer with an opening phrase and refocuses so the user keeps
// typing — it does not send on its own.
const SUGGESTIONS: { label: string; icon: typeof Code; prompt: string }[] = [
  { label: "Write", icon: PenLine, prompt: "Help me write " },
  { label: "Learn", icon: GraduationCap, prompt: "Explain " },
  { label: "Code", icon: Code, prompt: "Help me write code that " },
  { label: "Analyze", icon: Lightbulb, prompt: "Help me analyze " },
  { label: "Claw's choice", icon: Sparkles, prompt: "Surprise me — " },
];

interface ToolCallRow {
  name: string;
  status: "running" | "complete" | "error";
  argsPreview?: string;
  resultPreview?: string;
  startedAt: number;
  duration?: string;
}

type PermissionMode = "ask" | "auto";

interface ConfirmRow {
  requestId: string;
  tool: string;
  argsPreview?: string;
  status: "pending" | "approved" | "denied";
}

type TranscriptItem =
  | { kind: "message"; role: "user" | "assistant"; content: string; artifacts?: string[] }
  | { kind: "tools"; calls: ToolCallRow[] }
  | { kind: "confirm"; row: ConfirmRow };

const PERMISSION_OPTS: {
  key: PermissionMode;
  label: string;
  desc: string;
  icon: typeof ShieldCheck;
}[] = [
  {
    key: "ask",
    label: "Ask",
    desc: "Ask before running commands in the sandbox",
    icon: ShieldCheck,
  },
  {
    key: "auto",
    label: "Auto",
    desc: "Run sandbox commands automatically without asking",
    icon: ShieldAlert,
  },
];

interface ChatProps {
  /** Active session, or null for the draft landing (no session yet). */
  sessionId: string | null;
  userName?: string;
  onFirstMessage?: (text: string) => void;
  /** Create a session on demand (first message / attachment) and return its id. */
  onRequireSession?: () => Promise<string>;
  /** Fired on turn start/finish so the parent can refresh sidebar status. */
  onActivity?: () => void;
  /** Whether the backend reports this session as processing (from the parent's
   * session poll) — lets a chat reopened mid-turn show "Thinking" and recover
   * a completion event missed while the socket was closed. */
  running?: boolean;
  initialModel?: string | null;
}

export function Chat({
  sessionId,
  userName,
  onFirstMessage,
  onRequireSession,
  onActivity,
  running,
  initialModel,
}: ChatProps) {
  const [items, setItems] = useState<TranscriptItem[]>([]);
  const [streaming, setStreaming] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);
  const [uploading, setUploading] = useState(false);
  const [feedback, setFeedback] = useState<Record<number, "up" | "down">>({});
  const [copied, setCopied] = useState<Record<number, boolean>>({});
  const [models, setModels] = useState<ModelOption[]>([]);
  const [model, setModel] = useState<string>(initialModel ?? "");
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [plusOpen, setPlusOpen] = useState(false);
  const [plusView, setPlusView] = useState<"root" | "skills" | "connectors">("root");
  const [modelOpen, setModelOpen] = useState(false);
  // Permission mode: "ask" pauses for user approval before unsafe sandbox
  // commands; "auto" runs them without asking. Remembered across sessions.
  const [permission, setPermission] = useState<PermissionMode>(
    () => (localStorage.getItem("claw_permission_mode") === "auto" ? "auto" : "ask"),
  );
  const [permOpen, setPermOpen] = useState(false);
  // Right execution panel: remember the user's explicit show/hide choice; a tool
  // starting auto-opens it transiently without overwriting that saved default.
  const [execOpen, setExecOpen] = useState(() => localStorage.getItem("claw_exec_open") === "1");
  const socketRef = useRef<WebSocket | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const sentCountRef = useRef(0);
  // A message typed on the draft landing, held until the freshly-created
  // session's socket opens, then flushed. Lets the composer create the session
  // lazily (like claude.ai) instead of on page load.
  const pendingRef = useRef<{ content: string; atts: AttachmentRef[] } | null>(null);
  const rawSendRef = useRef<(content: string, atts: AttachmentRef[]) => void>(() => {});
  // Did this mount's socket deliver turn_completed? If the parent's poll sees
  // the session finish and we DIDN'T (event missed while navigated away), we
  // refetch the persisted answer instead of losing it.
  const sawCompletionRef = useRef(false);
  const prevRunningRef = useRef(false);
  const toast = useToast();

  useEffect(() => {
    // Safari's contenteditable text layout picks shaping rules from the
    // element's language, and the page is lang="en" — without an explicit
    // hint it can skip Thai mark-positioning (GPOS) while typing, leaving
    // tone/vowel marks floating instead of stacked on their consonant.
    // Harmless for English text, so just always set it.
    const editable = document.querySelector<HTMLElement>(
      ".astryx-chat-composer-input [contenteditable]",
    );
    if (editable) editable.lang = "th";
  }, []);

  useEffect(() => {
    setItems([]);
    setStreaming("");
    setError("");
    setAttachments([]);
    setFeedback({});
    sentCountRef.current = 0;
    sawCompletionRef.current = false;
    prevRunningRef.current = !!running;
    // Draft landing: no session yet, so no socket and nothing to load.
    if (!sessionId) {
      setBusy(false);
      return;
    }
    // A pending draft message means this session was just created and is
    // therefore empty — skip the message fetch (which would race the flush
    // and could clobber the optimistic bubble) and keep the spinner up so the
    // greeting doesn't flash back in during the handoff. Also keep the spinner
    // up when reopening a session the backend says is still processing.
    const handoff = pendingRef.current !== null;
    setBusy(handoff || running === true);
    if (!handoff) {
      api
        .listMessages(sessionId)
        .then((msgs) => {
          sentCountRef.current = msgs.length;
          setItems(
            msgs.map((m) => ({
              kind: "message",
              role: m.role,
              content: m.content,
              artifacts: m.meta?.artifacts,
            })),
          );
        })
        .catch((e) => setError(String(e)));
    }

    const socket = openChatSocket(sessionId);
    socketRef.current = socket;
    socket.onopen = () => {
      const pending = pendingRef.current;
      if (pending) {
        pendingRef.current = null;
        rawSendRef.current(pending.content, pending.atts);
      }
    };
    socket.onmessage = (raw) => {
      const event: AgentEvent = JSON.parse(raw.data);
      switch (event.type) {
        case "turn_started":
          setBusy(true);
          setStreaming("");
          sawCompletionRef.current = false;
          onActivity?.();
          break;
        case "text_delta":
          setStreaming((prev) => prev + (event.text ?? ""));
          break;
        case "tool_started":
          setStreaming("");
          setExecOpen(true); // auto-open the execution panel while the agent works
          setItems((prev) => {
            const last = prev[prev.length - 1];
            const row: ToolCallRow = {
              name: event.tool ?? "tool",
              status: "running",
              argsPreview: event.args_preview,
              startedAt: Date.now(),
            };
            if (last?.kind === "tools") {
              return [...prev.slice(0, -1), { kind: "tools", calls: [...last.calls, row] }];
            }
            return [...prev, { kind: "tools", calls: [row] }];
          });
          break;
        case "tool_finished":
          setItems((prev) => {
            // The finished tool is the most recent still-running call. Don't
            // assume it's in the last item: an "ask"-mode confirm card gets
            // appended after the tools group, which previously left approved
            // tool steps spinning forever. Scan back for the last tools group
            // that still has a running call and complete it.
            for (let gi = prev.length - 1; gi >= 0; gi--) {
              const it = prev[gi];
              if (it.kind !== "tools") continue;
              let ri = -1;
              for (let k = it.calls.length - 1; k >= 0; k--) {
                if (it.calls[k].status === "running") {
                  ri = k;
                  break;
                }
              }
              if (ri === -1) continue;
              const calls = it.calls.map((c, k) =>
                k === ri
                  ? {
                      ...c,
                      status: (event.is_error ? "error" : "complete") as ToolCallRow["status"],
                      resultPreview: event.result_preview,
                      duration: `${((Date.now() - c.startedAt) / 1000).toFixed(1)}s`,
                    }
                  : c,
              );
              const next = [...prev];
              next[gi] = { kind: "tools", calls };
              return next;
            }
            return prev;
          });
          break;
        case "tool_confirm_request":
          setExecOpen(true);
          setItems((prev) => {
            // Ignore a resend for a card we already show (reconnect).
            if (
              prev.some(
                (it) => it.kind === "confirm" && it.row.requestId === event.request_id,
              )
            ) {
              return prev;
            }
            return [
              ...prev,
              {
                kind: "confirm",
                row: {
                  requestId: event.request_id ?? "",
                  tool: event.tool ?? "tool",
                  argsPreview: event.args_preview,
                  status: "pending",
                },
              },
            ];
          });
          break;
        case "tool_confirm_resolved":
          setItems((prev) =>
            prev.map((it) =>
              it.kind === "confirm" && it.row.requestId === event.request_id
                ? { kind: "confirm", row: { ...it.row, status: event.approved ? "approved" : "denied" } }
                : it,
            ),
          );
          break;
        case "turn_completed":
          setBusy(false);
          setStreaming("");
          sawCompletionRef.current = true;
          if (event.content) {
            setItems((prev) => [
              ...prev,
              { kind: "message", role: "assistant", content: event.content!, artifacts: event.artifacts },
            ]);
          }
          onActivity?.();
          break;
        case "turn_error":
          setBusy(false);
          setStreaming("");
          sawCompletionRef.current = true;
          setError(event.message ?? "unknown error");
          onActivity?.();
          break;
      }
    };
    socket.onclose = (ev) => {
      if (ev.code === 4401) setError("Authentication failed — check your token and email.");
    };
    return () => {
      // Closing a socket that's still mid-handshake (e.g. React StrictMode's
      // dev-only mount→cleanup→remount) logs a spurious "closed before the
      // connection is established" browser warning. Deferring the close
      // until it actually opens avoids that noise; already-open/closed
      // sockets close immediately as before.
      if (socket.readyState === WebSocket.CONNECTING) {
        socket.addEventListener("open", () => socket.close());
      } else {
        socket.close();
      }
    };
  }, [sessionId]);

  // Load the model picker options once. Default to the chat's sticky model,
  // otherwise the admin-configured default.
  useEffect(() => {
    api
      .listModels()
      .then((r) => {
        setModels(r.models);
        setModel((cur) => cur || initialModel || r.default || "");
      })
      .catch(() => setModels([]));
  }, [initialModel]);

  // Recover a completion missed while navigated away: when the parent's poll
  // flips this session running→done but our socket never delivered a
  // turn_completed (it fired during the gap), refetch the persisted transcript
  // so the answer isn't lost. Skips the normal case (we saw the completion).
  useEffect(() => {
    const was = prevRunningRef.current;
    prevRunningRef.current = !!running;
    if (was && !running && sessionId && !sawCompletionRef.current) {
      api
        .listMessages(sessionId)
        .then((msgs) => {
          sentCountRef.current = msgs.length;
          setItems(
            msgs.map((m) => ({
              kind: "message",
              role: m.role,
              content: m.content,
              artifacts: m.meta?.artifacts,
            })),
          );
          setBusy(false);
          setStreaming("");
        })
        .catch(() => undefined);
    }
  }, [running, sessionId]);

  // Enabled skills + ready connectors for the "+" menu. Refetch on session
  // change and whenever a turn finishes (running→false): a connector may only
  // connect during a chat turn, so this surfaces it without a manual reload.
  useEffect(() => {
    if (running) return;
    api.listSkills().then((s) => setSkills(s.filter((x) => x.enabled))).catch(() => setSkills([]));
    api
      .listConnectors()
      .then((c) => setConnectors(c.filter((x) => x.enabled && x.runtime.status === "connected")))
      .catch(() => setConnectors([]));
  }, [sessionId, running]);

  // Insert an "@mention" for a skill/connector at the caret, then refocus so the
  // user can keep typing. The agent already sees enabled skills + connected
  // connector tools in context, so the mention nudges it to use that one.
  const insertMention = useCallback((name: string) => {
    const editable = document.querySelector<HTMLElement>(
      ".astryx-chat-composer-input [contenteditable]",
    );
    if (editable) {
      editable.focus();
      document.execCommand("insertText", false, `@${name} `);
    }
    setPlusOpen(false);
    setPlusView("root");
  }, []);

  // Seed the composer with a starter phrase from a suggestion chip: clear any
  // existing content, drop in the phrase, and leave the caret at the end so the
  // user just keeps typing.
  const fillComposer = useCallback((text: string) => {
    const editable = document.querySelector<HTMLElement>(
      ".astryx-chat-composer-input [contenteditable]",
    );
    if (!editable) return;
    editable.focus();
    const sel = window.getSelection();
    sel?.selectAllChildren(editable);
    document.execCommand("insertText", false, text);
  }, []);

  // Push a message over the (open) socket and reflect it locally. Assumes a
  // connected session — the draft path in `send` routes through here only
  // after the new session's socket opens.
  const rawSend = useCallback(
    (content: string, atts: AttachmentRef[]) => {
      if (socketRef.current?.readyState !== WebSocket.OPEN) return;
      if (sentCountRef.current === 0 && content) onFirstMessage?.(content);
      sentCountRef.current += 1;
      socketRef.current.send(
        JSON.stringify({
          content,
          attachments: atts.map((a) => a.path),
          model: model || undefined,
          permission_mode: permission,
        }),
      );
      const shown = atts.length ? `${content}${content ? "\n" : ""}📎 ${atts.map((a) => a.name).join(", ")}` : content;
      setItems((prev) => [...prev, { kind: "message", role: "user", content: shown }]);
      setError("");
    },
    [onFirstMessage, model, permission],
  );

  // Answer a pending Ask-mode confirmation card and optimistically settle it.
  const sendDecision = useCallback((requestId: string, approved: boolean) => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type: "tool_decision", request_id: requestId, approved }));
    }
    setItems((prev) =>
      prev.map((it) =>
        it.kind === "confirm" && it.row.requestId === requestId
          ? { kind: "confirm", row: { ...it.row, status: approved ? "approved" : "denied" } }
          : it,
      ),
    );
  }, []);

  const changePermission = useCallback((mode: PermissionMode) => {
    setPermission(mode);
    localStorage.setItem("claw_permission_mode", mode);
    setPermOpen(false);
  }, []);

  // Keep a live ref so the socket's onopen handler (bound once per session)
  // always flushes through the latest rawSend without reconnecting.
  useEffect(() => {
    rawSendRef.current = rawSend;
  }, [rawSend]);

  const send = useCallback(
    (value: string) => {
      const content = value.trim();
      const atts = attachments;
      if (!content && atts.length === 0) return;
      // Draft landing: create the session first, then flush this message once
      // its socket connects (handled in the session effect's onopen).
      if (!sessionId) {
        pendingRef.current = { content, atts };
        setAttachments([]);
        setError("");
        onRequireSession?.().catch((e) => {
          pendingRef.current = null;
          setError(`Couldn't start chat: ${String(e)}`);
        });
        return;
      }
      if (socketRef.current?.readyState !== WebSocket.OPEN) return;
      rawSend(content, atts);
      setAttachments([]);
    },
    [attachments, sessionId, onRequireSession, rawSend],
  );

  const onPickFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;
      setUploading(true);
      setError("");
      try {
        // Attachments are stored per session, so materialize the draft session
        // before uploading.
        const sid = sessionId ?? (await onRequireSession?.());
        if (!sid) throw new Error("No session");
        const refs = await api.uploadAttachments(sid, Array.from(files));
        setAttachments((prev) => [...prev, ...refs]);
      } catch (e) {
        setError(`Upload failed: ${String(e)}`);
      } finally {
        setUploading(false);
        if (fileRef.current) fileRef.current.value = "";
      }
    },
    [sessionId, onRequireSession],
  );

  const rate = useCallback(
    (index: number, content: string, signal: "up" | "down") => {
      setFeedback((prev) => ({ ...prev, [index]: signal }));
      void api
        .submitFeedback(signal, { session_id: sessionId ?? undefined, message_preview: content.slice(0, 400) })
        .catch(() => undefined);
    },
    [sessionId],
  );

  const copyMessage = useCallback(
    (index: number, content: string) => {
      const markCopied = () => {
        setCopied((prev) => ({ ...prev, [index]: true }));
        setTimeout(() => setCopied((prev) => ({ ...prev, [index]: false })), 1500);
        toast({ body: "Copied to clipboard", type: "info", autoHideDuration: 2000 });
      };
      const legacyCopy = () => {
        // Fallback for browsers/contexts without the Clipboard API — e.g. any
        // page loaded over plain http (LAN/Tailscale IP, not localhost),
        // where `navigator.clipboard` doesn't exist at all.
        const textarea = document.createElement("textarea");
        textarea.value = content;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        try {
          if (document.execCommand("copy")) {
            markCopied();
          } else {
            toast({ body: "Couldn't copy to clipboard", type: "error" });
          }
        } catch {
          toast({ body: "Couldn't copy to clipboard", type: "error" });
        } finally {
          document.body.removeChild(textarea);
        }
      };

      try {
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(content).then(markCopied, legacyCopy);
        } else {
          legacyCopy();
        }
      } catch {
        legacyCopy();
      }
    },
    [toast],
  );

  const isEmpty = items.length === 0 && !streaming && !busy;

  // Flatten every tool-call group (in order) into the execution timeline.
  const execSteps = items.flatMap((it) => (it.kind === "tools" ? it.calls : []));

  const toggleExec = (v: boolean) => {
    setExecOpen(v);
    localStorage.setItem("claw_exec_open", v ? "1" : "0");
  };

  const greeting = (
    <div className="claw-greeting">
      <div className="claw-greeting-title">
        <SoftnixLogo height={44} />
        <Text type="display-2">
          {new Date().getHours() < 12 ? "Good morning" : "Hello"}
          {userName ? `, ${userName}` : " there"}
        </Text>
      </div>
    </div>
  );

  return (
    <div className="claw-chat-shell">
    <div className={`claw-chat${isEmpty ? " claw-chat--empty" : ""}`}>
      {!isEmpty && !execOpen && (
        <IconButton
          label="Show execution"
          icon={<Icon icon={PanelRight} size="sm" />}
          variant="ghost"
          clickAction={() => toggleExec(true)}
          className="claw-exec-toggle"
        />
      )}
      <ChatLayout
        emptyState={isEmpty ? greeting : undefined}
        composer={
          <div className="claw-composer">
            {error && <ErrorText>{error}</ErrorText>}
            {attachments.length > 0 && (
              <div className="claw-attach-row">
                {attachments.map((a, i) => (
                  <span key={a.path} className="claw-attach-chip">
                    <Icon icon={a.is_image ? ImageIcon : FileIcon} size="sm" color="secondary" />
                    {a.name}
                    <IconButton
                      label="Remove attachment"
                      icon={<Icon icon="close" size="xsm" />}
                      variant="ghost"
                      size="sm"
                      clickAction={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                    />
                  </span>
                ))}
              </div>
            )}
            <input
              ref={fileRef}
              type="file"
              multiple
              style={{ display: "none" }}
              onChange={(e) => void onPickFiles(e.target.files)}
            />
            <ChatComposer
              onSubmit={send}
              placeholder={isEmpty ? "How can Claw help you today?" : "Message Claw…"}
              isDisabled={false}
              footerActions={
                <div className="claw-composer-actions">
                  <Popover
                    label="Add"
                    placement="above"
                    alignment="start"
                    isOpen={plusOpen}
                    onOpenChange={(o) => {
                      setPlusOpen(o);
                      if (!o) setPlusView("root");
                    }}
                    width={280}
                    hasAutoFocus={false}
                    content={
                      <div className="claw-plus-menu">
                        {plusView === "root" && (
                          <>
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => {
                                setPlusOpen(false);
                                fileRef.current?.click();
                              }}
                            >
                              <Icon icon={Paperclip} size="sm" color="secondary" />
                              <span>Add files or photos</span>
                            </button>
                            <div className="claw-plus-divider" />
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => setPlusView("skills")}
                            >
                              <Icon icon={BookOpen} size="sm" color="secondary" />
                              <span>Skills</span>
                              <Icon icon={ChevronRight} size="sm" color="secondary" />
                            </button>
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => setPlusView("connectors")}
                            >
                              <Icon icon={Plug} size="sm" color="secondary" />
                              <span>Connectors</span>
                              <Icon icon={ChevronRight} size="sm" color="secondary" />
                            </button>
                          </>
                        )}
                        {plusView === "skills" && (
                          <>
                            <button
                              type="button"
                              className="claw-plus-item claw-plus-back"
                              onClick={() => setPlusView("root")}
                            >
                              <Icon icon={ArrowLeft} size="sm" color="secondary" />
                              <span>Skills</span>
                            </button>
                            <div className="claw-plus-divider" />
                            {skills.length === 0 ? (
                              <div className="claw-plus-empty">
                                <Text size="sm" color="secondary">
                                  No skills enabled.
                                </Text>
                              </div>
                            ) : (
                              skills.map((s) => (
                                <button
                                  key={s.id}
                                  type="button"
                                  className="claw-plus-item"
                                  onClick={() => insertMention(s.name)}
                                >
                                  <Icon icon={BookOpen} size="sm" color="secondary" />
                                  <span>{s.name}</span>
                                </button>
                              ))
                            )}
                          </>
                        )}
                        {plusView === "connectors" && (
                          <>
                            <button
                              type="button"
                              className="claw-plus-item claw-plus-back"
                              onClick={() => setPlusView("root")}
                            >
                              <Icon icon={ArrowLeft} size="sm" color="secondary" />
                              <span>Connectors</span>
                            </button>
                            <div className="claw-plus-divider" />
                            {connectors.length === 0 ? (
                              <div className="claw-plus-empty">
                                <Text size="sm" color="secondary">
                                  No connectors ready.
                                </Text>
                              </div>
                            ) : (
                              connectors.map((c) => (
                                <button
                                  key={c.id}
                                  type="button"
                                  className="claw-plus-item"
                                  onClick={() => insertMention(c.name)}
                                >
                                  <Icon icon={Plug} size="sm" color="secondary" />
                                  <span>{c.name}</span>
                                  <span className="claw-plus-count">{c.runtime.tools ?? 0}</span>
                                </button>
                              ))
                            )}
                          </>
                        )}
                      </div>
                    }
                  >
                    <IconButton
                      label={uploading ? "Uploading…" : "Add"}
                      icon={<Icon icon={Plus} size="sm" />}
                      variant="ghost"
                      isDisabled={uploading}
                    />
                  </Popover>
                  <Popover
                    label="Permission mode"
                    placement="above"
                    alignment="start"
                    isOpen={permOpen}
                    onOpenChange={setPermOpen}
                    width={340}
                    hasAutoFocus={false}
                    content={
                      <div className="claw-perm-menu">
                        {PERMISSION_OPTS.map((opt) => (
                          <button
                            key={opt.key}
                            type="button"
                            className="claw-perm-option"
                            onClick={() => changePermission(opt.key)}
                          >
                            <Icon icon={opt.icon} size="sm" color="secondary" />
                            <div className="claw-perm-option-main">
                              <span className="claw-perm-option-name">{opt.label}</span>
                              <span className="claw-perm-option-desc">{opt.desc}</span>
                            </div>
                            {permission === opt.key && <Icon icon={Check} size="sm" color="secondary" />}
                          </button>
                        ))}
                      </div>
                    }
                  >
                    <button type="button" className="claw-perm-trigger">
                      <Icon
                        icon={permission === "ask" ? ShieldCheck : ShieldAlert}
                        size="sm"
                        color="secondary"
                      />
                      <span className="claw-perm-trigger-label">
                        {permission === "ask" ? "Ask" : "Auto"}
                      </span>
                      <Icon icon={ChevronDown} size="xsm" color="secondary" />
                    </button>
                  </Popover>
                </div>
              }
              sendActions={
                models.length > 0 ? (
                  <Popover
                    label="Select model"
                    placement="above"
                    alignment="end"
                    isOpen={modelOpen}
                    onOpenChange={setModelOpen}
                    width={440}
                    hasAutoFocus={false}
                    content={
                      <div className="claw-model-menu">
                        <div className="claw-model-menu-title">
                          <Text size="sm" weight="semibold" color="secondary">
                            Select model
                          </Text>
                        </div>
                        <div className="claw-plus-divider" />
                        {models.map((m) => (
                          <button
                            key={m.model_id}
                            type="button"
                            className="claw-model-option"
                            onClick={() => {
                              setModel(m.model_id);
                              setModelOpen(false);
                            }}
                          >
                            <div className="claw-model-option-main">
                              <div className="claw-model-option-head">
                                <span className="claw-model-option-name">{m.label}</span>
                                <span className={`claw-cost claw-cost-${m.cost}`}>{COST_LABEL[m.cost]}</span>
                              </div>
                              {m.description && (
                                <span className="claw-model-option-desc">{m.description}</span>
                              )}
                            </div>
                            {m.model_id === model && (
                              <Icon icon={Check} size="sm" color="secondary" />
                            )}
                          </button>
                        ))}
                      </div>
                    }
                  >
                    <button type="button" className="claw-model-trigger">
                      <Icon icon={Box} size="sm" color="secondary" />
                      <span className="claw-model-trigger-label">
                        {models.find((m) => m.model_id === model)?.label ?? "Model"}
                      </span>
                      <Icon icon={ChevronDown} size="xsm" color="secondary" />
                    </button>
                  </Popover>
                ) : undefined
              }
            />
            {isEmpty && (
              <div className="claw-suggestions">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s.label}
                    type="button"
                    className="claw-suggestion-chip"
                    onClick={() => fillComposer(s.prompt)}
                  >
                    <Icon icon={s.icon} size="sm" color="secondary" />
                    <span>{s.label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        }
      >
        {isEmpty ? null : (
        <div className="claw-column">
          <ChatMessageList density="spacious">
            {items.map((item, i) =>
              // Tool steps render both inline (here) and in the right Execution diagram.
              item.kind === "tools" ? (
                <ChatToolCalls
                  key={i}
                  calls={item.calls.map((c, j) => ({
                    key: `${i}-${j}`,
                    name: c.name,
                    status: c.status,
                    target: c.argsPreview,
                    duration: c.duration,
                    errorMessage: c.status === "error" ? c.resultPreview : undefined,
                    resultDetail: c.resultPreview ? <Text size="sm">{c.resultPreview}</Text> : undefined,
                  }))}
                />
              ) : item.kind === "confirm" ? (
                <div key={i} className={`claw-confirm claw-confirm--${item.row.status}`}>
                  <div className="claw-confirm-head">
                    <Icon
                      icon={item.row.status === "pending" ? ShieldAlert : item.row.tool === "exec" ? Terminal : ShieldCheck}
                      size="sm"
                      color={item.row.status === "denied" ? "error" : item.row.status === "approved" ? "success" : "secondary"}
                    />
                    <Text size="sm" weight="semibold">
                      {item.row.status === "pending"
                        ? "Run this command in the sandbox?"
                        : item.row.status === "approved"
                          ? "Approved — command ran"
                          : "Declined"}
                    </Text>
                  </div>
                  {item.row.argsPreview && (
                    <pre className="claw-confirm-cmd">{item.row.argsPreview}</pre>
                  )}
                  {item.row.status === "pending" && (
                    <div className="claw-confirm-actions">
                      <Button
                        label="Decline"
                        variant="secondary"
                        size="sm"
                        clickAction={() => sendDecision(item.row.requestId, false)}
                      >
                        Decline
                      </Button>
                      <Button
                        label="Approve and run"
                        variant="primary"
                        size="sm"
                        clickAction={() => sendDecision(item.row.requestId, true)}
                      >
                        Approve &amp; run
                      </Button>
                    </div>
                  )}
                </div>
              ) : (
                <ChatMessage key={i} sender={item.role === "user" ? "user" : "assistant"}>
                  {item.role === "assistant" ? (
                    <>
                      <ChatMessageBubble variant="ghost">
                        <Markdown>{item.content}</Markdown>
                      </ChatMessageBubble>
                      {sessionId && item.artifacts && item.artifacts.length > 0 && (
                        <div className="claw-artifacts">
                          {item.artifacts.map((p) => {
                            const href = fileUrl(sessionId, p);
                            // Show images (e.g. a generated chart) inline; keep
                            // everything else as an openable chip.
                            if (/\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(p)) {
                              return (
                                <a
                                  key={p}
                                  className="claw-artifact-image"
                                  href={href}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  title={`Open ${p}`}
                                >
                                  <img src={href} alt={p.split("/").pop()} loading="lazy" />
                                </a>
                              );
                            }
                            return (
                              <a
                                key={p}
                                className="claw-artifact-chip"
                                href={href}
                                target="_blank"
                                rel="noopener noreferrer"
                                title={`Open ${p}`}
                              >
                                <Icon icon={FileIcon} size="sm" color="secondary" />
                                <span className="claw-artifact-name">{p.split("/").pop()}</span>
                                <Icon icon={ExternalLink} size="xsm" color="secondary" />
                              </a>
                            );
                          })}
                        </div>
                      )}
                      <ChatMessageMetadata
                        footer={
                          <div className="claw-feedback">
                            <IconButton
                              label={copied[i] ? "Copied" : "Copy response"}
                              icon={<Icon icon={copied[i] ? Check : Copy} size="sm" color={copied[i] ? "success" : "secondary"} />}
                              variant="ghost"
                              size="sm"
                              clickAction={() => copyMessage(i, item.content)}
                            />
                            <IconButton
                              label="Good response"
                              icon={
                                <Icon icon={ThumbsUp} size="sm" color={feedback[i] === "up" ? "success" : "secondary"} />
                              }
                              variant="ghost"
                              size="sm"
                              clickAction={() => rate(i, item.content, "up")}
                            />
                            <IconButton
                              label="Bad response"
                              icon={
                                <Icon icon={ThumbsDown} size="sm" color={feedback[i] === "down" ? "error" : "secondary"} />
                              }
                              variant="ghost"
                              size="sm"
                              clickAction={() => rate(i, item.content, "down")}
                            />
                          </div>
                        }
                      />
                    </>
                  ) : (
                    <>
                      <ChatMessageBubble>{item.content}</ChatMessageBubble>
                      <ChatMessageMetadata
                        footer={
                          <div className="claw-feedback">
                            <IconButton
                              label={copied[i] ? "Copied" : "Copy message"}
                              icon={<Icon icon={copied[i] ? Check : Copy} size="sm" color={copied[i] ? "success" : "secondary"} />}
                              variant="ghost"
                              size="sm"
                              clickAction={() => copyMessage(i, item.content)}
                            />
                          </div>
                        }
                      />
                    </>
                  )}
                </ChatMessage>
              ),
            )}
            {streaming && (
              <ChatMessage sender="assistant">
                <ChatMessageBubble variant="ghost">
                  <Markdown>{streaming}</Markdown>
                  <span className="claw-cursor">▍</span>
                </ChatMessageBubble>
              </ChatMessage>
            )}
            {busy && !streaming && (
              <ChatMessage sender="assistant">
                <ChatMessageBubble variant="ghost">
                  <span className="claw-thinking">
                    <Spinner size="sm" shade="subtle" /> Thinking…
                  </span>
                </ChatMessageBubble>
              </ChatMessage>
            )}
          </ChatMessageList>
        </div>
        )}
      </ChatLayout>
    </div>
      {!isEmpty && execOpen && (
        <ExecutionPanel steps={execSteps} running={busy} onClose={() => toggleExec(false)} />
      )}
    </div>
  );
}
