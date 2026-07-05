import { IconButton } from "@astryxdesign/core/IconButton";
import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";
import {
  Brain,
  FileText,
  GitBranch,
  Globe,
  Loader2,
  type LucideIcon,
  MessageSquare,
  Plug,
  Sparkles,
  Terminal,
  Wrench,
  X,
} from "lucide-react";
import { useState } from "react";

export interface ExecStep {
  name: string;
  status: "running" | "complete" | "error";
  argsPreview?: string;
  resultPreview?: string;
  duration?: string;
}

// Map a tool name to a small lane icon — the category still reads at a glance
// from the icon shape, without a noisy per-row text label.
function laneIcon(name: string): LucideIcon {
  const n = name.toLowerCase();
  if (/^exec$|shell|bash|sandbox|command/.test(n)) return Terminal;
  // Connector tools are namespaced `mcp_*` — match before the Web patterns,
  // since a connector name may itself contain "search"/"fetch".
  if (/^mcp_/.test(n)) return Plug;
  if (/web_search|web_fetch|browse|browser|fetch|search/.test(n)) return Globe;
  if (/read_|write_|edit_|list_dir|excel|csv|pdf|docx|file|upload|download|attach/.test(n))
    return FileText;
  if (/spawn|workflow|subagent|agent/.test(n)) return GitBranch;
  if (/remember|memory|skill/.test(n)) return Brain;
  return Wrench;
}

type RowStatus = "running" | "complete" | "error" | "pending";

interface Row {
  kind: "request" | "tool" | "response";
  title: string;
  detail?: string;
  result?: string;
  duration?: string;
  status: RowStatus;
  icon: LucideIcon;
  isError?: boolean;
  stepIndex?: number;
}

/** Minimal, live list of the agent's execution — one dense line per step
 * (status dot · lane icon · tool name · faint detail · duration), click to
 * expand a step's input/result. Replaces the earlier card-per-step diagram. */
export function ExecutionPanel({
  steps,
  running,
  onClose,
}: {
  steps: ExecStep[];
  running: boolean;
  onClose: () => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);

  const anyStepRunning = steps.some((s) => s.status === "running");
  const active = running || anyStepRunning;

  // Build the list: Request → each tool step → Response.
  const rows: Row[] = [];
  if (steps.length > 0 || running) {
    rows.push({ kind: "request", title: "Request", status: "complete", icon: MessageSquare });
  }
  steps.forEach((s, i) => {
    rows.push({
      kind: "tool",
      title: s.name,
      detail: s.argsPreview,
      result: s.resultPreview,
      duration: s.duration,
      status: s.status,
      icon: laneIcon(s.name),
      isError: s.status === "error",
      stepIndex: i,
    });
  });
  if (steps.length > 0 || running) {
    rows.push({
      kind: "response",
      title: running ? (anyStepRunning ? "Waiting…" : "Generating response…") : "Response",
      status: running ? (anyStepRunning ? "pending" : "running") : "complete",
      icon: Sparkles,
    });
  }

  return (
    <aside className="claw-exec-panel">
      <div className="claw-exec-header">
        <div className="claw-row">
          <Text weight="semibold">Execution</Text>
          {active ? (
            <span className="claw-exec-live">
              <Icon icon={Loader2} size="xsm" />
              live
            </span>
          ) : steps.length > 0 ? (
            <Text size="sm" color="secondary">
              {steps.length} step{steps.length === 1 ? "" : "s"}
            </Text>
          ) : null}
        </div>
        <IconButton
          label="Hide execution"
          icon={<Icon icon={X} size="sm" />}
          variant="ghost"
          size="sm"
          clickAction={onClose}
        />
      </div>

      {rows.length === 0 ? (
        <div className="claw-exec-empty">
          <Text size="sm" color="secondary">
            No activity yet — steps appear here while Claw works.
          </Text>
        </div>
      ) : (
        <div className="claw-xlist">
          {rows.map((row, i) => {
            const isTool = row.kind === "tool";
            const isOpen = isTool && expanded === row.stepIndex;
            const hasDetail = isTool && (row.detail || row.result);
            return (
              <div
                key={i}
                className={`claw-xrow${row.kind !== "tool" ? " claw-xrow--bookend" : ""}`}
              >
                <button
                  type="button"
                  className="claw-xrow-line"
                  disabled={!hasDetail}
                  onClick={() => hasDetail && setExpanded(isOpen ? null : row.stepIndex!)}
                >
                  <span className={`claw-xdot claw-xdot--${row.status}`} aria-hidden="true" />
                  <Icon icon={row.icon} size="xsm" color="secondary" />
                  <span className="claw-xname">{row.title}</span>
                  {row.detail && !isOpen && <span className="claw-xdetail">{row.detail}</span>}
                  {isTool && row.duration && <span className="claw-xdur">{row.duration}</span>}
                </button>
                {isOpen && (
                  <div className="claw-xexpand">
                    {row.detail && (
                      <>
                        <div className="claw-xexpand-label">Input</div>
                        <pre className="claw-xpre">{row.detail}</pre>
                      </>
                    )}
                    {row.result && (
                      <>
                        <div className="claw-xexpand-label">
                          {row.isError ? "Error" : "Result"}
                        </div>
                        <pre className={`claw-xpre${row.isError ? " claw-xpre--error" : ""}`}>
                          {row.result}
                        </pre>
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </aside>
  );
}
