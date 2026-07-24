import { IconButton } from "@astryxdesign/core/IconButton";
import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";
import {
  Brain,
  Check,
  FileText,
  GitBranch,
  Globe,
  ListChecks,
  Loader2,
  type LucideIcon,
  MessageSquare,
  Plug,
  Sparkles,
  Terminal,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { WorkingPlan } from "./api";
import { useT } from "./branding";

export interface ExecSubStep {
  key: string;
  label: string;
  status: "running" | "complete" | "error";
}

export interface ExecStep {
  name: string;
  status: "running" | "complete" | "error";
  argsPreview?: string;
  resultPreview?: string;
  duration?: string;
  substeps?: ExecSubStep[];
  stepIndex?: number;
  stepTotal?: number;
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
  substeps?: ExecSubStep[];
  counter?: string;
}

/** Minimal, live list of the agent's execution — one dense line per step
 * (status dot · lane icon · tool name · faint detail · duration), click to
 * expand a step's input/result. Replaces the earlier card-per-step diagram. */
// Map a plan step's status to the shared status-dot modifier.
const PLAN_DOT: Record<string, RowStatus> = {
  done: "complete",
  in_progress: "running",
  pending: "pending",
};

export function ExecutionPanel({
  steps,
  plan,
  running,
  onClose,
}: {
  steps: ExecStep[];
  plan?: WorkingPlan | null;
  running: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const [expanded, setExpanded] = useState<number | null>(null);

  const planSteps = plan?.steps ?? [];
  const planDone = planSteps.filter((s) => s.status === "done").length;
  const hasPlan = Boolean(plan && (plan.goal || planSteps.length > 0));

  const anyStepRunning = steps.some((s) => s.status === "running");
  const active = running || anyStepRunning;

  // Live "it's alive" elapsed timer while the agent works — resets when idle.
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef<number | null>(null);
  useEffect(() => {
    if (!active) {
      startRef.current = null;
      setElapsed(0);
      return;
    }
    if (startRef.current == null) startRef.current = Date.now();
    const id = setInterval(
      () => setElapsed(Math.floor((Date.now() - (startRef.current ?? Date.now())) / 1000)),
      1000,
    );
    return () => clearInterval(id);
  }, [active]);
  const mmss = `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}`;

  // Build the list: Request → each tool step → Response.
  const rows: Row[] = [];
  if (steps.length > 0 || running) {
    rows.push({ kind: "request", title: t("exec.request"), status: "complete", icon: MessageSquare });
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
      substeps: s.substeps,
      counter:
        s.status === "running" && s.stepIndex && s.stepTotal
          ? t("exec.stepCounter", { index: String(s.stepIndex), total: String(s.stepTotal) })
          : undefined,
    });
  });
  if (steps.length > 0 || running) {
    rows.push({
      kind: "response",
      title: running
        ? anyStepRunning
          ? t("exec.waiting")
          : t("exec.generatingResponse")
        : t("exec.response"),
      status: running ? (anyStepRunning ? "pending" : "running") : "complete",
      icon: Sparkles,
    });
  }

  return (
    <aside className="claw-exec-panel">
      <div className="claw-exec-header">
        <div className="claw-row">
          <Text weight="semibold">{t("exec.title")}</Text>
          {active ? (
            <span className="claw-exec-live">
              <Icon icon={Loader2} size="xsm" />
              live · {mmss}
            </span>
          ) : steps.length > 0 ? (
            <Text size="sm" color="secondary">
              {t("exec.stepCount", { count: String(steps.length), plural: steps.length === 1 ? "" : "s" })}
            </Text>
          ) : null}
        </div>
        <IconButton
          label={t("exec.hide")}
          icon={<Icon icon={X} size="sm" />}
          variant="ghost"
          size="sm"
          clickAction={onClose}
        />
      </div>

      {hasPlan && (
        <div className="claw-plan-card">
          <div className="claw-plan-head">
            <Icon icon={ListChecks} size="xsm" color="secondary" />
            <span className="claw-plan-title">{t("exec.plan")}</span>
            {planSteps.length > 0 && (
              <span className="claw-plan-progress">
                {planDone}/{planSteps.length}
              </span>
            )}
          </div>
          {plan?.goal && <div className="claw-plan-goal">{plan.goal}</div>}
          {planSteps.length > 0 && (
            <div className="claw-plan-steps">
              {planSteps.map((s, i) => {
                const dot = PLAN_DOT[s.status] ?? "pending";
                return (
                  <div key={i} className={`claw-plan-step claw-plan-step--${dot}`}>
                    {s.status === "done" ? (
                      <Icon icon={Check} size="xsm" />
                    ) : (
                      <span className={`claw-xdot claw-xdot--${dot}`} aria-hidden="true" />
                    )}
                    <span className="claw-plan-step-label">{s.step}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {rows.length === 0 && !hasPlan ? (
        <div className="claw-exec-empty">
          <Text size="sm" color="secondary">
            {t("exec.empty")}
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
                  {row.counter && <span className="claw-xcounter">{row.counter}</span>}
                  {row.detail && !isOpen && !row.counter && (
                    <span className="claw-xdetail">{row.detail}</span>
                  )}
                  {isTool && row.duration && <span className="claw-xdur">{row.duration}</span>}
                </button>
                {row.substeps && row.substeps.length > 0 && (
                  <div className="claw-xsubs">
                    {row.substeps.map((s) => (
                      <div key={s.key} className="claw-xsub">
                        <span className={`claw-xdot claw-xdot--${s.status}`} aria-hidden="true" />
                        <span className={`claw-xsub-label${s.status === "running" ? " claw-xsub-label--active" : ""}`}>
                          {s.label}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {isOpen && (
                  <div className="claw-xexpand">
                    {row.detail && (
                      <>
                        <div className="claw-xexpand-label">{t("exec.input")}</div>
                        <pre className="claw-xpre">{row.detail}</pre>
                      </>
                    )}
                    {row.result && (
                      <>
                        <div className="claw-xexpand-label">
                          {row.isError ? t("exec.error") : t("exec.result")}
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
