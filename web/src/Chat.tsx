import {
  ChatComposer,
  ChatComposerInput,
  type ChatComposerInputHandle,
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
import { Lightbox } from "@astryxdesign/core/Lightbox";
import { Popover } from "@astryxdesign/core/Popover";
import { Spinner } from "@astryxdesign/core/Spinner";
import { Text } from "@astryxdesign/core/Text";
import { useToast } from "@astryxdesign/core/Toast";
import {
  ArrowLeft,
  BarChart3,
  BookOpen,
  Box,
  Check,
  ChevronDown,
  ChevronRight,
  Code,
  Copy,
  ExternalLink,
  File as FileIcon,
  GitBranch,
  Image as ImageIcon,
  Library,
  Mic,
  PanelRight,
  Paperclip,
  PenLine,
  Plug,
  Plus,
  Share2,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Square,
  Terminal,
  ThumbsDown,
  ThumbsUp,
  Users,
  Volume2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorText } from "./ErrorText";
import { ExecutionPanel } from "./ExecutionPanel";
import {
  AgentEvent,
  ApiError,
  AttachmentRef,
  ConnectorInfo,
  KnowledgeBase,
  ModelOption,
  SkillInfo,
  WorkingPlan,
  api,
  fileUrl,
  openChatSocket,
} from "./api";
import { SoftnixLogo } from "./Logo";
import { useT } from "./branding";
import { sanitizeModelMarkdown, stripMarkdownForSpeech } from "./markdown";

// Copy text to the clipboard, falling back to execCommand for non-secure
// contexts (plain-http LAN/Tailscale IPs) where navigator.clipboard is absent.
async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to legacy path */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

const COST_LABEL: Record<string, string> = {
  low: "Low cost",
  medium: "Medium cost",
  high: "High cost",
  very_high: "Very high cost",
};

// Starter chips shown on the empty landing screen, à la claude.ai — tailored to
// Thai enterprise users (this product's actual audience) rather than generic
// consumer prompts. Clicking a category opens a panel of concrete, ready-to-use
// prompts for that category; clicking one seeds the composer with the full
// prompt and refocuses so the user can edit before sending.
interface SuggestionCategory {
  key: string;
  label: string;
  icon: typeof Code;
  prompts: string[];
}

const SUGGESTIONS: SuggestionCategory[] = [
  {
    key: "write",
    label: "เขียนงาน",
    icon: PenLine,
    prompts: [
      "ร่างอีเมลถึงลูกค้าเพื่อแจ้งความคืบหน้าของโครงการอย่างสุภาพและได้ใจความ",
      "สรุปรายงานการประชุมจากบันทึกคร่าวๆ ให้เป็นข้อความทางการ",
      "เขียนประกาศ/บันทึกข้อความแจ้งพนักงานภายในองค์กร",
      "ร่างคำอธิบายลักษณะงาน (Job Description) สำหรับตำแหน่งที่เปิดรับ",
      "เขียนโพสต์โซเชียลมีเดียประชาสัมพันธ์กิจกรรมของบริษัท",
    ],
  },
  {
    key: "analyze",
    label: "วิเคราะห์ข้อมูล",
    icon: BarChart3,
    prompts: [
      "สรุปผลประกอบการจากตัวเลขที่ฉันมีให้ในรูปแบบเข้าใจง่าย",
      "วิเคราะห์จุดแข็งจุดอ่อนของคู่แข่งจากข้อมูลที่ฉันให้",
      "สรุปผลแบบสำรวจความพึงพอใจของลูกค้าหรือพนักงาน",
      "ช่วยตีความและสรุปข้อมูลจากไฟล์ Excel/CSV ที่แนบมา",
      "คาดการณ์แนวโน้มยอดขายจากข้อมูลย้อนหลัง",
    ],
  },
  {
    key: "hr",
    label: "งานบุคคล",
    icon: Users,
    prompts: [
      "ร่างประกาศรับสมัครงานให้น่าสนใจและตรงกลุ่มเป้าหมาย",
      "ออกแบบแผนการอบรมปฐมนิเทศพนักงานใหม่ 1 สัปดาห์",
      "ร่างแบบฟอร์มประเมินผลการปฏิบัติงานประจำปี",
      "เขียนคำถามสัมภาษณ์งานสำหรับตำแหน่งที่ต้องการ",
      "สรุปนโยบายหรือระเบียบบริษัทให้พนักงานเข้าใจง่าย",
    ],
  },
  {
    key: "code",
    label: "ระบบ/โค้ด",
    icon: Code,
    prompts: [
      "เขียนสคริปต์อัตโนมัติสำหรับงานที่ต้องทำซ้ำๆ ทุกวัน",
      "ช่วยเขียนสูตร Excel/Google Sheets ที่ซับซ้อน",
      "เขียนคำสั่ง SQL สำหรับดึงรายงานจากฐานข้อมูล",
      "ตรวจสอบและช่วยแก้บั๊กในโค้ดที่มีปัญหา",
      "อธิบายโค้ดที่คนอื่นเขียนไว้ให้เข้าใจง่ายขึ้น",
    ],
  },
  {
    key: "pick",
    label: "Claw แนะนำ",
    icon: Sparkles,
    prompts: [
      "แนะนำวิธีจัดลำดับความสำคัญของงานประจำวันให้มีประสิทธิภาพขึ้น",
      "ช่วยคิดหัวข้อและวาระการประชุมทีมประจำสัปดาห์",
      "แนะนำวิธีเขียนอีเมลปฏิเสธคำขอของลูกค้าอย่างสุภาพ",
      "สรุปข่าวธุรกิจและเทคโนโลยีที่น่าสนใจวันนี้",
      "แนะนำแนวทางลดขั้นตอนการทำงานที่ซ้ำซ้อนในทีม",
    ],
  },
];

interface SubStep {
  key: string;
  label: string;
  status: "running" | "complete" | "error";
}

interface ToolCallRow {
  name: string;
  status: "running" | "complete" | "error";
  argsPreview?: string;
  resultPreview?: string;
  startedAt: number;
  duration?: string;
  // Live sub-steps streamed from a long tool (e.g. workflow), + step counter.
  substeps?: SubStep[];
  stepIndex?: number;
  stepTotal?: number;
}

type PermissionMode = "ask" | "auto";

interface ConfirmRow {
  requestId: string;
  tool: string;
  argsPreview?: string;
  status: "pending" | "approved" | "denied";
}

// Confirmation-card copy per gated tool (Ask mode). The default covers any
// future gated tool without a code change.
const CONFIRM_COPY: Record<string, { pending: string; approved: string }> = {
  exec: { pending: "Run this command in the sandbox?", approved: "Approved — command ran" },
  workflow: { pending: "Start this multi-step workflow?", approved: "Approved — workflow started" },
  spawn: { pending: "Run this delegated task?", approved: "Approved — task started" },
};
function confirmTitle(tool: string, status: ConfirmRow["status"]): string {
  const copy = CONFIRM_COPY[tool] ?? { pending: "Run this action?", approved: "Approved" };
  if (status === "pending") return copy.pending;
  if (status === "approved") return copy.approved;
  return "Declined";
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
    desc: "Ask before sandbox commands and multi-step workflows",
    icon: ShieldCheck,
  },
  {
    key: "auto",
    label: "Auto",
    desc: "Run everything automatically without asking",
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
  /** Jump to the given Settings section (e.g. from a "Manage" link in the "+"
   * menu or the model picker) so Skills/Connectors/Knowledge/My Models stay one
   * click away from where the user actually uses them, instead of a dead end
   * when the list is empty. */
  onOpenSettings?: (section: "skills" | "connectors" | "knowledge" | "models") => void;
}

export function Chat({
  sessionId,
  userName,
  onFirstMessage,
  onRequireSession,
  onActivity,
  running,
  initialModel,
  onOpenSettings,
}: ChatProps) {
  const t = useT();
  const [items, setItems] = useState<TranscriptItem[]>([]);
  const [streaming, setStreaming] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);
  const [uploading, setUploading] = useState(false);
  const [feedback, setFeedback] = useState<Record<number, "up" | "down">>({});
  const [copied, setCopied] = useState<Record<number, boolean>>({});
  const [sharing, setSharing] = useState(false);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [defaultModel, setDefaultModel] = useState<string>("");
  const [model, setModel] = useState<string>(initialModel ?? "");
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [knowledge, setKnowledge] = useState<KnowledgeBase[]>([]);
  const [plusOpen, setPlusOpen] = useState(false);
  const [plusView, setPlusView] = useState<"root" | "skills" | "connectors" | "knowledge">("root");
  // Text-to-image (composer "Create image" mode): separate from chat, a
  // one-shot REST call. imageMode swaps the SAME composer shell into an
  // image-prompt input rather than opening a separate floating panel.
  const [imageModels, setImageModels] = useState<ModelOption[]>([]);
  const [imageModel, setImageModel] = useState<string>("");
  const [imagePrompt, setImagePrompt] = useState("");
  const [imageBusy, setImageBusy] = useState(false);
  const [imageMode, setImageMode] = useState(false);
  const [imageModelOpen, setImageModelOpen] = useState(false);
  // Click-to-enlarge for artifact images (generated or agent-produced).
  const [lightbox, setLightbox] = useState<{ src: string; alt: string } | null>(null);
  const [modelOpen, setModelOpen] = useState(false);
  // Which starter-suggestion category panel is expanded (null = show the chip
  // row). Reset whenever the session changes so a stale panel doesn't linger.
  const [suggestionCategory, setSuggestionCategory] = useState<string | null>(null);
  // Permission mode: "ask" pauses for user approval before unsafe sandbox
  // commands; "auto" runs them without asking. Remembered across sessions.
  const [permission, setPermission] = useState<PermissionMode>(
    () => (localStorage.getItem("claw_permission_mode") === "auto" ? "auto" : "ask"),
  );
  const [permOpen, setPermOpen] = useState(false);
  // Speech-to-text: whether the backend has Groq Whisper configured, and the
  // mic's live state (idle → recording → transcribing).
  const [sttEnabled, setSttEnabled] = useState(false);
  const [micState, setMicState] = useState<"idle" | "recording" | "transcribing">("idle");
  // Text-to-speech: whether the backend has an OpenAI-compatible provider
  // configured for the per-message "read aloud" speaker button, plus each
  // message's own loading/playing state and the audio element currently playing.
  const [ttsEnabled, setTtsEnabled] = useState(false);
  const [speaking, setSpeaking] = useState<Record<number, "loading" | "playing" | undefined>>({});
  const speakingAudioRef = useRef<HTMLAudioElement | null>(null);
  const speakingIndexRef = useRef<number | null>(null);
  const speakingUrlRef = useRef<string | null>(null);
  // Right execution panel: remember the user's explicit show/hide choice; a tool
  // starting auto-opens it transiently without overwriting that saved default.
  const [execOpen, setExecOpen] = useState(() => localStorage.getItem("claw_exec_open") === "1");
  // The agent's live working plan (from plan_updated events), shown pinned atop
  // the Execution panel. Null until the agent sets one this session.
  const [plan, setPlan] = useState<WorkingPlan | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  // Mirrors the `sessionId` prop for use inside async callbacks (e.g.
  // runGeneratedImage) — lets them tell, after an await, whether the user has
  // since switched to a different session so they don't apply a stale
  // result/error/busy-state to the wrong session's transcript.
  const sessionIdRef = useRef(sessionId);
  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);
  const fileRef = useRef<HTMLInputElement | null>(null);
  // Imperative handle for the composer's contentEditable — lets us insert a
  // styled mention chip (skill/connector/knowledge) instead of raw "@name " text.
  const composerHandleRef = useRef<ChatComposerInputHandle>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const sentCountRef = useRef(0);
  // A message typed on the draft landing, held until the freshly-created
  // session's socket opens, then flushed. Lets the composer create the session
  // lazily (like claude.ai) instead of on page load.
  // Captures the model chosen at send-click too, so the flush uses exactly what
  // the user picked even if `model` state changes during the create handoff.
  const pendingRef = useRef<{ content: string; atts: AttachmentRef[]; model: string } | null>(null);
  const rawSendRef = useRef<(content: string, atts: AttachmentRef[], modelOverride?: string) => void>(
    () => {},
  );
  // Same idea as pendingRef, for a "Create image" prompt submitted from the
  // draft landing: held until the freshly-created session's id lands, then
  // run by the effect below (ordered after the per-session reset effect so
  // it never gets its optimistic bubble wiped by that reset).
  const pendingImageRef = useRef<string | null>(null);
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
    // Switching sessions must not leave the previous session's "read aloud"
    // audio playing (or its per-index state) bleeding into the new one.
    // Deliberately not in the deps array: stopSpeaking has a stable identity
    // (empty-dep useCallback further down), and listing it here would try to
    // evaluate an as-yet-undeclared const at this point in the render.
    stopSpeaking();
    setItems([]);
    setStreaming("");
    setError("");
    setAttachments([]);
    setFeedback({});
    setPlan(null); // plan is per-session; don't leak one chat's plan into another
    setSuggestionCategory(null);
    // "Create image" mode is per-session UI state too — without this, opening
    // it in one chat and switching to another leaves the composer stuck in
    // image-prompt mode (and any leftover prompt text) in the new session.
    setImageMode(false);
    setImagePrompt("");
    setImageBusy(false);
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
    // A pending image prompt is the same "fresh, empty session" situation,
    // but it's a REST call with no socket turn to eventually clear `busy` —
    // only skip the fetch (so it can't clobber the optimistic user bubble
    // pushed by the pendingImageRef effect below), don't fold it into the
    // handoff spinner.
    const skipMessageFetch = handoff || pendingImageRef.current !== null;
    setBusy(handoff || running === true);
    let cancelled = false;
    let socket: WebSocket | null = null;
    const onOpen = () => {
      const pending = pendingRef.current;
      if (pending) {
        pendingRef.current = null;
        rawSendRef.current(pending.content, pending.atts, pending.model);
      }
    };
    const onMessage = (raw: MessageEvent) => {
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
                      // Any sub-steps still spinning (e.g. synthesize) are done
                      // once the whole tool finishes.
                      substeps: c.substeps?.map((s) =>
                        s.status === "running"
                          ? { ...s, status: (event.is_error ? "error" : "complete") as SubStep["status"] }
                          : s,
                      ),
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
        case "tool_progress":
          // Live sub-step of a long tool (workflow): attach a checklist to the
          // most recent still-running tool call.
          setItems((prev) => {
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
              const stageKey =
                event.stage === "step" ? `step-${event.index}` : event.stage || "stage";
              const st: SubStep["status"] =
                event.status === "done" ? "complete" : event.status === "error" ? "error" : "running";
              const calls = it.calls.map((c, k) => {
                if (k !== ri) return c;
                const subs = [...(c.substeps ?? [])];
                const at = subs.findIndex((s) => s.key === stageKey);
                const row: SubStep = { key: stageKey, label: event.label ?? "", status: st };
                if (at >= 0) subs[at] = row;
                else subs.push(row);
                return {
                  ...c,
                  substeps: subs,
                  stepIndex: event.index || c.stepIndex,
                  stepTotal: event.total || c.stepTotal,
                };
              });
              const next = [...prev];
              next[gi] = { kind: "tools", calls };
              return next;
            }
            return prev;
          });
          break;
        case "plan_updated":
          // The agent revised its working plan — show it pinned in the panel.
          setExecOpen(true);
          setPlan({ goal: event.goal ?? "", steps: event.steps ?? [] });
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
    const onClose = (ev: CloseEvent) => {
      if (ev.code === 4401) setError("Authentication failed — check your token and email.");
    };

    const openSocket = () => {
      if (cancelled) return;
      socket = openChatSocket(sessionId);
      socketRef.current = socket;
      socket.onopen = onOpen;
      socket.onmessage = onMessage;
      socket.onclose = onClose;
    };

    if (skipMessageFetch) {
      // Fresh draft session — connect immediately to flush the pending message.
      openSocket();
    } else {
      // Seed the persisted transcript FIRST, then open the socket. The bus
      // replays the current turn's live events on connect; connecting only
      // after listMessages resolves keeps those replayed tool/confirm/sub-step
      // cards from being clobbered by a late message-list response — the bug
      // where returning to a still-processing session dropped the live view.
      api
        .listMessages(sessionId)
        .then((msgs) => {
          if (cancelled) return;
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
        .catch((e) => {
          if (!cancelled) setError(String(e));
        })
        .finally(() => openSocket());
    }

    return () => {
      cancelled = true;
      const s = socket;
      if (!s) return;
      // Closing a socket still mid-handshake (React StrictMode remount) logs a
      // spurious "closed before the connection is established" warning; defer.
      if (s.readyState === WebSocket.CONNECTING) {
        s.addEventListener("open", () => s.close());
      } else {
        s.close();
      }
    };
  }, [sessionId]);

  // Load the model picker options + the admin-configured default once.
  useEffect(() => {
    api
      .listModels()
      .then((r) => {
        setModels(r.models);
        setDefaultModel(r.default || "");
      })
      .catch(() => setModels([]));
  }, []);

  // The picker must always reflect the ACTIVE session's model — its sticky
  // per-session model, else the global default. Because <Chat> isn't remounted
  // when you switch sessions (no key), this has to re-sync on sessionId change;
  // otherwise the previous session's selection leaks / the label reverts to the
  // default and misleads the user about what's actually running. Keying on
  // initialModel too means a just-persisted model (after send) stays in sync,
  // while an unsent in-session pick is never clobbered by the background poll
  // (initialModel only changes once the choice is persisted).
  const prevSessionIdRef = useRef<string | null>(sessionId);
  useEffect(() => {
    const prev = prevSessionIdRef.current;
    prevSessionIdRef.current = sessionId;
    // Draft (null) → its own freshly-created session: the user's just-picked
    // model must carry over. Resetting here would clobber it (the new session's
    // persisted model isn't known yet, so we'd wrongly fall back to default —
    // and since the queued first message is flushed reading `model`, the turn
    // would even RUN on the wrong model). Keep the current selection.
    if (prev === null && sessionId !== null) return;
    setModel(initialModel || defaultModel || "");
  }, [sessionId, initialModel, defaultModel]);

  // Show the composer mic only when the backend has speech-to-text configured.
  useEffect(() => {
    api
      .features()
      .then((f) => {
        setSttEnabled(f.speech_to_text);
        setTtsEnabled(f.text_to_speech);
      })
      .catch(() => {
        setSttEnabled(false);
        setTtsEnabled(false);
      });
  }, []);

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
    // Knowledge bases with at least one document (searchable).
    api
      .listKnowledge()
      .then((k) => setKnowledge(k.filter((x) => x.docs > 0)))
      .catch(() => setKnowledge([]));
    // Text-to-image models for the "+ Image" picker.
    api
      .listImageModels()
      .then((r) => {
        setImageModels(r.models);
        setImageModel((cur) => cur || r.models[0]?.model_id || "");
      })
      .catch(() => setImageModels([]));
  }, [sessionId, running]);

  // Insert an "@mention" for a skill/connector at the caret, then refocus so the
  // user can keep typing. The agent already sees enabled skills + connected
  // connector tools in context, so the mention nudges it to use that one.

  // Sync the composer's React state after mutating its contentEditable DOM
  // directly (insertToken / chip removal) — neither fires a native input
  // event, so nudge it the same way a real keystroke would.
  const emitComposerInput = useCallback(() => {
    const editable = document.querySelector<HTMLElement>(
      ".astryx-chat-composer-input [contenteditable]",
    );
    editable?.dispatchEvent(new InputEvent("input", { bubbles: true }));
  }, []);

  // Remove a mention chip the user changed their mind about: drop its token
  // span (and the trailing NBSP the library inserts after it), then re-sync.
  const removeMentionChip = useCallback(
    (target: HTMLElement) => {
      const span = target.closest<HTMLElement>("[data-astryx-token]");
      if (!span) return;
      const next = span.nextSibling;
      if (next?.nodeType === Node.TEXT_NODE && next.textContent === " ") next.remove();
      span.remove();
      emitComposerInput();
    },
    [emitComposerInput],
  );

  const insertMention = useCallback(
    (name: string, icon: typeof BookOpen) => {
      const handle = composerHandleRef.current;
      if (handle) {
        handle.focus();
        // Insert as a styled chip (not raw "@name " text) — the submitted value
        // stays "@name " under the hood so the agent still sees the same mention
        // cue, but the composer shows a compact pill with a remove (×) control
        // in case the user changes their mind.
        handle.insertToken({
          value: `@${name} `,
          render: () => (
            <span className="claw-mention-chip">
              <Icon icon={icon} size="xsm" />
              <span className="claw-mention-chip-label">{name}</span>
              <button
                type="button"
                className="claw-mention-chip-remove"
                aria-label={`Remove ${name}`}
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  removeMentionChip(e.currentTarget);
                }}
              >
                <Icon icon={X} size="xsm" />
              </button>
            </span>
          ),
        });
        emitComposerInput();
      }
      setPlusOpen(false);
      setPlusView("root");
    },
    [emitComposerInput, removeMentionChip],
  );

  // Jump from the "+" menu straight to the matching Settings section — keeps
  // the "where do I manage this?" answer one click away instead of a dead end.
  const manageSettings = useCallback(
    (section: "skills" | "connectors" | "knowledge") => {
      setPlusOpen(false);
      setPlusView("root");
      onOpenSettings?.(section);
    },
    [onOpenSettings],
  );

  // Same idea for the model picker's "Manage my models" footer link.
  const manageModelSettings = useCallback(() => {
    setModelOpen(false);
    onOpenSettings?.("models");
  }, [onOpenSettings]);

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

  // Drop transcribed speech in at the caret (or end), adding a leading space so
  // it doesn't glue onto existing text; keeps the caret after it for more typing.
  const appendTranscript = useCallback((text: string) => {
    const editable = document.querySelector<HTMLElement>(
      ".astryx-chat-composer-input [contenteditable]",
    );
    if (!editable) return;
    editable.focus();
    const existing = editable.textContent ?? "";
    const prefix = existing && !/\s$/.test(existing) ? " " : "";
    document.execCommand("insertText", false, prefix + text);
  }, []);

  // Mic toggle: first click starts recording from the mic, second click stops
  // → uploads the audio to /api/transcribe (Groq Whisper) → inserts the text.
  const toggleMic = useCallback(async () => {
    // Stop an in-progress recording; the recorder's onstop handles transcription.
    if (micState === "recording") {
      mediaRecorderRef.current?.stop();
      return;
    }
    if (micState === "transcribing") return;
    if (!navigator.mediaDevices?.getUserMedia) {
      toast({ body: "Microphone not available in this browser", type: "error" });
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      mediaRecorderRef.current = recorder;
      audioChunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunksRef.current.push(e.data);
      };
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop()); // release the mic
        const blob = new Blob(audioChunksRef.current, { type: recorder.mimeType || "audio/webm" });
        if (blob.size === 0) {
          setMicState("idle");
          return;
        }
        setMicState("transcribing");
        try {
          const ext = (recorder.mimeType || "audio/webm").includes("ogg") ? "ogg" : "webm";
          const text = await api.transcribe(blob, `speech.${ext}`);
          if (text) appendTranscript(text);
          else toast({ body: "Didn't catch that — try again", type: "info" });
        } catch (e) {
          toast({ body: `Transcription failed: ${String(e)}`, type: "error" });
        } finally {
          setMicState("idle");
        }
      };
      recorder.start();
      setMicState("recording");
    } catch {
      toast({ body: "Microphone permission denied", type: "error" });
      setMicState("idle");
    }
  }, [micState, appendTranscript, toast]);

  // Push a message over the (open) socket and reflect it locally. Assumes a
  // connected session — the draft path in `send` routes through here only
  // after the new session's socket opens.
  const rawSend = useCallback(
    (content: string, atts: AttachmentRef[], modelOverride?: string) => {
      if (socketRef.current?.readyState !== WebSocket.OPEN) return;
      if (sentCountRef.current === 0 && content) onFirstMessage?.(content);
      sentCountRef.current += 1;
      socketRef.current.send(
        JSON.stringify({
          content,
          attachments: atts.map((a) => a.path),
          model: (modelOverride ?? model) || undefined,
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
        pendingRef.current = { content, atts, model };
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
    [attachments, sessionId, onRequireSession, rawSend, model],
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

  // Text-to-image generation: a one-shot REST call (no socket, no agent loop).
  // Shows the prompt right away (like a normal sent message), then the
  // "Generating image…" bubble covers the wait until the artifact lands.
  const runGeneratedImage = useCallback(
    async (sid: string, prompt: string) => {
      let userIndex = -1;
      setItems((prev) => {
        userIndex = prev.length;
        return [...prev, { kind: "message", role: "user", content: prompt }];
      });
      // The REST call has no socket/turn to tie it to a session — if the user
      // switches to a different chat before it resolves, `sid` no longer
      // matches what's on screen. Check before touching `items`/`error`/
      // `imageBusy` so a stale result/failure never lands in the wrong
      // session's transcript (those setters aren't otherwise session-scoped).
      const stillOnThisSession = () => sessionIdRef.current === sid;
      try {
        const res = await api.generateImage(sid, imageModel, prompt);
        if (!stillOnThisSession()) return;
        // The backend may have masked the prompt (guardrail/PII policy) —
        // `res.prompt` is what's actually persisted, so reconcile the
        // optimistic bubble (and the session auto-title) to match instead of
        // showing/storing the raw text the policy was meant to strip. Titling
        // only on success also avoids naming a session after a prompt that
        // never produced anything.
        if (sentCountRef.current === 0) onFirstMessage?.(res.prompt);
        setItems((prev) => {
          const next = [...prev];
          const userItem = next[userIndex];
          if (userItem?.kind === "message" && userItem.role === "user") {
            next[userIndex] = { ...userItem, content: res.prompt };
          }
          next.push({ kind: "message", role: "assistant", content: "", artifacts: [res.path] });
          return next;
        });
        sentCountRef.current += 1;
      } catch (e) {
        if (!stillOnThisSession()) return;
        // Nothing was persisted server-side on failure — drop the optimistic
        // bubble instead of leaving a prompt with no response in the
        // transcript (it would vanish on reload anyway).
        setItems((prev) => {
          const userItem = prev[userIndex];
          if (userItem?.kind === "message" && userItem.role === "user" && userItem.content === prompt) {
            return [...prev.slice(0, userIndex), ...prev.slice(userIndex + 1)];
          }
          return prev;
        });
        setError(`Image generation failed: ${e instanceof ApiError ? e.message : String(e)}`);
      } finally {
        if (stillOnThisSession()) setImageBusy(false);
      }
    },
    [imageModel, onFirstMessage],
  );

  const generateImage = useCallback(
    async (value: string) => {
      const prompt = value.trim();
      if (!prompt || !imageModel || imageBusy) return;
      setImageBusy(true);
      setError("");
      if (sessionId) {
        void runGeneratedImage(sessionId, prompt);
        return;
      }
      // Draft landing: materialize a session first (like attachments). The
      // per-session reset effect (keyed on sessionId) clears `items` the
      // moment the id swaps in from null — pushing the prompt bubble here
      // would race that reset. Instead queue it and let the effect below
      // (ordered after the reset effect) run it once sessionId settles,
      // mirroring how the chat-text path defers via pendingRef + socket onopen.
      pendingImageRef.current = prompt;
      onRequireSession?.().catch((e) => {
        pendingImageRef.current = null;
        setImageBusy(false);
        setError(`Image generation failed: ${e instanceof ApiError ? e.message : String(e)}`);
      });
    },
    [imageModel, imageBusy, sessionId, onRequireSession, runGeneratedImage],
  );

  // Runs after the per-session reset effect (defined earlier, so it commits
  // first within the same pass) — picks up a prompt queued by generateImage
  // while materializing a draft session.
  useEffect(() => {
    if (sessionId && pendingImageRef.current) {
      const prompt = pendingImageRef.current;
      pendingImageRef.current = null;
      void runGeneratedImage(sessionId, prompt);
    }
  }, [sessionId, runGeneratedImage]);

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

  // Stop whatever message is currently being read aloud, if any.
  const stopSpeaking = useCallback(() => {
    speakingAudioRef.current?.pause();
    speakingAudioRef.current = null;
    if (speakingUrlRef.current) {
      URL.revokeObjectURL(speakingUrlRef.current);
      speakingUrlRef.current = null;
    }
    const idx = speakingIndexRef.current;
    speakingIndexRef.current = null;
    if (idx !== null) setSpeaking((prev) => ({ ...prev, [idx]: undefined }));
  }, []);

  useEffect(() => stopSpeaking, [stopSpeaking]);

  // Read one assistant message aloud (text-to-speech). Clicking the button on
  // the message currently playing stops it instead of restarting it.
  const speakMessage = useCallback(
    async (index: number, content: string) => {
      if (speakingIndexRef.current === index) {
        stopSpeaking();
        return;
      }
      stopSpeaking();
      speakingIndexRef.current = index;
      setSpeaking((prev) => ({ ...prev, [index]: "loading" }));
      try {
        const plain = stripMarkdownForSpeech(content);
        const blob = await api.speak(plain);
        if (speakingIndexRef.current !== index) return; // superseded/stopped
        const url = URL.createObjectURL(blob);
        speakingUrlRef.current = url;
        const audioEl = new Audio(url);
        speakingAudioRef.current = audioEl;
        setSpeaking((prev) => ({ ...prev, [index]: "playing" }));
        audioEl.onended = () => {
          if (speakingUrlRef.current === url) {
            URL.revokeObjectURL(url);
            speakingUrlRef.current = null;
          }
          if (speakingIndexRef.current === index) {
            speakingIndexRef.current = null;
            setSpeaking((prev) => ({ ...prev, [index]: undefined }));
          }
        };
        await audioEl.play();
      } catch {
        toast({ body: "Couldn't read this response aloud", type: "error" });
        if (speakingIndexRef.current === index) {
          speakingIndexRef.current = null;
          setSpeaking((prev) => ({ ...prev, [index]: undefined }));
        }
      }
    },
    [stopSpeaking, toast],
  );

  // Share one answer (plus its preceding question, for context) as a public,
  // expiring link. Creates an immutable server-side snapshot, then copies the
  // capability URL to the clipboard and confirms with a "Link copied" toast.
  const shareAnswer = useCallback(
    async (index: number) => {
      if (!sessionId || sharing) return;
      const answer = items[index];
      if (!answer || answer.kind !== "message" || answer.role !== "assistant") return;
      setSharing(true);
      try {
        const msgs: { role: string; content: string; artifacts?: string[] }[] = [];
        for (let j = index - 1; j >= 0; j--) {
          const it = items[j];
          if (it.kind === "message" && it.role === "user") {
            msgs.push({ role: "user", content: it.content });
            break;
          }
        }
        msgs.push({ role: "assistant", content: answer.content, artifacts: answer.artifacts });
        const res = await api.createShare(sessionId, { messages: msgs });
        // Build the URL from the current origin so the recipient hits the same
        // host the sharer is on (works on localhost, LAN IP, or a real domain).
        const url = `${window.location.origin}${res.path}`;
        const ok = await writeClipboard(url);
        toast(
          ok
            ? { body: "Link copied", type: "info", autoHideDuration: 2500 }
            : { body: `Share link: ${url}`, type: "info", autoHideDuration: 10000 },
        );
      } catch {
        toast({ body: "Couldn't create the share link", type: "error" });
      } finally {
        setSharing(false);
      }
    },
    [sessionId, items, sharing, toast],
  );

  const isEmpty = items.length === 0 && !streaming && !busy && !imageBusy;

  // Flatten every tool-call group (in order) into the execution timeline.
  const execSteps = items.flatMap((it) => (it.kind === "tools" ? it.calls : []));

  const toggleExec = (v: boolean) => {
    setExecOpen(v);
    localStorage.setItem("claw_exec_open", v ? "1" : "0");
  };

  const greeting = (
    <div className="claw-greeting">
      <div className="claw-greeting-title">
        <SoftnixLogo height={44} slot="chat" />
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
              onSubmit={imageMode ? (value) => void generateImage(value) : send}
              value={imageMode ? imagePrompt : undefined}
              onChange={imageMode ? setImagePrompt : undefined}
              placeholder={
                imageMode
                  ? "Describe the image to generate…"
                  : isEmpty
                    ? t("chat.composerEmpty")
                    : t("chat.composerPlaceholder")
              }
              isDisabled={imageMode && imageBusy}
              input={<ChatComposerInput handleRef={composerHandleRef} />}
              footerActions={
                imageMode ? (
                  <div className="claw-composer-actions">
                    <button
                      type="button"
                      className="claw-image-mode-back"
                      onClick={() => {
                        setImageMode(false);
                        setImagePrompt("");
                      }}
                    >
                      <Icon icon={ArrowLeft} size="sm" color="secondary" />
                      <span className="claw-image-mode-back-label">Back to chat</span>
                    </button>
                  </div>
                ) : (
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
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => setPlusView("knowledge")}
                            >
                              <Icon icon={Library} size="sm" color="secondary" />
                              <span>Knowledge</span>
                              <Icon icon={ChevronRight} size="sm" color="secondary" />
                            </button>
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => {
                                if (imageModels.length === 0) {
                                  toast({
                                    body: "Image generation isn't included in your current plan. Ask an admin to upgrade your plan or enable an image model in the Control Plane.",
                                    type: "info",
                                  });
                                  return;
                                }
                                setImageMode(true);
                                setPlusOpen(false);
                              }}
                            >
                              <Icon icon={ImageIcon} size="sm" color="secondary" />
                              <span>Create image</span>
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
                                  onClick={() => insertMention(s.name, BookOpen)}
                                >
                                  <Icon icon={BookOpen} size="sm" color="secondary" />
                                  <span>{s.name}</span>
                                </button>
                              ))
                            )}
                            <div className="claw-plus-divider" />
                            <button
                              type="button"
                              className="claw-plus-item claw-plus-manage"
                              onClick={() => manageSettings("skills")}
                            >
                              <Icon icon={ExternalLink} size="sm" color="secondary" />
                              <span>Manage skills in Settings</span>
                            </button>
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
                                  onClick={() => insertMention(c.name, Plug)}
                                >
                                  <Icon icon={Plug} size="sm" color="secondary" />
                                  <span>{c.name}</span>
                                  <span className="claw-plus-count">{c.runtime.tools ?? 0}</span>
                                </button>
                              ))
                            )}
                            <div className="claw-plus-divider" />
                            <button
                              type="button"
                              className="claw-plus-item claw-plus-manage"
                              onClick={() => manageSettings("connectors")}
                            >
                              <Icon icon={ExternalLink} size="sm" color="secondary" />
                              <span>Manage connectors in Settings</span>
                            </button>
                          </>
                        )}
                        {plusView === "knowledge" && (
                          <>
                            <button
                              type="button"
                              className="claw-plus-item claw-plus-back"
                              onClick={() => setPlusView("root")}
                            >
                              <Icon icon={ArrowLeft} size="sm" color="secondary" />
                              <span>Knowledge</span>
                            </button>
                            <div className="claw-plus-divider" />
                            {knowledge.length === 0 ? (
                              <div className="claw-plus-empty">
                                <Text size="sm" color="secondary">
                                  No knowledge bases with documents yet.
                                </Text>
                              </div>
                            ) : (
                              knowledge.map((k) => (
                                <button
                                  key={k.id}
                                  type="button"
                                  className="claw-plus-item"
                                  onClick={() => insertMention(k.name, Library)}
                                >
                                  <Icon icon={Library} size="sm" color="secondary" />
                                  <span>{k.name}</span>
                                  <span className="claw-plus-count">{k.docs}</span>
                                </button>
                              ))
                            )}
                            <div className="claw-plus-divider" />
                            <button
                              type="button"
                              className="claw-plus-item claw-plus-manage"
                              onClick={() => manageSettings("knowledge")}
                            >
                              <Icon icon={ExternalLink} size="sm" color="secondary" />
                              <span>Manage knowledge in Settings</span>
                            </button>
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
                  {sttEnabled && (
                    <button
                      type="button"
                      className={`claw-mic claw-mic--${micState}`}
                      onClick={() => void toggleMic()}
                      aria-label={
                        micState === "recording"
                          ? "Stop recording"
                          : micState === "transcribing"
                            ? "Transcribing…"
                            : "Dictate with your voice"
                      }
                      title={
                        micState === "recording"
                          ? "Stop recording"
                          : micState === "transcribing"
                            ? "Transcribing…"
                            : "Dictate with your voice"
                      }
                      disabled={micState === "transcribing"}
                    >
                      {micState === "transcribing" ? (
                        <Spinner size="sm" shade="subtle" />
                      ) : (
                        <Icon icon={micState === "recording" ? Square : Mic} size="sm" />
                      )}
                    </button>
                  )}
                </div>
                )
              }
              sendActions={
                imageMode ? (
                  imageModels.length > 1 ? (
                    <Popover
                      label="Select image model"
                      placement="above"
                      alignment="end"
                      isOpen={imageModelOpen}
                      onOpenChange={setImageModelOpen}
                      width={360}
                      hasAutoFocus={false}
                      content={
                        <div className="claw-model-menu">
                          <div className="claw-model-menu-title">
                            <Text size="sm" weight="semibold" color="secondary">
                              Select image model
                            </Text>
                          </div>
                          <div className="claw-plus-divider" />
                          {imageModels.map((m) => (
                            <button
                              key={m.model_id}
                              type="button"
                              className="claw-model-option"
                              onClick={() => {
                                setImageModel(m.model_id);
                                setImageModelOpen(false);
                              }}
                            >
                              <div className="claw-model-option-main">
                                <div className="claw-model-option-head">
                                  <span className="claw-model-option-name">{m.label}</span>
                                  {m.scope === "private" && (
                                    <span className="claw-model-option-private">Private</span>
                                  )}
                                  <span className={`claw-cost claw-cost-${m.cost}`}>{COST_LABEL[m.cost]}</span>
                                </div>
                                {m.description && (
                                  <span className="claw-model-option-desc">{m.description}</span>
                                )}
                              </div>
                              {m.model_id === imageModel && (
                                <Icon icon={Check} size="sm" color="secondary" />
                              )}
                            </button>
                          ))}
                        </div>
                      }
                    >
                      <button type="button" className="claw-model-trigger">
                        <Icon icon={ImageIcon} size="sm" color="secondary" />
                        <span className="claw-model-trigger-label">
                          {imageModels.find((m) => m.model_id === imageModel)?.label ?? "Model"}
                        </span>
                        <Icon icon={ChevronDown} size="xsm" color="secondary" />
                      </button>
                    </Popover>
                  ) : undefined
                ) : models.length > 0 ? (
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
                                {m.scope === "private" && (
                                  <span className="claw-model-option-private">Private</span>
                                )}
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
                        <div className="claw-plus-divider" />
                        <button
                          type="button"
                          className="claw-plus-item claw-plus-manage"
                          onClick={manageModelSettings}
                        >
                          <Icon icon={ExternalLink} size="sm" color="secondary" />
                          <span>Manage my models in Settings</span>
                        </button>
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
            {isEmpty && (() => {
              const activeCategory = SUGGESTIONS.find((c) => c.key === suggestionCategory);
              if (!activeCategory) {
                return (
                  <div className="claw-suggestions">
                    {SUGGESTIONS.map((s) => (
                      <button
                        key={s.key}
                        type="button"
                        className="claw-suggestion-chip"
                        onClick={() => setSuggestionCategory(s.key)}
                      >
                        <Icon icon={s.icon} size="sm" color="secondary" />
                        <span>{s.label}</span>
                      </button>
                    ))}
                  </div>
                );
              }
              return (
                <div className="claw-suggestion-panel">
                  <div className="claw-suggestion-panel-header">
                    <Icon icon={activeCategory.icon} size="sm" color="secondary" />
                    <Text weight="semibold">{activeCategory.label}</Text>
                    <IconButton
                      label="Close"
                      icon={<Icon icon="close" size="sm" />}
                      variant="ghost"
                      size="sm"
                      clickAction={() => setSuggestionCategory(null)}
                    />
                  </div>
                  <div className="claw-suggestion-panel-list">
                    {activeCategory.prompts.map((prompt) => (
                      <button
                        key={prompt}
                        type="button"
                        className="claw-suggestion-panel-item"
                        onClick={() => {
                          fillComposer(prompt);
                          setSuggestionCategory(null);
                        }}
                      >
                        {prompt}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })()}
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
                      icon={
                        item.row.status === "pending"
                          ? ShieldAlert
                          : item.row.status === "approved"
                            ? item.row.tool === "exec"
                              ? Terminal
                              : item.row.tool === "workflow" || item.row.tool === "spawn"
                                ? GitBranch
                                : ShieldCheck
                            : ShieldCheck
                      }
                      size="sm"
                      color={item.row.status === "denied" ? "error" : item.row.status === "approved" ? "success" : "secondary"}
                    />
                    <Text size="sm" weight="semibold">
                      {confirmTitle(item.row.tool, item.row.status)}
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
                      <ChatMessageBubble variant="ghost" className="claw-msg-bubble">
                        <Markdown>{sanitizeModelMarkdown(item.content)}</Markdown>
                      </ChatMessageBubble>
                      {sessionId && item.artifacts && item.artifacts.length > 0 && (
                        <div className="claw-artifacts">
                          {item.artifacts.map((p) => {
                            const href = fileUrl(sessionId, p);
                            // Show images (e.g. a generated chart) inline; keep
                            // everything else as an openable chip.
                            if (/\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(p)) {
                              const name = p.split("/").pop() ?? p;
                              return (
                                <a
                                  key={p}
                                  className="claw-artifact-image"
                                  href={href}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  title={`View ${p}`}
                                  onClick={(e) => {
                                    // Plain left-click opens the in-app lightbox instead of
                                    // navigating; cmd/ctrl/shift/middle-click fall through to
                                    // the native anchor behavior (new tab), restoring the
                                    // open-in-new-tab / save-as affordance a <button> lost.
                                    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
                                      return;
                                    }
                                    e.preventDefault();
                                    setLightbox({ src: href, alt: name });
                                  }}
                                >
                                  <img src={href} alt={name} loading="lazy" />
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
                            {ttsEnabled && (
                              <IconButton
                                label={speaking[i] === "playing" ? "Stop reading aloud" : "Read aloud"}
                                icon={
                                  speaking[i] === "loading" ? (
                                    <Spinner size="sm" />
                                  ) : (
                                    <Icon
                                      icon={Volume2}
                                      size="sm"
                                      color={speaking[i] === "playing" ? "primary" : "secondary"}
                                    />
                                  )
                                }
                                variant="ghost"
                                size="sm"
                                isDisabled={speaking[i] === "loading"}
                                clickAction={() => speakMessage(i, item.content)}
                              />
                            )}
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
                            {sessionId && (
                              <IconButton
                                label="Share answer"
                                icon={<Icon icon={Share2} size="sm" color="secondary" />}
                                variant="ghost"
                                size="sm"
                                isDisabled={sharing}
                                clickAction={() => shareAnswer(i)}
                              />
                            )}
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
                <ChatMessageBubble variant="ghost" className="claw-msg-bubble">
                  <Markdown>{streaming}</Markdown>
                  <span className="claw-cursor">▍</span>
                </ChatMessageBubble>
              </ChatMessage>
            )}
            {busy && !streaming && (
              <ChatMessage sender="assistant">
                <ChatMessageBubble variant="ghost" className="claw-msg-bubble">
                  <span className="claw-thinking">
                    <Spinner size="sm" shade="subtle" /> Thinking…
                  </span>
                </ChatMessageBubble>
              </ChatMessage>
            )}
            {imageBusy && (
              <ChatMessage sender="assistant">
                <ChatMessageBubble variant="ghost" className="claw-msg-bubble">
                  <span className="claw-thinking">
                    <Spinner size="sm" shade="subtle" /> Generating image…
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
        <ExecutionPanel steps={execSteps} plan={plan} running={busy} onClose={() => toggleExec(false)} />
      )}
      <Lightbox
        isOpen={lightbox !== null}
        onOpenChange={(open) => {
          if (!open) setLightbox(null);
        }}
        media={lightbox ?? { src: "", alt: "" }}
        hasZoom
      />
    </div>
  );
}
