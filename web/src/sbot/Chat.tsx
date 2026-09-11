import type { TaskResult } from "./api";
import {
  ChatComposer,
  ChatComposerInput,
  type ChatComposerInputHandle,
  type ChatComposerToken,
  type ChatComposerTrigger,
  ChatLayout,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageList,
  ChatMessageMetadata,
  ChatToolCalls,
} from "@astryxdesign/core/Chat";
import { createStaticSource, type SearchableItem } from "@astryxdesign/core/Typeahead";
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
  ArrowRightLeft,
  BarChart3,
  BookOpen,
  Box,
  Check,
  ChevronDown,
  ChevronRight,
  Code,
  Copy,
  Download as DownloadIcon,
  ExternalLink,
  File as FileIcon,
  FileType,
  GitBranch,
  Hourglass,
  Image as ImageIcon,
  Library,
  Loader2,
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
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { BotAvatar, avatarVariantFor, type BotAvatarVariant } from "./BotAvatar";
import { ErrorText } from "./ErrorText";
import { ExecutionPanel } from "./ExecutionPanel";
import {
  ActiveMission,
  AgentEvent,
  ApiError,
  artifactTypeMeta,
  AttachmentRef,
  BlueprintInfo,
  BotInfo,
  type ChatMessage as ChatMessageRow,
  ConnectorInfo,
  KnowledgeBase,
  ModelOption,
  PREVIEWABLE_DOCUMENT_RE,
  PREVIEWABLE_HTML_RE,
  PREVIEWABLE_TABLE_RE,
  SkillInfo,
  WorkingPlan,
  api,
  fileUrl,
  openChatSocket,
  visibleArtifacts,
} from "./api";
import { HtmlPreview } from "./HtmlPreview";
import { ArtifactList } from "./ArtifactList";
import { DocumentPreview } from "./DocumentPreview";
import { SoftnixLogo } from "./Logo";
import { ModeSwitcher } from "../ModeSwitcher";
import { LocalWorkspacePicker } from './LocalWorkspacePicker';
import { TablePreview } from "./TablePreview";
import { SaveToBlueprintButton } from "./SaveToBlueprintButton";
import { useBranding, useT } from "../branding";
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

const COST_LABEL_KEY: Record<string, string> = {
  low: "chat.cost.low",
  medium: "chat.cost.medium",
  high: "chat.cost.high",
  very_high: "chat.cost.veryHigh",
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
    label: "Sbot แนะนำ",
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

// Confirmation-card copy keys per gated tool (Ask mode). The default covers
// any future gated tool without a code change.
const CONFIRM_COPY_KEY: Record<string, { pending: string; approved: string }> = {
  exec: { pending: "chat.confirm.exec.pending", approved: "chat.confirm.exec.approved" },
  workflow: { pending: "chat.confirm.workflow.pending", approved: "chat.confirm.workflow.approved" },
  spawn: { pending: "chat.confirm.spawn.pending", approved: "chat.confirm.spawn.approved" },
};
function confirmTitle(
  t: (key: string) => string,
  tool: string,
  status: ConfirmRow["status"],
): string {
  const copy = CONFIRM_COPY_KEY[tool] ?? {
    pending: "chat.confirm.default.pending",
    approved: "chat.confirm.default.approved",
  };
  if (status === "pending") return t(copy.pending);
  if (status === "approved") return t(copy.approved);
  return t("chat.confirm.declined");
}

type TranscriptItem =
  | {
      kind: "message";
      role: "user" | "assistant";
      content: string;
      artifacts?: string[];
      deliveryId?: string;
      visionModel?: string;
      // A delegated reply: another bot on the team wrote this, not the one the
      // user is chatting with. Not a separate item kind on purpose — it is a
      // message with a different author, and a reloaded row has to render the
      // same as the live one.
      speakerBotId?: string;
      speakerName?: string;
      speakerRole?: string;
      delegatedTask?: string;
      delegationId?: string;
      speakerError?: boolean;
      taskResult?: TaskResult;
      // On a user row in a specialist's own thread: the leader that gave the
      // instruction. Without it the bubble reads as something the user typed
      // in this thread, which is the one thing it is not.
      delegatedBy?: string;
      // The turn that delivered this instruction, on a live `delegated_task`
      // row. A reconnect replays the turn from the start, so the row needs an
      // identity of its own or the same instruction is appended again.
      taskTurnId?: string;
      // What the specialist did while the user waited. Live-only: the server
      // stores the reply, not the working, so a reloaded row has neither and
      // renders as the finished message it is.
      liveSteps?: { index: number; tool: string; detail: string; status: string }[];
      liveText?: string;
    }
  | { kind: "tools"; calls: ToolCallRow[] }
  | { kind: "confirm"; row: ConfirmRow };

function toTranscriptItem(m: ChatMessageRow): TranscriptItem {
  return {
    kind: "message",
    role: m.role,
    content: m.content,
    artifacts: m.meta?.artifacts,
    deliveryId: m.meta?.delivery_id,
    visionModel: m.meta?.vision_model,
    speakerBotId: m.speaker_bot_id ?? m.meta?.coordinator_bot_id ?? undefined,
    speakerName: m.meta?.speaker_name,
    speakerRole: m.meta?.speaker_role,
    delegatedTask: m.meta?.delegated_task,
    delegationId: m.meta?.delegation_id,
    speakerError: m.meta?.speaker_error,
    taskResult: m.meta?.task_result,
    delegatedBy: m.meta?.delegated_by ?? undefined,
  };
}

// `delegate` is left out of the inline tool strip: the same handoff is already
// rendered as the specialist's own message, and a chip reading
// `delegate · {"bot_name":"…"}` next to it is the least readable copy of it.
// The Execution panel still lists it — that view is the technical one.
//
// A failed one is kept, though: a call rejected before any bot was resolved
// (a name the model invented, a missing task, the repeat-limit) never produced
// a specialist message, so hiding its chip too leaves the user with nothing at
// all where a handoff was attempted.
function transcriptCalls(calls: ToolCallRow[]): ToolCallRow[] {
  return calls.filter((c) => c.name !== "delegate" || c.status === "error");
}

// Who a delegated reply belongs to. The live team wins over the names stored on
// the row, so a renamed bot shows its current identity — but the stored pair is
// what keeps an old reply attributable after that bot is deleted.
function speakerOf(
  bots: BotInfo[],
  item: { speakerBotId?: string; speakerName?: string; speakerRole?: string },
): { name: string; role: string; variant: BotAvatarVariant } {
  const bot = bots.find((b) => b.id === item.speakerBotId);
  const name = bot?.name || item.speakerName || "";
  return {
    name,
    role: bot?.role_title || item.speakerRole || "",
    // The bot may be gone — deleted, or archived mid-mission — but the id
    // stored on the row still hashes to the face it wore, so an old reply
    // keeps the avatar the user remembers it by.
    variant: avatarVariantFor(bot ?? { id: item.speakerBotId }),
  };
}

// How many times one scroll-to-top may re-fetch before giving up. A page can
// come back holding no visible messages at all (the server drops tool-call
// stubs after paging the raw table), and the scroll sentinel only re-fires when
// the content it observes changes — so a page that renders nothing would stall
// the walk instead of continuing it. Bounded so a session that is nothing but
// tool traffic can't turn one gesture into an unbounded request run.
const OLDER_PAGE_ATTEMPTS = 5;

// The most recent tool call still running, if any — a confirm card can land
// after the tools group (see the tool_finished handler's comment on why), so
// this scans backward rather than trusting the last item is the tools group.
function findRunningCall(items: TranscriptItem[]): ToolCallRow | null {
  for (let gi = items.length - 1; gi >= 0; gi--) {
    const it = items[gi];
    if (it.kind !== "tools") continue;
    for (let k = it.calls.length - 1; k >= 0; k--) {
      if (it.calls[k].status === "running") return it.calls[k];
    }
  }
  return null;
}

// A turn that has ended has no tool still executing. Any row left "running"
// was orphaned — a tool_finished lost to a reconnect that replayed its
// tool_started, or a call cancelled without one — and because findRunningCall
// scans the whole transcript, one stale row would relabel the thinking spinner
// with a finished turn's tool for the rest of the session.
function settleRunningCalls(
  items: TranscriptItem[],
  status: "complete" | "error",
): TranscriptItem[] {
  let changed = false;
  const next = items.map((it) => {
    if (it.kind !== "tools" || !it.calls.some((c) => c.status === "running")) return it;
    changed = true;
    return {
      ...it,
      calls: it.calls.map((c) =>
        c.status === "running"
          ? {
              ...c,
              status,
              duration: c.duration ?? `${((Date.now() - c.startedAt) / 1000).toFixed(1)}s`,
              substeps: c.substeps?.map((s) => (s.status === "running" ? { ...s, status } : s)),
            }
          : c,
      ),
    };
  });
  return changed ? next : items;
}

// A short human label for whatever a running tool call is doing right now —
// its live sub-step if it has one (e.g. a workflow step), else its args
// preview, else just its name. Used to replace a static "Thinking…" with
// something that visibly moves during a long-running call.
function describeRunningCall(call: ToolCallRow): string {
  const activeSub =
    call.substeps && call.substeps.length > 0
      ? (call.substeps.find((s) => s.status === "running") ?? call.substeps[call.substeps.length - 1])
      : undefined;
  const detail = activeSub?.label ?? call.argsPreview;
  return detail ? `${call.name} · ${detail}` : call.name;
}

// "12s" under a minute, "1m 05s" past it — coarse enough that it doesn't
// need to update more than once a second, but still visibly moves so a long
// wait doesn't read as a frozen screen.
function formatElapsed(startedAt: number | null): string {
  if (startedAt == null) return "";
  const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  return `${mins}m ${String(secs % 60).padStart(2, "0")}s`;
}

const PERMISSION_OPTS: {
  key: PermissionMode;
  labelKey: string;
  descKey: string;
  icon: typeof ShieldCheck;
}[] = [
  {
    key: "ask",
    labelKey: "chat.permission.ask.label",
    descKey: "chat.permission.ask.desc",
    icon: ShieldCheck,
  },
  {
    key: "auto",
    labelKey: "chat.permission.auto.label",
    descKey: "chat.permission.auto.desc",
    icon: ShieldAlert,
  },
];

interface ChatProps {
  /** Active session, or null for the draft landing (no session yet). */
  sessionId: string | null;
  bot?: BotInfo | null;
  groupName?: string;
  /** The user's whole team, used to give a specialist's reply its own avatar
   * when the leader delegates work to it. */
  bots?: BotInfo[];
  userName?: string;
  onFirstMessage?: (text: string) => void;
  /** Open the session this bot converses in (first message / attachment),
   * creating it if it doesn't exist yet. `isNew` distinguishes the two: a
   * reused thread already has a transcript to load. */
  onRequireSession?: () => Promise<{ id: string; isNew: boolean }>;
  /** Fired on turn start/finish so the parent can refresh sidebar status. */
  onActivity?: () => void;
  /** The teammates currently mid-delegation, so the sidebar can mark them busy.
   * Only this transcript knows: a delegated specialist works inside the leader's
   * turn, so the backend never reports a turn of its own for it. Sorted, and the
   * same array is not resent, so the parent can hold it as state. */
  onWorkingBotsChange?: (botIds: string[]) => void;
  /** Whether the backend reports this session as processing (from the parent's
   * session poll) — lets a chat reopened mid-turn show "Thinking" and recover
   * a completion event missed while the socket was closed. */
  running?: boolean;
  initialModel?: string | null;
  /** Jump to the given Settings section (e.g. from a "Manage" link in the "+"
   * menu or the model picker) so Skills/Connectors/Knowledge/My Models stay one
   * click away from where the user actually uses them, instead of a dead end
   * when the list is empty. */
  onOpenSettings?: (section: "skills" | "connectors" | "knowledge" | "blueprints" | "models") => void;
  /** In-flight missions planned in this thread. Passed down rather than fetched
   * here because a mission runs detached from any turn: the socket says nothing
   * about it, and the parent already polls for the sidebar's sake. */
  missions?: ActiveMission[];
}

// Cap on WebSocket reconnect attempts before giving up retrying — a
// deleted/foreign session or a permanently invalid token both close the socket
// in a way the browser can't reliably distinguish from a transient drop, so an
// infinite retry loop isn't an option. Giving up is silent unless a message was
// riding on that socket; the next send reopens it from scratch.
const MAX_RECONNECT_ATTEMPTS = 6;

// How long a send waits for the socket to come back before telling the user the
// connection dropped. A reconnect normally lands well inside this, and a notice
// that resolves itself a moment later reads as a fault the user has to act on.
const RECONNECT_NOTICE_DELAY_MS = 1500;

export function Chat({
  sessionId,
  bot,
  bots = [],
  groupName,
  userName,
  onFirstMessage,
  onRequireSession,
  onActivity,
  onWorkingBotsChange,
  running,
  initialModel,
  onOpenSettings,
  missions = [],
}: ChatProps) {
  const t = useT();
  const { executionPanelEnabled } = useBranding();
  const [items, setItems] = useState<TranscriptItem[]>([]);
  // The session whose initial transcript page has finished loading. Keeping
  // this separate from `items` prevents the previous session (or an empty
  // intermediate frame) from being painted during a bot switch.
  const [loadedSessionId, setLoadedSessionId] = useState<string | null>(null);
  const chatLayoutRef = useRef<HTMLDivElement | null>(null);
  const [streaming, setStreaming] = useState("");
  const [busy, setBusy] = useState(false);
  // When the current busy period started (for the "Thinking… 12s" counter) —
  // a ref because it drives a manual re-render tick below, not React state.
  const busyStartedAtRef = useRef<number | null>(null);
  // Forces a re-render once a second while busy so the elapsed counter (and
  // any active tool's live label) visibly moves instead of looking frozen.
  const [, forceBusyTick] = useState(0);
  const [error, setError] = useState("");
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);
  const [uploading, setUploading] = useState(false);
  const [feedback, setFeedback] = useState<Record<number, "up" | "down">>({});
  const [copied, setCopied] = useState<Record<number, boolean>>({});
  // Which specialists' working-out is expanded, keyed by delegation id. Absent
  // means "follow the default": open while the bot is still working (the wait
  // is what this view is for), closed once its reply is on screen.
  const [openWork, setOpenWork] = useState<Record<string, boolean>>({});
  const [sharing, setSharing] = useState(false);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [defaultModel, setDefaultModel] = useState<string>("");
  const [model, setModel] = useState<string>(initialModel ?? "");
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [knowledge, setKnowledge] = useState<KnowledgeBase[]>([]);
  const [blueprints, setBlueprints] = useState<BlueprintInfo[]>([]);
  const [plusOpen, setPlusOpen] = useState(false);
  const [plusView, setPlusView] = useState<"root" | "skills" | "connectors" | "knowledge" | "blueprints">("root");
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
  // The panel itself is opt-in (Settings > Profile > Preferences, off by
  // default) — a stale "1" from before the setting existed, or from another
  // account on this browser, must never resurrect it on its own. Exception:
  // a live plan (see "plan_updated" below) forces it open regardless of the
  // preference, since a working plan is worth surfacing either way.
  const [execOpen, setExecOpen] = useState(() => localStorage.getItem("claw_exec_open") === "1");
  const openExecIfEnabled = useCallback(() => {
    if (executionPanelEnabled) setExecOpen(true);
  }, [executionPanelEnabled]);
  // The agent's live working plan (from plan_updated events), shown pinned atop
  // the Execution panel. Null until the agent sets one this session.
  const [plan, setPlan] = useState<WorkingPlan | null>(null);
  // Mirrors `plan` for the "plan_updated" handler below, which runs inside a
  // WebSocket effect keyed only on [sessionId] and so closes over a stale
  // `plan` — used to detect a genuinely new/changed plan vs. a reconnect
  // replaying the same event, so closing the panel doesn't get undone by a resend.
  const planRef = useRef<WorkingPlan | null>(null);
  useEffect(() => {
    planRef.current = plan;
  }, [plan]);
  const socketRef = useRef<WebSocket | null>(null);
  // Mirrors the `sessionId` prop for use inside async callbacks (e.g.
  // runGeneratedImage) — lets them tell, after an await, whether the user has
  // since switched to a different session so they don't apply a stale
  // result/error/busy-state to the wrong session's transcript.
  const sessionIdRef = useRef(sessionId);
  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);
  // Drives the "Thinking… Ns" counter: starts the clock the moment a turn
  // goes busy, ticks once a second so the label visibly moves during a long
  // tool call, and stops (no wasted timer) the moment it isn't busy anymore.
  useEffect(() => {
    if (!busy) {
      busyStartedAtRef.current = null;
      return;
    }
    busyStartedAtRef.current = Date.now();
    const id = window.setInterval(() => forceBusyTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [busy]);
  // Who the leader has work out with right now: an assignment card that has not
  // been given its reply yet — the same condition that renders "Working on it…"
  // below. Gated on `busy` because a card can only be genuinely open during a
  // turn; a transcript left with an unanswered card by a dropped socket would
  // otherwise pin its bot as busy forever. Joined into a string so switching
  // sessions or streaming text does not hand the parent a fresh array to store
  // on every render.
  const workingBotKey = useMemo(() => {
    if (!busy) return "";
    const ids = items.flatMap((it) =>
      it.kind === "message" && it.speakerBotId && !it.content ? [it.speakerBotId] : [],
    );
    return [...new Set(ids)].sort().join(",");
  }, [items, busy]);
  useEffect(() => {
    onWorkingBotsChange?.(workingBotKey ? workingBotKey.split(",") : []);
    // Clearing on the way out covers unmount — the socket closes with it, so
    // this transcript stops being able to tell whether anyone is still working.
    return () => onWorkingBotsChange?.([]);
  }, [workingBotKey, onWorkingBotsChange]);
  const fileRef = useRef<HTMLInputElement | null>(null);
  // Imperative handle for the composer's contentEditable — lets us insert a
  // styled mention chip (skill/connector/knowledge) instead of raw "@name " text.
  const composerHandleRef = useRef<ChatComposerInputHandle>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const sentCountRef = useRef(0);
  // Transcript paging. `olderCursor` is the lowest `seq` on screen and is what
  // the next fetch asks to go before; state rather than a ref because the
  // scroll-to-top callback has to be rebuilt when it moves, or the sentinel
  // keeps calling a closure pinned to the first page's cursor.
  const [hasOlder, setHasOlder] = useState(false);
  const [olderCursor, setOlderCursor] = useState<number | null>(null);
  // Guards re-entry: the sentinel can fire again while a fetch is in flight.
  const loadingOlderRef = useRef(false);
  // A message typed on the draft landing, held until the freshly-created
  // session's socket opens, then flushed. Lets the composer create the session
  // lazily (like claude.ai) instead of on page load. Also doubles as the
  // offline-send queue for an EXISTING session whose socket dropped (see
  // reconnect handling below) — `sessionId: null` means "the next session to
  // be created" (draft handoff), a real id means "only that session's onopen
  // may flush this," so a message queued in one session can't be delivered
  // into, or wrongly mistaken for a fresh empty session by, a different one.
  // Captures the model chosen at send-click too, so the flush uses exactly what
  // the user picked even if `model` state changes during the create handoff.
  const pendingRef = useRef<{
    sessionId: string | null;
    content: string;
    atts: AttachmentRef[];
    model: string;
  } | null>(null);
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
  // Mobile browsers (iOS Safari, Chrome/Android) aggressively tear down open
  // WebSockets on backgrounding, screen lock, or a WiFi<->cellular handoff —
  // desktop doesn't do this, which is why the bug below only shows up on
  // phones. Without a reconnect path, the socket just stays CLOSED until the
  // user picks a different session (which re-runs the per-session effect).
  // These let code outside that effect (send/rawSend, visibility/online
  // listeners) trigger a reconnect without restructuring its closures.
  const openSocketRef = useRef<() => void>(() => {});
  const ensureConnectedRef = useRef<() => void>(() => {});
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectNoticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The banner text the connection paths below put on screen, so a successful
  // reconnect can retract it without also wiping an unrelated error (a model
  // failure, a failed upload) that happened to land in the same banner after.
  const connectionErrorRef = useRef<string | null>(null);
  // `busy` read from inside the per-session effect, whose closures are bound
  // once per session and would otherwise see it as it was at bind time.
  const busyRef = useRef(false);
  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

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
    setLoadedSessionId(null);
    setStreaming("");
    setError("");
    connectionErrorRef.current = null;
    setAttachments([]);
    setFeedback({});
    setCopied({});
    setPlan(null); // plan is per-session; don't leak one chat's plan into another
    setSuggestionCategory(null);
    // "Create image" mode is per-session UI state too — without this, opening
    // it in one chat and switching to another leaves the composer stuck in
    // image-prompt mode (and any leftover prompt text) in the new session.
    setImageMode(false);
    setImagePrompt("");
    setImageBusy(false);
    sentCountRef.current = 0;
    setHasOlder(false);
    setOlderCursor(null);
    loadingOlderRef.current = false;
    sawCompletionRef.current = false;
    prevRunningRef.current = !!running;
    // A prior session's flap count must not inflate this session's first
    // reconnect delay.
    reconnectAttemptRef.current = 0;
    // Draft landing: no session yet, so no socket and nothing to load.
    if (!sessionId) {
      setBusy(false);
      return;
    }
    // A pending draft message (queued with sessionId: null) means this
    // session was just created and is therefore empty — skip the message
    // fetch (which would race the flush and could clobber the optimistic
    // bubble) and keep the spinner up so the greeting doesn't flash back in
    // during the handoff. A message queued for a DIFFERENT, already-existing
    // session (offline-send while that session's socket was down) must NOT
    // trigger this — this session has real history to load — so only a
    // null-tagged (draft) pending counts as a handoff.
    const handoff = pendingRef.current?.sessionId === null;
    // Also keep the spinner up when reopening a session the backend says is
    // still processing.
    // A pending image prompt is the same "fresh, empty session" situation,
    // but it's a REST call with no socket turn to eventually clear `busy` —
    // only skip the fetch (so it can't clobber the optimistic user bubble
    // pushed by the pendingImageRef effect below), don't fold it into the
    // handoff spinner.
    const skipMessageFetch = handoff || pendingImageRef.current !== null;
    setBusy(handoff || running === true);
    let cancelled = false;
    let socket: WebSocket | null = null;
    const cancelReconnectNotice = () => {
      if (!reconnectNoticeTimerRef.current) return;
      clearTimeout(reconnectNoticeTimerRef.current);
      reconnectNoticeTimerRef.current = null;
    };
    // Every banner about the connection goes through here so onOpen knows which
    // text it is allowed to retract later.
    const showConnectionError = (message: string) => {
      connectionErrorRef.current = message;
      setError(message);
    };
    const onOpen = () => {
      // A socket still mid-handshake when this effect was torn down (session
      // switch, StrictMode remount) is closed by the cleanup only once it
      // opens, so its onopen still fires — and everything below would then
      // act on the CURRENT session's state: retracting its notice, and
      // draining pendingRef into a rawSend that no-ops because socketRef now
      // points at a different, not-yet-open socket. That silently ate the
      // message.
      if (cancelled) return;
      reconnectAttemptRef.current = 0;
      // Back before the notice was due — the drop never became the user's
      // problem, so don't tell them about it.
      cancelReconnectNotice();
      // The connection is demonstrably back, so retract the banner that said
      // otherwise — but only if it's still the one on screen. Leaving it up
      // was the whole reason users learned to reload a working app.
      if (connectionErrorRef.current !== null) {
        const stale = connectionErrorRef.current;
        connectionErrorRef.current = null;
        setError((current) => (current === stale ? "" : current));
      }
      const pending = pendingRef.current;
      // Only flush a pending message that's either a draft handoff (targets
      // whichever session gets created next) or explicitly queued for THIS
      // session — otherwise a message queued in a different, still-open
      // session would get delivered down this one's socket.
      if (pending && (pending.sessionId === null || pending.sessionId === sessionId)) {
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
          openExecIfEnabled(); // auto-open the execution panel while the agent works, if enabled
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
        case "delegated_task":
          // This bot's own thread, and the leader that gave the instruction is
          // in another session — so unlike every other user message, no client
          // has rendered this one.
          setItems((prev) => {
            const already = prev.some(
              (it) => it.kind === "message" && !!it.taskTurnId && it.taskTurnId === event.turn_id,
            );
            if (already) return prev;
            return [
              ...prev,
              {
                kind: "message",
                role: "user",
                content: event.content ?? "",
                delegatedBy: event.delegated_by || undefined,
                taskTurnId: event.turn_id,
              },
            ];
          });
          break;
        case "delegation_started":
          // The leader handed the work to a teammate: show the assignment and
          // who is on it right away, so a long delegation isn't a dead screen.
          setStreaming("");
          setItems((prev) => {
            // A reconnect replays the whole turn, so an assignment already on
            // screen must not be shown a second time. Keyed on the delegation
            // rather than bot+task: the leader may legitimately hand the same
            // bot the same task twice, and that is two cards, not one.
            const already = prev.some(
              (it) =>
                it.kind === "message" && !!it.delegationId && it.delegationId === event.delegation_id,
            );
            // The replay does not stop here: every step and delta of this
            // assignment is about to arrive a second time, and the deltas
            // accumulate. Clearing the live view lets the replay rebuild it
            // instead of printing the specialist's answer twice.
            if (already)
              return prev.map((it) =>
                it.kind === "message" &&
                it.delegationId === event.delegation_id &&
                !it.content
                  ? { ...it, liveText: undefined, liveSteps: undefined }
                  : it,
              );
            return [
              ...prev,
              {
                kind: "message",
                role: "assistant",
                content: "",
                speakerBotId: event.bot_id,
                speakerName: event.bot_name,
                speakerRole: event.role_title,
                delegatedTask: event.task ?? "",
                delegationId: event.delegation_id,
              },
            ];
          });
          break;
        case "delegation_step":
        case "delegation_delta":
          setItems((prev) => {
            const at = prev.findIndex(
              (it) =>
                it.kind === "message" && !!it.delegationId && it.delegationId === event.delegation_id,
            );
            // No card to report into: its `delegation_started` was evicted from
            // a full queue, or the reply already landed and replaced the live
            // view. Either way this is a preview of something that has a home
            // elsewhere, so dropping it is better than inventing a second card.
            if (at < 0) return prev;
            const it = prev[at];
            if (it.kind !== "message" || it.content) return prev;
            const next = [...prev];
            if (event.type === "delegation_delta") {
              next[at] = { ...it, liveText: (it.liveText ?? "") + (event.text ?? "") };
              return next;
            }
            const steps = [...(it.liveSteps ?? [])];
            const step = {
              index: event.index ?? 0,
              tool: event.tool ?? "",
              detail: event.detail ?? "",
              status: event.status ?? "running",
            };
            // The finish carries the same index as its start and replaces it:
            // one row per step, showing arguments while it runs and the result
            // once it is done.
            const found = steps.findIndex((s) => s.index === step.index);
            if (found >= 0) steps[found] = step;
            else steps.push(step);
            next[at] = { ...it, liveSteps: steps };
            return next;
          });
          break;
        case "delegation_finished":
          setItems((prev) => {
            const at = prev.findIndex(
              (it) =>
                it.kind === "message" && !!it.delegationId && it.delegationId === event.delegation_id,
            );
            if (at >= 0) {
              const it = prev[at];
              // Already answered — a reconnect is replaying the turn. Without
              // this the fallback below would append the whole reply again.
              if (it.kind !== "message" || it.content) return prev;
              const next = [...prev];
              next[at] = {
                ...it,
                content: event.text ?? "",
                artifacts: event.artifacts,
                speakerError: event.is_error,
                taskResult: event.result,
              };
              return next;
            }
            // No open assignment: its delegation_started was dropped (a slow
            // subscriber's queue overflowed). The answer still belongs in the
            // transcript — an unlabelled gap is worse than a card with no task.
            return [
              ...prev,
              {
                kind: "message",
                role: "assistant",
                content: event.text ?? "",
                speakerBotId: event.bot_id,
                delegationId: event.delegation_id,
                artifacts: event.artifacts,
                speakerError: event.is_error,
                taskResult: event.result,
              },
            ];
          });
          break;
        case "plan_updated": {
          // The agent revised its working plan — show it pinned in the panel.
          // Unlike other auto-opens, this bypasses the execution_panel_enabled
          // preference: a live plan is worth surfacing even for users who've
          // opted out of the tool-activity feed. Only force the panel open for
          // an actual change, not a reconnect replaying the same event — the
          // backend's per-turn replay buffer resends everything sent so far
          // this turn, and without this check that resend would undo a user's
          // deliberate close.
          const nextPlan = { goal: event.goal ?? "", steps: event.steps ?? [] };
          const prev = planRef.current;
          const changed =
            !prev ||
            prev.goal !== nextPlan.goal ||
            JSON.stringify(prev.steps) !== JSON.stringify(nextPlan.steps);
          if (changed) setExecOpen(true);
          setPlan(nextPlan);
          break;
        }
        case "tool_confirm_request":
          openExecIfEnabled();
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
        case "mission_reported":
          setItems((prev) => prev.some((item) => item.kind === "message" && item.deliveryId === event.turn_id)
            ? prev : [...prev, { kind: "message", role: "assistant", content: event.content ?? "",
              artifacts: event.artifacts, deliveryId: event.turn_id }]);
          break;
        case "turn_completed":
          setBusy(false);
          setStreaming("");
          sawCompletionRef.current = true;
          setItems((prev) => {
            const settled = settleRunningCalls(prev, "complete");
            if (!event.content) return settled;
            return [
              ...settled,
              {
                kind: "message",
                role: "assistant",
                content: event.content!,
                artifacts: event.artifacts,
                visionModel: event.vision_model,
                speakerBotId: event.coordinator_bot_id,
                speakerName: event.speaker_name,
                speakerRole: event.coordinator_bot_id ? "Group leader" : undefined,
              },
            ];
          });
          onActivity?.();
          break;
        case "turn_error":
          setBusy(false);
          setStreaming("");
          sawCompletionRef.current = true;
          setItems((prev) => settleRunningCalls(prev, "error"));
          setError(event.message ?? "unknown error");
          onActivity?.();
          break;
      }
    };
    // A give-up (permanent auth failure, or reconnect attempts exhausted)
    // leaves a queued message stranded forever otherwise — surface that
    // instead of silently discarding it.
    const discardQueuedMessage = () => {
      const pending = pendingRef.current;
      if (!pending || (pending.sessionId !== null && pending.sessionId !== sessionId)) return false;
      pendingRef.current = null;
      toast({ body: t("chat.error.sendFailed"), type: "error" });
      return true;
    };
    const onClose = (ev: CloseEvent) => {
      // Checked before anything below reads component state: a socket belonging
      // to a torn-down session must not paint a banner or drain pendingRef,
      // which by now may hold the draft the user is typing into a new chat.
      if (cancelled) return;
      if (ev.code === 4401) {
        showConnectionError(t("chat.error.authFailed"));
        discardQueuedMessage();
        cancelReconnectNotice();
        return;
      }
      // Any other close (network drop, mobile suspension, server restart) is
      // abnormal — retry with capped exponential backoff instead of leaving
      // the session stuck until the user reloads the page. Note:
      // a permanently-failing close (e.g. a deleted/foreign session, closed
      // server-side before the WS handshake completes) isn't distinguishable
      // from a transient drop by close code alone — such closes never reach
      // the browser with their real code, only as an aborted handshake — so
      // this can't special-case them; the attempt cap below bounds the
      // damage instead.
      const attempt = reconnectAttemptRef.current + 1;
      reconnectAttemptRef.current = attempt;
      if (attempt > MAX_RECONNECT_ATTEMPTS) {
        // Stop retrying, but only say so if the user is actually waiting on
        // this socket — a message queued on it, or a turn still streaming. An
        // idle tab drops its socket routinely (server restart, laptop sleep)
        // and reopens fine on the next send or tab focus; a banner telling
        // those users to reload is wrong, and it trained people to reload for
        // a connection that was never broken. `busyRef` matters because the
        // spinner has no other way out here: the transcript refetch that
        // normally rescues a missed completion is driven by the session-list
        // poll flipping running true→false, and that poll fails (leaving the
        // stale value) against the same dead backend that killed this socket.
        if (discardQueuedMessage() || busyRef.current) {
          showConnectionError(t("chat.error.reconnectFailed"));
        }
        cancelReconnectNotice();
        return;
      }
      const delay = Math.min(1000 * 2 ** (attempt - 1), 15000);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = setTimeout(() => openSocketRef.current(), delay);
    };

    const openSocket = () => {
      if (cancelled) return;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      socket = openChatSocket(sessionId);
      socketRef.current = socket;
      socket.onopen = onOpen;
      socket.onmessage = onMessage;
      socket.onclose = onClose;
    };
    openSocketRef.current = openSocket;
    // Reconnect immediately (skipping the backoff delay) when the tab regains
    // focus or the network comes back — don't make the user wait out a timer
    // that was sized for a background retry once they're actively looking.
    ensureConnectedRef.current = () => {
      if (cancelled) return;
      const state = socketRef.current?.readyState;
      if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) return;
      reconnectAttemptRef.current = 0;
      openSocketRef.current();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") ensureConnectedRef.current();
    };
    // Not gated on visibility: network can recover while the tab is still
    // backgrounded, and a hidden tab's backoff timer is subject to browser
    // throttling — don't wait on it when a real "online" signal fires.
    const onOnline = () => ensureConnectedRef.current();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onOnline);

    if (skipMessageFetch) {
      // Fresh draft session — connect immediately to flush the pending message.
      setLoadedSessionId(sessionId);
      openSocket();
    } else {
      // Seed the persisted transcript FIRST, then open the socket. The bus
      // replays the current turn's live events on connect; connecting only
      // after listMessages resolves keeps those replayed tool/confirm/sub-step
      // cards from being clobbered by a late message-list response — the bug
      // where returning to a still-processing session dropped the live view.
      api
        .listMessages(sessionId)
        .then((page) => {
          if (cancelled) return;
          // A page can come back all-filtered (tool-only stubs) with history
          // still behind it; counting that as 0 would re-arm the auto-title
          // and rename an already-titled session on the next send.
          sentCountRef.current = page.messages.length || (page.has_more ? 1 : 0);
          setItems(page.messages.map(toTranscriptItem));
          setHasOlder(page.has_more);
          setOlderCursor(page.next_before_seq);
          setLoadedSessionId(sessionId);
        })
        .catch((e) => {
          if (!cancelled) {
            setError(String(e));
            setLoadedSessionId(sessionId);
          }
        })
        .finally(() => openSocket());
    }

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onOnline);
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      cancelReconnectNotice();
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

  // Initial history restoration must be an instant placement, not the spring
  // animation ChatLayout uses for newly streaming content. This layout effect
  // runs after the populated transcript is committed but before it is painted,
  // so the first visible frame is already anchored at the latest message.
  useLayoutEffect(() => {
    if (!sessionId || loadedSessionId !== sessionId) return;
    const container = chatLayoutRef.current;
    if (!container) return;
    container.scrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
  }, [sessionId, loadedSessionId]);

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
        .then((page) => {
          sentCountRef.current = page.messages.length || (page.has_more ? 1 : 0);
          // Back to just the newest page, cursor reset to match. This already
          // replaced the whole transcript before paging existed; keeping older
          // pages here would leave them stranded above a cursor that no longer
          // describes what is on screen.
          setItems(page.messages.map(toTranscriptItem));
          setHasOlder(page.has_more);
          setOlderCursor(page.next_before_seq);
          setBusy(false);
          setStreaming("");
        })
        .catch(() => undefined);
    }
  }, [running, sessionId]);

  // Feedback, copy confirmations and TTS state are keyed by an item's position
  // in `items`, which only ever changes by prepending older pages — so rebase
  // them by the same amount instead of letting them point at the wrong message.
  const shiftIndexedState = useCallback((by: number) => {
    if (by <= 0) return;
    const rekey = <T,>(prev: Record<number, T>) =>
      Object.fromEntries(
        Object.entries(prev).map(([k, v]) => [Number(k) + by, v]),
      ) as Record<number, T>;
    setFeedback(rekey);
    setCopied(rekey);
    setSpeaking(rekey);
    if (speakingIndexRef.current !== null) speakingIndexRef.current += by;
  }, []);

  // Walk one page further back into the transcript. Driven by the message
  // list's scroll-to-top sentinel, which also fires on mount when the first
  // page doesn't fill the viewport — that is wanted, it just keeps loading
  // until the history is exhausted or the screen is full.
  const loadOlder = useCallback(async () => {
    if (!sessionId || loadingOlderRef.current) return;
    loadingOlderRef.current = true;
    try {
      let cursor = olderCursor;
      for (let attempt = 0; attempt < OLDER_PAGE_ATTEMPTS; attempt++) {
        if (cursor == null) return;
        const page = await api.listMessages(sessionId, { beforeSeq: cursor });
        // Switching chats mid-fetch: `items` and the cursor now belong to the
        // other session, so prepending this page would splice one chat's
        // history into another and leave a cursor pointing at foreign seqs.
        if (sessionIdRef.current !== sessionId) return;
        cursor = page.next_before_seq;
        // A page with no cursor of its own reached the start of the history;
        // `has_more` cannot be trusted to also be false, so stop on either.
        const more = page.has_more && cursor != null;
        if (page.messages.length > 0) {
          setItems((prev) => [...page.messages.map(toTranscriptItem), ...prev]);
          // Per-message state is keyed by position, so prepending a page moves
          // every existing message and would leave a thumbs-up, a "Copied", or
          // a playing-audio highlight sitting on someone else's bubble.
          shiftIndexedState(page.messages.length);
          setOlderCursor(cursor);
          setHasOlder(more);
          return;
        }
        // Nothing visible on this page — keep the cursor advancing so the next
        // iteration asks for something new rather than refetching this page.
        setOlderCursor(cursor);
        setHasOlder(more);
        if (!more) return;
      }
    } catch {
      // Leave `hasOlder` set so the sentinel can retry; a failed page must not
      // look like the top of the conversation.
    } finally {
      loadingOlderRef.current = false;
    }
  }, [sessionId, olderCursor, shiftIndexedState]);

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
    api.listBlueprints().then(setBlueprints).catch(() => setBlueprints([]));
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

  // Shared chip builder: the submitted value is always "@name " (the agent's
  // mention cue) regardless of whether the chip was inserted via the "+" menu
  // or the "/" trigger below — both are just two entry points to the same token.
  const mentionToken = useCallback(
    (name: string, icon: typeof BookOpen): ChatComposerToken => ({
      value: `@${name} `,
      render: () => (
        <span className="claw-mention-chip">
          <Icon icon={icon} size="xsm" />
          <span className="claw-mention-chip-label">{name}</span>
          <button
            type="button"
            className="claw-mention-chip-remove"
            aria-label={t("chat.mention.remove", { name })}
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
    }),
    [removeMentionChip, t],
  );

  const insertMention = useCallback(
    (name: string, icon: typeof BookOpen) => {
      const handle = composerHandleRef.current;
      if (handle) {
        handle.focus();
        // Insert as a styled chip (not raw "@name " text) so the composer shows
        // a compact pill with a remove (×) control in case the user changes
        // their mind.
        handle.insertToken(mentionToken(name, icon));
        emitComposerInput();
      }
      setPlusOpen(false);
      setPlusView("root");
    },
    [emitComposerInput, mentionToken],
  );

  // Same skills/connectors/knowledge as the "+" menu, exposed as a "/" trigger
  // inside the composer too — a faster, keyboard-only path for users coming
  // from other AI agent tools that expect "/" to bring up a command menu.
  type SlashMentionItem = SearchableItem<{ icon: typeof BookOpen; kind: string }>;

  const slashItems = useMemo<SlashMentionItem[]>(
    () => [
      ...skills.map((s) => ({
        id: `skill:${s.id}`,
        label: s.name,
        auxiliaryData: { icon: BookOpen, kind: t("chat.slash.kind.skill") },
      })),
      ...connectors.map((c) => ({
        id: `connector:${c.id}`,
        label: c.name,
        auxiliaryData: { icon: Plug, kind: t("chat.slash.kind.connector") },
      })),
      ...knowledge.map((k) => ({
        id: `knowledge:${k.id}`,
        label: k.name,
        auxiliaryData: { icon: Library, kind: t("chat.slash.kind.knowledge") },
      })),
    ],
    [skills, connectors, knowledge, t],
  );

  const composerTriggers = useMemo<ChatComposerTrigger[]>(
    () => [
      {
        character: "/",
        searchSource: createStaticSource(slashItems),
        menuLabel: t("chat.slash.menuLabel"),
        emptySearchResultsText: t("chat.slash.empty"),
        renderItem: (item) => {
          const data = (item as SlashMentionItem).auxiliaryData;
          return (
            <span className="claw-slash-item">
              <Icon icon={data?.icon ?? Sparkles} size="sm" color="secondary" />
              <span className="claw-slash-item-label">{item.label}</span>
              <span className="claw-slash-item-kind">{data?.kind}</span>
            </span>
          );
        },
        onSelect: (item) => {
          const data = (item as SlashMentionItem).auxiliaryData;
          return mentionToken(item.label, data?.icon ?? Sparkles);
        },
      },
    ],
    [slashItems, mentionToken, t],
  );

  // Jump from the "+" menu straight to the matching Settings section — keeps
  // the "where do I manage this?" answer one click away instead of a dead end.
  const manageSettings = useCallback(
    (section: "skills" | "connectors" | "knowledge" | "blueprints") => {
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
      toast({ body: t("chat.error.micUnavailable"), type: "error" });
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
          else toast({ body: t("chat.error.micNoSpeech"), type: "info" });
        } catch (e) {
          toast({ body: `${t("chat.error.transcriptionFailed")}: ${String(e)}`, type: "error" });
        } finally {
          setMicState("idle");
        }
      };
      recorder.start();
      setMicState("recording");
    } catch {
      toast({ body: t("chat.error.micDenied"), type: "error" });
      setMicState("idle");
    }
  }, [micState, appendTranscript, toast, t]);

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
          blueprints: atts.flatMap((a) => a.blueprint ? [{ ...a.blueprint, path: a.path }] : []),
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

  // Shared by both pendingRef writers (draft handoff and offline-send queue,
  // below). Only one message can be queued at a time — warn instead of
  // silently dropping if a still-unflushed message for a DIFFERENT session
  // is about to be overwritten (e.g. user starts a new chat while a previous
  // session's send is still waiting to reconnect).
  const queueOfflineMessage = useCallback(
    (targetSessionId: string | null, content: string, atts: AttachmentRef[], model: string) => {
      const prior = pendingRef.current;
      if (prior && prior.sessionId !== targetSessionId) {
        toast({ body: t("chat.error.prevMessageFailed"), type: "error" });
      }
      pendingRef.current = { sessionId: targetSessionId, content, atts, model };
    },
    [toast, t],
  );

  const send = useCallback(
    (value: string) => {
      const content = value.trim();
      const atts = attachments;
      if (!content && atts.length === 0) return;
      // Draft landing: create the session first, then flush this message once
      // its socket connects (handled in the session effect's onopen).
      if (!sessionId) {
        queueOfflineMessage(null, content, atts, model);
        setAttachments([]);
        setError("");
        onRequireSession?.()
          .then(({ id, isNew }) => {
            // A reused thread (one bot, one chat) is not the empty session the
            // null tag stands for. Re-tagging it makes this the ordinary
            // queued-for-an-existing-session case instead, so the effect loads
            // the thread's history rather than assuming there is none.
            const pending = pendingRef.current;
            if (!isNew && pending?.sessionId === null) pending.sessionId = id;
          })
          .catch((e) => {
            pendingRef.current = null;
            setError(`${t("chat.error.startChatFailed")}: ${String(e)}`);
          });
        return;
      }
      if (socketRef.current?.readyState !== WebSocket.OPEN) {
        // Socket dropped (common on mobile: backgrounding/lock/network switch)
        // — don't silently swallow the tap. Queue it the same way the draft
        // landing does (flushed by the session effect's onopen once
        // reconnected) and kick a reconnect attempt right away instead of
        // waiting out the backoff timer. Only the latest queued message for
        // THIS session is kept; a burst of sends while offline collapses to
        // the last one. uniqueID collapses repeat taps into one toast instead
        // of stacking a new one per tap.
        queueOfflineMessage(sessionId, content, atts, model);
        setAttachments([]);
        setError("");
        // Held back so a reconnect that lands quickly stays invisible: onopen
        // cancels this and flushes the queued message, and the send looks like
        // any other. uniqueID collapses repeat taps into one toast.
        if (reconnectNoticeTimerRef.current) clearTimeout(reconnectNoticeTimerRef.current);
        reconnectNoticeTimerRef.current = setTimeout(() => {
          reconnectNoticeTimerRef.current = null;
          toast({ body: t("chat.info.reconnecting"), type: "info", uniqueID: "ws-reconnect" });
        }, RECONNECT_NOTICE_DELAY_MS);
        ensureConnectedRef.current();
        return;
      }
      rawSend(content, atts);
      setAttachments([]);
    },
    [attachments, sessionId, onRequireSession, rawSend, model, toast, queueOfflineMessage, t],
  );

  const onPickFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;
      setUploading(true);
      setError("");
      try {
        // Attachments are stored per session, so materialize the draft session
        // before uploading.
        const sid = sessionId ?? (await onRequireSession?.())?.id;
        if (!sid) throw new Error("No session");
        const refs = await api.uploadAttachments(sid, Array.from(files));
        setAttachments((prev) => [...prev, ...refs]);
      } catch (e) {
        setError(`${t("chat.error.uploadFailed")}: ${String(e)}`);
      } finally {
        setUploading(false);
        if (fileRef.current) fileRef.current.value = "";
      }
    },
    [sessionId, onRequireSession, t],
  );

  const selectBlueprint = useCallback(
    async (blueprint: BlueprintInfo) => {
      setUploading(true);
      setError("");
      try {
        const sid = sessionId ?? (await onRequireSession?.())?.id;
        if (!sid) throw new Error("No session");
        const ref = await api.materializeBlueprint(blueprint.id);
        setAttachments((prev) => [...prev, ref]);
        setPlusOpen(false);
        setPlusView("root");
      } catch (e) {
        setError(`${t("chat.error.blueprintFailed")}: ${String(e)}`);
      } finally {
        setUploading(false);
      }
    },
    [sessionId, onRequireSession, t],
  );

  // Text-to-image generation: a one-shot REST call (no socket, no agent loop).
  // Shows the prompt right away (like a normal sent message), then the
  // "Generating image…" bubble covers the wait until the artifact lands.
  const runGeneratedImage = useCallback(
    async (sid: string, prompt: string) => {
      // Held by reference, not by index: loading older messages prepends to
      // `items`, which would shift a saved index onto someone else's bubble.
      const optimistic: TranscriptItem = { kind: "message", role: "user", content: prompt };
      setItems((prev) => [...prev, optimistic]);
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
          const at = next.indexOf(optimistic);
          if (at !== -1) next[at] = { ...optimistic, content: res.prompt };
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
          const at = prev.indexOf(optimistic);
          return at === -1 ? prev : [...prev.slice(0, at), ...prev.slice(at + 1)];
        });
        setError(`${t("chat.error.imageGenFailed")}: ${e instanceof ApiError ? e.message : String(e)}`);
      } finally {
        if (stillOnThisSession()) setImageBusy(false);
      }
    },
    [imageModel, onFirstMessage, t],
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
        setError(`${t("chat.error.imageGenFailed")}: ${e instanceof ApiError ? e.message : String(e)}`);
      });
    },
    [imageModel, imageBusy, sessionId, onRequireSession, runGeneratedImage, t],
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
        toast({ body: t("chat.info.copied"), type: "info", autoHideDuration: 2000 });
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
            toast({ body: t("chat.error.copyFailed"), type: "error" });
          }
        } catch {
          toast({ body: t("chat.error.copyFailed"), type: "error" });
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
    [toast, t],
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
        toast({ body: t("chat.error.speakFailed"), type: "error" });
        if (speakingIndexRef.current === index) {
          speakingIndexRef.current = null;
          setSpeaking((prev) => ({ ...prev, [index]: undefined }));
        }
      }
    },
    [stopSpeaking, toast, t],
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
        // Share only what the strip shows. The server filters too, but a public
        // link is the one place where over-sharing is unrecoverable, so don't
        // hand it helper scripts and base64 payloads in the first place.
        msgs.push({
          role: "assistant",
          content: answer.content,
          artifacts: visibleArtifacts(answer.artifacts),
        });
        const res = await api.createShare(sessionId, { messages: msgs });
        // Build the URL from the current origin so the recipient hits the same
        // host the sharer is on (works on localhost, LAN IP, or a real domain).
        const url = `${window.location.origin}${res.path}`;
        const ok = await writeClipboard(url);
        toast(
          ok
            ? { body: t("chat.info.linkCopied"), type: "info", autoHideDuration: 2500 }
            : { body: `${t("chat.info.shareLinkFallback")}: ${url}`, type: "info", autoHideDuration: 10000 },
        );
      } catch {
        toast({ body: t("chat.error.shareFailed"), type: "error" });
      } finally {
        setSharing(false);
      }
    },
    [sessionId, items, sharing, toast, t],
  );

  // `hasOlder` counts as non-empty: an all-filtered first page renders nothing
  // yet, and treating that as an empty session would hide the message list —
  // and with it the scroll-to-top sentinel that is the only way to reach the
  // rest of the history.
  // The welcome landing belongs only to an actual draft (no session id). A
  // persisted session starts with an empty in-memory transcript while its
  // newest page is fetched; treating that short loading state as "empty" made
  // the landing flash, then the viewport raced down to the latest message.
  // Keeping the message surface mounted lets ChatMessageList anchor directly
  // to the restored transcript instead.
  const isEmpty = sessionId === null && items.length === 0 && !hasOlder && !streaming && !busy && !imageBusy;
  const isRestoringTranscript = sessionId !== null && loadedSessionId !== sessionId;

  // Flatten every tool-call group (in order) into the execution timeline.
  const execSteps = items.flatMap((it) => (it.kind === "tools" ? it.calls : []));
  // A plan still in progress forces the execution panel to be showable even
  // when the user has execution_panel_enabled off — see the "plan_updated"
  // handler above. Scoped to "in progress" (not just "a plan was ever set
  // this session") so the bypass doesn't outlive the task it was for: once
  // every step is done and the agent isn't busy, visibility reverts to
  // purely following the user's saved preference.
  const planInProgress = Boolean(
    plan && (busy || plan.steps.some((s) => s.status !== "done")),
  );

  const toggleExec = (v: boolean) => {
    setExecOpen(v);
    localStorage.setItem("claw_exec_open", v ? "1" : "0");
  };

  const renderArtifacts = (paths?: string[]) => (
    sessionId && visibleArtifacts(paths).length > 0 && (
      <ArtifactList sessionId={sessionId} paths={paths} render={(p) => {
          const href = fileUrl(sessionId, p);
          // Show images (e.g. a generated chart) inline; keep
          // everything else as an openable chip.
          if (PREVIEWABLE_TABLE_RE.test(p)) {
            return (
              <TablePreview key={p} sessionId={sessionId} path={p} href={href} />
            );
          }
          if (PREVIEWABLE_HTML_RE.test(p)) {
            return (
              <HtmlPreview key={p} sessionId={sessionId} path={p} href={href} />
            );
          }
          if (PREVIEWABLE_DOCUMENT_RE.test(p) || /\.(mp3|wav|ogg|m4a|mp4|webm|mov)$/i.test(p)) {
            return (
              <DocumentPreview key={p} sessionId={sessionId} path={p} href={href} />
            );
          }
          if (/\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(p)) {
            const name = p.split("/").pop() ?? p;
            return (
              <a
                key={p}
                className="claw-artifact-image"
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                title={t("chat.artifact.view", { name: p })}
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
          // Everything else is a download: show a card with a
          // clear per-type icon and badge so a non-technical
          // user can tell at a glance what each file is.
          const meta = artifactTypeMeta(p);
          const name = p.split("/").pop() ?? p;
          return (
            <div key={p} className="claw-artifact-card-row">
              <a
                className={`claw-artifact-card claw-artifact-card--${meta.className}`}
                href={href}
                download={name}
                title={t("chat.artifact.download", { name: p })}
              >
                <span className="claw-artifact-card-icon">
                  <Icon icon={meta.icon} size="md" color="secondary" />
                </span>
                <span className="claw-artifact-card-body">
                  <span className="claw-artifact-name">{name}</span>
                  <span className="claw-artifact-card-type">
                    <span className="claw-artifact-card-badge">{meta.label}</span>
                    <span className="claw-artifact-card-action">{t("chat.artifact.downloadAction")}</span>
                  </span>
                </span>
                <Icon icon={DownloadIcon} size="sm" color="secondary" />
              </a>
              <SaveToBlueprintButton sessionId={sessionId} path={p} />
            </div>
          );
        }} />
    )
  );

  const greeting = (
    <div className="claw-greeting">
      <div className="sbot-greeting-identity">
        {groupName ? <Text type="display-2">{groupName}</Text> : bot ? (
          <>
            <BotAvatar variant={avatarVariantFor(bot)} size={60} title={bot.name} working={busy} />
            <Text type="display-2" className="sbot-greeting-name">{bot.name}</Text>
            <div className="sbot-greeting-meta">
              <span className="sbot-badge-cos">{bot.role_title}</span>
              {bot.kind === "chief_of_staff" && (
                <Text size="sm" color="secondary">หัวหน้าทีม AI กระจายและควบคุมงาน</Text>
              )}
            </div>
          </>
        ) : <SoftnixLogo height={44} slot="chat" />}
      </div>
      <Text type="display-2" className="sbot-greeting-message">
          {new Date().getHours() < 12 ? t("chat.greeting.morning") : t("chat.greeting.hello")}
        {userName ? t("chat.greeting.withName", { name: userName }) : t("chat.greeting.anonymous")}
      </Text>
    </div>
  );

  return (
    <div className="claw-chat-shell">
    <div className={`claw-chat${isEmpty ? " claw-chat--empty" : ""}`}>
      {!sessionId && <div className="claw-mode-switcher-landing"><ModeSwitcher /></div>}
      {!isEmpty && (executionPanelEnabled || planInProgress) && !execOpen && (
        <IconButton
          label={t("chat.exec.show")}
          icon={<Icon icon={PanelRight} size="sm" />}
          variant="ghost"
          clickAction={() => toggleExec(true)}
          className="claw-exec-toggle"
        />
      )}
      <ChatLayout
        ref={chatLayoutRef}
        emptyState={isEmpty ? greeting : undefined}
        composer={
          <div className="claw-composer">
            {error && <ErrorText>{error}</ErrorText>}
            {attachments.length > 0 && (
              <div className="claw-attach-row">
                {attachments.map((a, i) => (
                  <span key={a.path} className="claw-attach-chip">
                    <Icon icon={a.blueprint ? FileType : a.is_image ? ImageIcon : FileIcon} size="sm" color="secondary" />
                    {a.name}
                    {a.blueprint && <span className="claw-attach-kind">{t("chat.composer.blueprint")}</span>}
                    <IconButton
                      label={t("chat.composer.removeAttachment")}
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
                  ? t("chat.composer.imagePlaceholder")
                  : isEmpty
                    ? t("chat.composerEmpty")
                    : t("chat.composerPlaceholder")
              }
              isDisabled={imageMode && imageBusy}
              input={
                <ChatComposerInput handleRef={composerHandleRef} triggers={composerTriggers} />
              }
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
                      <span className="claw-image-mode-back-label">{t("chat.composer.backToChat")}</span>
                    </button>
                  </div>
                ) : (
                <div className="claw-composer-actions">
                  <Popover
                    label={t("chat.composer.add")}
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
                              <span>{t("chat.composer.addFiles")}</span>
                            </button>
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => setPlusView("blueprints")}
                            >
                              <Icon icon={FileType} size="sm" color="secondary" />
                              <span>{t("chat.composer.blueprints")}</span>
                              <Icon icon={ChevronRight} size="sm" color="secondary" />
                            </button>
                            <div className="claw-plus-divider" />
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => setPlusView("skills")}
                            >
                              <Icon icon={BookOpen} size="sm" color="secondary" />
                              <span>{t("chat.composer.skills")}</span>
                              <Icon icon={ChevronRight} size="sm" color="secondary" />
                            </button>
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => setPlusView("connectors")}
                            >
                              <Icon icon={Plug} size="sm" color="secondary" />
                              <span>{t("chat.composer.connectors")}</span>
                              <Icon icon={ChevronRight} size="sm" color="secondary" />
                            </button>
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => setPlusView("knowledge")}
                            >
                              <Icon icon={Library} size="sm" color="secondary" />
                              <span>{t("chat.composer.knowledge")}</span>
                              <Icon icon={ChevronRight} size="sm" color="secondary" />
                            </button>
                            <button
                              type="button"
                              className="claw-plus-item"
                              onClick={() => {
                                if (imageModels.length === 0) {
                                  toast({
                                    body: t("chat.error.imageGenUnavailable"),
                                    type: "info",
                                  });
                                  return;
                                }
                                setImageMode(true);
                                setPlusOpen(false);
                              }}
                            >
                              <Icon icon={ImageIcon} size="sm" color="secondary" />
                              <span>{t("chat.composer.createImage")}</span>
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
                              <span>{t("chat.composer.skills")}</span>
                            </button>
                            <div className="claw-plus-divider" />
                            {skills.length === 0 ? (
                              <div className="claw-plus-empty">
                                <Text size="sm" color="secondary">
                                  {t("chat.composer.noSkills")}
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
                              <span>{t("chat.composer.manageSkills")}</span>
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
                              <span>{t("chat.composer.connectors")}</span>
                            </button>
                            <div className="claw-plus-divider" />
                            {connectors.length === 0 ? (
                              <div className="claw-plus-empty">
                                <Text size="sm" color="secondary">
                                  {t("chat.composer.noConnectors")}
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
                              <span>{t("chat.composer.manageConnectors")}</span>
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
                              <span>{t("chat.composer.knowledge")}</span>
                            </button>
                            <div className="claw-plus-divider" />
                            {knowledge.length === 0 ? (
                              <div className="claw-plus-empty">
                                <Text size="sm" color="secondary">
                                  {t("chat.composer.noKnowledge")}
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
                              <span>{t("chat.composer.manageKnowledge")}</span>
                            </button>
                          </>
                        )}
                        {plusView === "blueprints" && (
                          <>
                            <button
                              type="button"
                              className="claw-plus-item claw-plus-back"
                              onClick={() => setPlusView("root")}
                            >
                              <Icon icon={ArrowLeft} size="sm" color="secondary" />
                              <span>{t("chat.composer.blueprints")}</span>
                            </button>
                            <div className="claw-plus-divider" />
                            {blueprints.length === 0 ? (
                              <div className="claw-plus-empty">
                                <Text size="sm" color="secondary">{t("chat.composer.noBlueprints")}</Text>
                              </div>
                            ) : (
                              blueprints.map((blueprint) => (
                                <button
                                  key={blueprint.id}
                                  type="button"
                                  className="claw-plus-item"
                                  onClick={() => void selectBlueprint(blueprint)}
                                >
                                  <Icon icon={FileType} size="sm" color="secondary" />
                                  <span>{blueprint.name}</span>
                                  <span className="claw-plus-count">v{blueprint.current_version}</span>
                                </button>
                              ))
                            )}
                            <div className="claw-plus-divider" />
                            <button
                              type="button"
                              className="claw-plus-item claw-plus-manage"
                              onClick={() => manageSettings("blueprints")}
                            >
                              <Icon icon={ExternalLink} size="sm" color="secondary" />
                              <span>{t("chat.composer.manageBlueprints")}</span>
                            </button>
                          </>
                        )}
                      </div>
                    }
                  >
                    <IconButton
                      label={uploading ? t("chat.composer.uploading") : t("chat.composer.add")}
                      icon={<Icon icon={Plus} size="sm" />}
                      variant="ghost"
                      isDisabled={uploading}
                    />
                  </Popover>
                  <Popover
                    label={t("chat.composer.permissionMode")}
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
                              <span className="claw-perm-option-name">{t(opt.labelKey)}</span>
                              <span className="claw-perm-option-desc">{t(opt.descKey)}</span>
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
                        {permission === "ask" ? t("chat.permission.ask.label") : t("chat.permission.auto.label")}
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
                          ? t("chat.mic.stop")
                          : micState === "transcribing"
                            ? t("chat.mic.transcribing")
                            : t("chat.mic.dictate")
                      }
                      title={
                        micState === "recording"
                          ? t("chat.mic.stop")
                          : micState === "transcribing"
                            ? t("chat.mic.transcribing")
                            : t("chat.mic.dictate")
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
                      label={t("chat.model.selectImage")}
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
                              {t("chat.model.selectImage")}
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
                                    <span className="claw-model-option-private">{t("chat.model.private")}</span>
                                  )}
                                  <span className={`claw-cost claw-cost-${m.cost}`}>{t(COST_LABEL_KEY[m.cost])}</span>
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
                          {imageModels.find((m) => m.model_id === imageModel)?.label ?? t("chat.model.fallback")}
                        </span>
                        <Icon icon={ChevronDown} size="xsm" color="secondary" />
                      </button>
                    </Popover>
                  ) : undefined
                ) : models.length > 0 ? (
                  <Popover
                    label={t("chat.model.select")}
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
                            {t("chat.model.select")}
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
                                  <span className="claw-model-option-private">{t("chat.model.private")}</span>
                                )}
                                <span className={`claw-cost claw-cost-${m.cost}`}>{t(COST_LABEL_KEY[m.cost])}</span>
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
                          <span>{t("chat.model.manage")}</span>
                        </button>
                      </div>
                    }
                  >
                    <button type="button" className="claw-model-trigger">
                      <Icon icon={Box} size="sm" color="secondary" />
                      <span className="claw-model-trigger-label">
                        {models.find((m) => m.model_id === model)?.label ?? t("chat.model.fallback")}
                      </span>
                      <Icon icon={ChevronDown} size="xsm" color="secondary" />
                    </button>
                  </Popover>
                ) : undefined
              }
            />
            <LocalWorkspacePicker sessionId={sessionId} ensureSession={onRequireSession} disabled={busy || Boolean(running)} />
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
                      label={t("chat.common.close")}
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
        {isEmpty || isRestoringTranscript ? null : (
        <div className="claw-column">
          <ChatMessageList
            className="claw-message-list"
            density="spacious"
            // Undefined when the history is exhausted: passing a callback also
            // renders the sentinel, and a live one at the top of a fully-loaded
            // transcript would keep firing a no-op.
            scrollToTopAction={hasOlder ? loadOlder : undefined}
          >
            {items.map((item, i) =>
              // Tool steps render both inline (here) and in the right Execution diagram.
              item.kind === "tools" ? (
                transcriptCalls(item.calls).length === 0 ? null : (
                <ChatToolCalls
                  key={i}
                  calls={transcriptCalls(item.calls).map((c, j) => {
                    // While a long tool (e.g. workflow) is running, surface its live
                    // sub-step here too — not just in the (opt-in, off-by-default)
                    // Execution panel — so this row doesn't look stuck.
                    const activeSub =
                      c.status === "running" && c.substeps && c.substeps.length > 0
                        ? (c.substeps.find((s) => s.status === "running") ?? c.substeps[c.substeps.length - 1])
                        : undefined;
                    const counter =
                      c.status === "running" && c.stepIndex && c.stepTotal
                        ? t("exec.stepCounter", { index: String(c.stepIndex), total: String(c.stepTotal) })
                        : undefined;
                    const liveTarget = activeSub
                      ? counter
                        ? `${counter} · ${activeSub.label}`
                        : activeSub.label
                      : c.argsPreview;
                    return {
                      key: `${i}-${j}`,
                      name: c.name,
                      status: c.status,
                      target: liveTarget,
                      duration: c.duration,
                      errorMessage: c.status === "error" ? c.resultPreview : undefined,
                      resultDetail: c.resultPreview ? <Text size="sm">{c.resultPreview}</Text> : undefined,
                    };
                  })}
                />
                )
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
                      {confirmTitle(t, item.row.tool, item.row.status)}
                    </Text>
                  </div>
                  {item.row.argsPreview && (
                    <pre className="claw-confirm-cmd">{item.row.argsPreview}</pre>
                  )}
                  {item.row.status === "pending" && (
                    <div className="claw-confirm-actions">
                      <Button
                        label={t("chat.confirm.decline")}
                        variant="secondary"
                        size="sm"
                        clickAction={() => sendDecision(item.row.requestId, false)}
                      >
                        {t("chat.confirm.decline")}
                      </Button>
                      <Button
                        label={t("chat.confirm.approve")}
                        variant="primary"
                        size="sm"
                        clickAction={() => sendDecision(item.row.requestId, true)}
                      >
                        {t("chat.confirm.approve")}
                      </Button>
                    </div>
                  )}
                </div>
              ) : item.kind === "message" && item.speakerBotId ? (
                // A teammate's reply, shown as that bot speaking rather than as
                // the leader's tool output — the handoff is the product here.
                (() => {
                  const speaker = speakerOf(bots, item);
                  return (
                    <div key={i} className="sbot-handoff">
                      {item.delegatedTask && (
                        <div className="sbot-handoff-task">
                          <Icon icon={ArrowRightLeft} size="sm" color="secondary" />
                          <span className="sbot-handoff-task-body">
                            <Text size="sm" color="secondary">
                              {t("chat.delegation.assigned", { name: speaker.name })}
                            </Text>
                            <Text size="sm">{item.delegatedTask}</Text>
                          </span>
                        </div>
                      )}
                      <ChatMessage
                        sender="assistant"
                        avatar={<BotAvatar variant={speaker.variant} size={28} />}
                      >
                        <ChatMessageBubble
                          variant="ghost"
                          className="claw-msg-bubble"
                          name={
                            <span className="sbot-handoff-who">
                              <Text size="sm" weight="semibold">
                                {speaker.name}
                              </Text>
                              {speaker.role && (
                                <Text size="sm" color="secondary">
                                  {speaker.role}
                                </Text>
                              )}
                            </span>
                          }
                        >
                          {item.content && (item.speakerError || item.taskResult) && (
                            // The reply is real but was cut short. Unmarked it
                            // reads as a specialist that simply trailed off.
                            <Text size="sm" color="secondary">
                              {t(item.taskResult
                                ? item.taskResult.failure_reason === "waiting_for_user"
                                  ? "chat.result.waiting"
                                  : `chat.result.${item.taskResult.status}`
                                : "chat.delegation.partial")}
                              {item.taskResult?.verification_status === "passed" &&
                                ` · ${t("chat.result.verified")}`}
                            </Text>
                          )}
                          {item.content ? (
                            <Markdown>{sanitizeModelMarkdown(item.content)}</Markdown>
                          ) : (
                            // Still working: the assignment is on screen with a
                            // name attached, so the wait reads as someone being
                            // busy rather than as the app hanging.
                            <span className="sbot-handoff-working">
                              <Spinner size="sm" />
                              <Text size="sm" color="secondary">
                                {t("chat.delegation.working")}
                              </Text>
                            </span>
                          )}
                          {(item.liveSteps?.length || item.liveText) &&
                            (() => {
                              const key = item.delegationId ?? "";
                              const open = openWork[key] ?? !item.content;
                              return (
                                <div className="sbot-handoff-work">
                                  <button
                                    type="button"
                                    className="sbot-handoff-work-toggle"
                                    onClick={() =>
                                      setOpenWork((p) => ({ ...p, [key]: !open }))
                                    }
                                  >
                                    <Icon
                                      icon={open ? ChevronDown : ChevronRight}
                                      size="sm"
                                      color="secondary"
                                    />
                                    <Text size="sm" color="secondary">
                                      {t(
                                        item.content
                                          ? "chat.delegation.workDone"
                                          : "chat.delegation.work",
                                        { name: speaker.name },
                                      )}
                                    </Text>
                                  </button>
                                  {open && (
                                    <div className="sbot-handoff-work-body">
                                      {item.liveSteps?.map((s) => (
                                        <div key={s.index} className="sbot-handoff-step">
                                          <span
                                            className={`sbot-handoff-step-dot sbot-handoff-step-dot--${s.status}`}
                                          />
                                          <span className="sbot-handoff-step-tool">{s.tool}</span>
                                          {s.detail && (
                                            <span className="sbot-handoff-step-detail">
                                              {s.detail}
                                            </span>
                                          )}
                                        </div>
                                      ))}
                                      {!item.content && item.liveText && (
                                        // A preview of the reply being written,
                                        // not the reply: `delegation_finished`
                                        // carries the whole thing and takes over
                                        // the bubble above. Plain text on
                                        // purpose — half a markdown table
                                        // renders as garbage.
                                        <div className="sbot-handoff-draft">
                                          <Text size="sm" color="secondary">
                                            {item.liveText}
                                          </Text>
                                        </div>
                                      )}
                                    </div>
                                  )}
                                </div>
                              );
                            })()}
                        </ChatMessageBubble>
                        {renderArtifacts(item.artifacts)}
                        {item.content && (
                          <ChatMessageMetadata
                            footer={
                              <div className="claw-feedback">
                                <IconButton
                                  label={
                                    copied[i] ? t("chat.msg.copied") : t("chat.msg.copyResponse")
                                  }
                                  icon={
                                    <Icon
                                      icon={copied[i] ? Check : Copy}
                                      size="sm"
                                      color={copied[i] ? "success" : "secondary"}
                                    />
                                  }
                                  variant="ghost"
                                  size="sm"
                                  clickAction={() => copyMessage(i, item.content)}
                                />
                              </div>
                            }
                          />
                        )}
                      </ChatMessage>
                    </div>
                  );
                })()
              ) : (
                <ChatMessage key={i} sender={item.role === "user" ? "user" : "assistant"}>
                  {item.role === "assistant" ? (
                    <>
                      <ChatMessageBubble variant="ghost" className="claw-msg-bubble">
                        <Markdown>{sanitizeModelMarkdown(item.content)}</Markdown>
                      </ChatMessageBubble>
                      {/* Intermediate files (the script that built the PDF, the
                          JSON/XML it read along the way) are not deliverables,
                          so the strip is filtered first and only rendered if
                          anything survives — gating on the raw list instead
                          leaves an empty, still-margined div behind. */}
                      {renderArtifacts(item.artifacts)}
                      <ChatMessageMetadata
                        footer={
                          <div className="claw-feedback">
                            <IconButton
                              label={copied[i] ? t("chat.msg.copied") : t("chat.msg.copyResponse")}
                              icon={<Icon icon={copied[i] ? Check : Copy} size="sm" color={copied[i] ? "success" : "secondary"} />}
                              variant="ghost"
                              size="sm"
                              clickAction={() => copyMessage(i, item.content)}
                            />
                            {ttsEnabled && (
                              <IconButton
                                label={speaking[i] === "playing" ? t("chat.msg.stopReading") : t("chat.msg.readAloud")}
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
                              label={t("chat.msg.good")}
                              icon={
                                <Icon icon={ThumbsUp} size="sm" color={feedback[i] === "up" ? "success" : "secondary"} />
                              }
                              variant="ghost"
                              size="sm"
                              clickAction={() => rate(i, item.content, "up")}
                            />
                            <IconButton
                              label={t("chat.msg.bad")}
                              icon={
                                <Icon icon={ThumbsDown} size="sm" color={feedback[i] === "down" ? "error" : "secondary"} />
                              }
                              variant="ghost"
                              size="sm"
                              clickAction={() => rate(i, item.content, "down")}
                            />
                            {sessionId && (
                              <IconButton
                                label={t("chat.msg.share")}
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
                      {item.delegatedBy && (
                        <div className="sbot-delegated-by">
                          <Icon icon={ArrowRightLeft} size="sm" color="secondary" />
                          <Text size="sm" color="secondary">
                            {/* Named if the leader is still on the team, and
                                still captioned if it is not — the point is that
                                the user did not write this, which holds either
                                way. */}
                            {speakerOf(bots, { speakerBotId: item.delegatedBy }).name
                              ? t("chat.delegation.from", {
                                  name: speakerOf(bots, { speakerBotId: item.delegatedBy }).name,
                                })
                              : t("chat.delegation.fromTeam")}
                          </Text>
                        </div>
                      )}
                      <ChatMessageBubble>{item.content}</ChatMessageBubble>
                      <ChatMessageMetadata
                        footer={
                          <div className="claw-feedback">
                            <IconButton
                              label={copied[i] ? t("chat.msg.copied") : t("chat.msg.copyMessage")}
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
                    <Spinner size="sm" shade="subtle" />{" "}
                    {(() => {
                      const elapsed = formatElapsed(busyStartedAtRef.current);
                      const running = findRunningCall(items);
                      return running
                        ? t("chat.msg.workingOn", { label: describeRunningCall(running), elapsed })
                        : t("chat.msg.thinkingElapsed", { elapsed });
                    })()}
                  </span>
                </ChatMessageBubble>
              </ChatMessage>
            )}
            {imageBusy && (
              <ChatMessage sender="assistant">
                <ChatMessageBubble variant="ghost" className="claw-msg-bubble">
                  <span className="claw-thinking">
                    <Spinner size="sm" shade="subtle" /> {t("chat.msg.generatingImage")}
                  </span>
                </ChatMessageBubble>
              </ChatMessage>
            )}
            {/* Pinned last so it reads as the current state. A mission outlives
                the turn that planned it, so by the time it is working there is
                no `busy` and no streaming text — without this the screen looks
                idle while the team is mid-job. */}
            {missions.map((m) => {
              // `awaiting` reflects live node state, not the mission's own
              // (possibly momentarily stale) status column — a mission just
              // picked back up after a restart can read `status === "running"`
              // for one poll cycle while a gate is still genuinely open. Keying
              // off `awaiting` directly means a real gate is never masked by
              // that transition. `blocked` with nothing awaiting is a stalled
              // mission (e.g. a stuck step recovering from a restart) rather
              // than something the user needs to act on.
              const waitingOnGate = m.awaiting.length > 0;
              const stalled = m.status === "blocked" && !waitingOnGate;
              const queued = m.status === "queued";
              const paused = m.status === "paused";
              return (
              <ChatMessage key={m.id} sender="assistant">
                <ChatMessageBubble variant="ghost" className="claw-msg-bubble">
                  <Text size="sm" weight="semibold">{m.goal}</Text>
                  <span className="claw-thinking">
                    {waitingOnGate || queued || paused ? (
                      <Icon icon={Hourglass} size="sm" color="secondary" />
                    ) : stalled ? (
                      <Icon icon={ShieldAlert} size="sm" color="secondary" />
                    ) : (
                      <Spinner size="sm" shade="subtle" />
                    )}{" "}
                    {t(
                      queued ? "chat.mission.queued" : paused ? "chat.mission.paused" : waitingOnGate
                        ? "chat.mission.blocked"
                        : stalled
                          ? "chat.mission.stalled"
                          : "chat.mission.running",
                      { done: String(m.done), total: String(m.total) },
                    )}
                  </span>
                  <div className="claw-mission-steps">
                    {m.running.map((step) => {
                      // Same lookup the delegation card uses, so a specialist
                      // wears the same avatar whether the leader handed it work
                      // directly or a mission scheduled it.
                      const speaker = speakerOf(bots, {
                        speakerBotId: step.bot_id,
                        speakerName: step.bot_name,
                      });
                      return (
                        <div key={step.node_id} className="claw-mission-step">
                          <BotAvatar variant={speaker.variant} size={18} />
                          <Text size="sm" color="secondary">
                            {t("chat.mission.step", {
                              // A bot archived mid-mission has no name left, but
                              // its step is still running and still worth naming.
                              bot: speaker.name || step.bot_id,
                              title: step.title,
                            })}
                          </Text>
                          <span className="claw-mission-step-badge" title={t("chat.msg.thinking")}>
                            <Icon icon={Loader2} size="xsm" />
                          </span>
                        </div>
                      );
                    })}
                    {m.awaiting.map((gate) => (
                      <Text key={gate.node_id} size="sm" color="secondary">
                        {t("chat.mission.awaiting", { title: gate.title })}
                      </Text>
                    ))}
                    {Boolean(m.queued?.length) && (
                      <details>
                        <summary>{t("chat.mission.pending", { count: String(m.queued?.length) })}</summary>
                        {m.queued?.map((step) => (
                          <Text key={step.node_id} size="sm" color="secondary">
                            {step.bot_name} · {step.title}
                          </Text>
                        ))}
                      </details>
                    )}
                    {m.running.length === 0 && m.awaiting.length === 0 && !m.queued?.length && !queued && !paused && (
                      <Text size="sm" color="secondary">{t("chat.mission.idle")}</Text>
                    )}
                  </div>
                </ChatMessageBubble>
              </ChatMessage>
              );
            })}
          </ChatMessageList>
        </div>
        )}
      </ChatLayout>
    </div>
      {!isEmpty && (executionPanelEnabled || planInProgress) && execOpen && (
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
