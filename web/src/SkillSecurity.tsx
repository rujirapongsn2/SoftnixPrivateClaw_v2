// Skill Security UI: the OWASP Agentic Skills Top 10 review of a skill and the
// admin's share policy. Shared by Control Plane › Skill Security (review table,
// policy editor) and Settings › Skills (badges, report, import review step).
// The backend decides everything (claw/api/skill_security.py); the helpers here
// only mirror its rules so the UI can explain them before a request fails.
import { Badge, type BadgeVariant } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Icon } from "@astryxdesign/core/Icon";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useToast } from "@astryxdesign/core/Toast";
import { AlertTriangle, ChevronLeft, ChevronRight, FileSearch, RefreshCw, ShieldAlert } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { useT } from "./branding";
import { ErrorText } from "./ErrorText";
import {
  api,
  apiErrorMessage,
  type SkillAssessmentReport,
  type SkillFinding,
  type SkillMaxSeverity,
  type SkillPolicyImpact,
  type SkillPolicyLevel,
  type SkillSecurityListParams,
  type SkillSecurityPolicy,
  type SkillSecurityRow,
  type SkillSecurityVerdict,
  type SkillSeverity,
  type SkillShareScope,
  type SkillVisibility,
} from "./shared-api";

type T = ReturnType<typeof useT>;

const AST_IDS = ["AST01", "AST02", "AST03", "AST04", "AST05", "AST06", "AST07", "AST08", "AST09", "AST10"];
// Most severe first: the order of the summary tiles, the filter chips and the policy matrix rows.
const SEVERITIES: SkillMaxSeverity[] = ["critical", "high", "medium", "low", "none"];
const POLICY_LEVELS: SkillPolicyLevel[] = ["critical", "high", "medium", "low"];
const SCOPES: SkillShareScope[] = ["public", "group", "owner", "blocked"];
const SCOPE_RANK: Record<SkillShareScope, number> = { public: 0, group: 1, owner: 2, blocked: 3 };
// The scope a visibility needs (mirrors claw/skills/security_policy.py::_VISIBILITY_NEEDS).
const VISIBILITY_NEEDS: Record<SkillVisibility, SkillShareScope> = { public: "public", group: "group", private: "owner" };
const PAGE_SIZE = 25;
const OWASP_URL = "https://github.com/OWASP/www-project-agentic-skills-top-10";

const SEVERITY_VARIANT: Record<SkillMaxSeverity | SkillSeverity | "pending", BadgeVariant> = {
  critical: "red",
  high: "orange",
  medium: "yellow",
  low: "blue",
  info: "neutral",
  none: "green",
  pending: "neutral",
};

/** Whether a skill limited to `scope` may be shared at `visibility` (security_policy.permits). */
export function permitsVisibility(scope: SkillShareScope, visibility: SkillVisibility): boolean {
  return SCOPE_RANK[scope] <= SCOPE_RANK[VISIBILITY_NEEDS[visibility]];
}

/** Why the share selector can't offer `visibility`, or null when it can. A skill still waiting
 * for its review is not refused here: saving runs the review and the server decides. */
export function shareBlockedReason(t: T, security: SkillSecurityVerdict | null | undefined, visibility: SkillVisibility): string | null {
  if (!security || visibility === "private" || security.reason === "pending") return null;
  if (permitsVisibility(security.scope, visibility)) return null;
  if (security.reason === "blocked") return t("skillsec.share.blockedByAdmin");
  return t(security.reason === "exception" ? "skillsec.share.cannotException" : "skillsec.share.cannot", {
    severity: severityLabel(t, security.max_severity ?? "none"),
    codes: security.ast_codes.join(", ") || "—",
    scope: t(`skillsec.scope.${security.scope}`),
  });
}

function severityLabel(t: T, severity: SkillMaxSeverity | SkillSeverity | "pending"): string {
  return t(`skillsec.severity.${severity}`);
}

// Long AST lists are clipped in badges; the report shows them all.
function codesLabel(codes: string[]): string {
  return codes.length > 3 ? `${codes.slice(0, 3).join(", ")} +${codes.length - 3}` : codes.join(", ");
}

/** "High · AST05", "No findings" or "Review pending" — the one-glance verdict for lists. */
export function SkillSecurityBadge({ security }: { security: SkillSecurityVerdict | null | undefined }) {
  const t = useT();
  if (!security) return null;
  const severity = security.reason === "pending" || !security.max_severity ? "pending" : security.max_severity;
  const label =
    severity === "pending" || severity === "none" || security.ast_codes.length === 0
      ? severityLabel(t, severity)
      : `${severityLabel(t, severity)} · ${codesLabel(security.ast_codes)}`;
  return <Badge variant={SEVERITY_VARIANT[severity]} label={label} />;
}

type RowStatus = "blocked" | "requested" | "exception" | "suspended" | "usable";

function rowStatus(row: SkillSecurityRow): RowStatus {
  if (row.policy.scope === "blocked") return "blocked";
  if (row.decision === "requested") return "requested";
  if (row.policy.reason === "exception") return "exception";
  return permitsVisibility(row.policy.scope, row.visibility) ? "usable" : "suspended";
}

const STATUS_VARIANT: Record<RowStatus, BadgeVariant> = {
  blocked: "error",
  requested: "purple",
  exception: "info",
  suspended: "warning",
  usable: "success",
};

function formatTime(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

function sourceLabel(t: T, source: string | null): string {
  if (!source) return t("skillsec.source.written");
  if (source.startsWith("https://github.com/")) return t("skillsec.source.github");
  return t("skillsec.source.zip");
}

// ---------------------------------------------------------------- report

function FindingItem({ finding }: { finding: SkillFinding }) {
  const t = useT();
  const where = finding.line > 0 ? `${finding.file}:${finding.line}` : finding.file;
  return (
    <li className="claw-skillsec-finding">
      <div className="claw-skillsec-finding-head">
        <span className="claw-skillsec-ast">{finding.ast}</span>
        <Badge variant={SEVERITY_VARIANT[finding.severity]} label={severityLabel(t, finding.severity)} />
        <Text weight="semibold" className="claw-skillsec-finding-title">{finding.title}</Text>
        {finding.count > 1 && <Text size="sm" color="secondary">×{finding.count}</Text>}
      </div>
      <Text size="sm" color="secondary" className="claw-skillsec-finding-meta">
        {finding.rule} · <code>{where}</code>
      </Text>
      {finding.excerpt && <pre className="claw-skillsec-excerpt">{finding.excerpt}</pre>}
    </li>
  );
}

function DecisionState({ report }: { report: SkillAssessmentReport }) {
  const t = useT();
  const d = report.decision;
  if (!d) return null;
  const label =
    d.decision === "allow"
      ? t("skillsec.decision.allow", { scope: t(`skillsec.scope.${d.scope ?? "owner"}`) })
      : t(`skillsec.decision.${d.decision}`);
  return (
    <div className="claw-skillsec-box">
      <div className="claw-row">
        <Badge variant={d.decision === "block" ? "error" : d.decision === "allow" ? "info" : d.decision === "reject" ? "neutral" : "purple"} label={label} />
        <Text size="sm" color="secondary">{formatTime(d.updated_at)}</Text>
      </div>
      {!d.current && (
        <Text size="sm" as="p" className="claw-skillsec-stale">
          <Icon icon={AlertTriangle} size="xsm" /> {t("skillsec.decision.stale")}
        </Text>
      )}
      {d.request_reason && (
        <Text size="sm" as="p">
          <strong>{t("skillsec.decision.requestReason")}</strong> {d.request_reason}
        </Text>
      )}
      {d.admin_note && (
        <Text size="sm" as="p">
          <strong>{t("skillsec.decision.adminNote")}</strong> {d.admin_note}
        </Text>
      )}
    </div>
  );
}

// The owner may ask for an exception when the policy (not a block or an approval) limits the share
// and no request for this content is already waiting or approved.
function canRequestException(report: SkillAssessmentReport): boolean {
  if (report.status === "pending" || report.policy.reason !== "policy" || report.policy.scope === "public") return false;
  const d = report.decision;
  if (d?.decision === "block") return false;
  return !(d && d.current && (d.decision === "requested" || d.decision === "allow"));
}

function OwnerRequestForm({ report, onReport }: { report: SkillAssessmentReport; onReport: (r: SkillAssessmentReport) => void }) {
  const t = useT();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const tooShort = reason.trim().length < 10;
  return (
    <div className="claw-skillsec-box">
      <Text weight="semibold" display="block">{t("skillsec.request.title")}</Text>
      <Text size="sm" color="secondary" as="p">{t("skillsec.request.desc")}</Text>
      <TextArea label={t("skillsec.request.reason")} value={reason} onChange={setReason} rows={3} maxLength={2000} isDisabled={busy} />
      {error && <ErrorText>{error}</ErrorText>}
      <div className="claw-row">
        <Button
          label={t("skillsec.request.submit")}
          size="sm"
          isDisabled={busy || tooShort}
          tooltip={tooShort ? t("skillsec.request.minLength") : undefined}
          clickAction={async () => {
            setBusy(true);
            setError("");
            try {
              onReport(await api.requestSkillException(report.id, reason.trim()));
              setReason("");
            } catch (e) {
              setError(apiErrorMessage(e));
            } finally {
              setBusy(false);
            }
          }}
        />
      </div>
    </div>
  );
}

function AdminActions({ report, onReport }: { report: SkillAssessmentReport; onReport: (r: SkillAssessmentReport) => void }) {
  const t = useT();
  const [note, setNote] = useState("");
  // An exception usually exists to allow the share the owner already chose.
  const [scope, setScope] = useState<Exclude<SkillShareScope, "blocked">>(
    report.visibility === "public" ? "public" : "group",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const noNote = !note.trim();
  const run = async (fn: () => Promise<SkillAssessmentReport>) => {
    setBusy(true);
    setError("");
    try {
      onReport(await fn());
      setNote("");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const decide = (decision: "allow" | "reject" | "block" | "clear") =>
    run(() => api.adminSkillSecurityDecide(report.id, {
        decision,
        note: note.trim(),
        // The version this admin read: the server refuses the approval if the skill changed since.
        ...(decision === "allow" ? { scope, sha256: report.sha256 ?? "" } : {}),
      }));
  const d = report.decision;
  return (
    <div className="claw-skillsec-box">
      <div className="claw-row claw-row-between">
        <Text weight="semibold">{t("skillsec.admin.actions")}</Text>
        <Button
          label={t("skillsec.admin.rescan")}
          icon={<Icon icon={RefreshCw} size="sm" />}
          size="sm"
          variant="secondary"
          isDisabled={busy}
          clickAction={() => run(() => api.adminSkillSecurityRescan(report.id))}
        />
      </div>
      <TextArea
        label={t("skillsec.admin.note")}
        description={t("skillsec.admin.noteDesc")}
        value={note}
        onChange={setNote}
        rows={2}
        maxLength={2000}
        isDisabled={busy}
      />
      <Text size="sm" color="secondary">{t("skillsec.admin.exceptionScope")}</Text>
      <SegmentedControl label={t("skillsec.admin.exceptionScope")} value={scope} onChange={(v) => setScope(v as typeof scope)} size="sm" isDisabled={busy}>
        <SegmentedControlItem value="public" label={t("skillsec.scope.public")} />
        <SegmentedControlItem value="group" label={t("skillsec.scope.group")} />
        <SegmentedControlItem value="owner" label={t("skillsec.scope.owner")} />
      </SegmentedControl>
      {error && <ErrorText>{error}</ErrorText>}
      <div className="claw-row claw-skillsec-actions">
        <Button
          label={t("skillsec.admin.approve")}
          size="sm"
          isDisabled={busy || noNote || report.status === "pending"}
          tooltip={noNote ? t("skillsec.admin.noteRequired") : undefined}
          clickAction={() => decide("allow")}
        />
        {d?.decision === "requested" && (
          <Button label={t("skillsec.admin.reject")} size="sm" variant="secondary" isDisabled={busy} clickAction={() => decide("reject")} />
        )}
        <Button
          label={t("skillsec.admin.block")}
          size="sm"
          variant="destructive"
          isDisabled={busy || noNote}
          tooltip={noNote ? t("skillsec.admin.noteRequired") : undefined}
          clickAction={() => decide("block")}
        />
        {d && <Button label={t("skillsec.admin.clear")} size="sm" variant="ghost" isDisabled={busy} clickAction={() => decide("clear")} />}
      </div>
    </div>
  );
}

function ReportBody({
  report,
  admin,
  canRequest,
  onReport,
}: {
  report: SkillAssessmentReport;
  admin: boolean;
  canRequest: boolean;
  onReport: (r: SkillAssessmentReport) => void;
}) {
  const t = useT();
  const findings = report.findings ?? [];
  // info-level items (e.g. "references external URLs") are listed but are not findings.
  const real = findings.filter((f) => f.severity !== "info");
  const found = new Set(real.map((f) => f.ast));
  const quiet = AST_IDS.filter((id) => !found.has(id) && !(id in report.platform));
  const severity = report.status === "pending" ? "pending" : report.max_severity ?? "none";
  const policy = report.policy;
  return (
    <div className="claw-skillsec-report">
      <div className="claw-skillsec-meta">
        {report.review_failed ? (
          <Badge variant="error" label={t("skillsec.severity.failed")} />
        ) : (
          <Badge variant={SEVERITY_VARIANT[severity]} label={severityLabel(t, severity)} />
        )}
        {report.status === "outdated" && <Badge variant="warning" label={t("skillsec.report.outdated")} />}
        <Text size="sm" color="secondary">{sourceLabel(t, report.source)}</Text>
        {report.sha256 && (
          <Text size="sm" color="secondary">
            sha256 <code title={report.sha256}>{report.sha256.slice(0, 12)}</code>
          </Text>
        )}
        {report.checked_at && <Text size="sm" color="secondary">{t("skillsec.report.checked", { time: formatTime(report.checked_at) })}</Text>}
        {report.scanner_version !== undefined && (
          <Text size="sm" color="secondary">{t("skillsec.report.scanner", { version: String(report.scanner_version) })}</Text>
        )}
      </div>
      {report.source?.startsWith("https://") && (
        <Text size="sm" color="secondary" className="claw-skillsec-source">
          <a href={report.source} target="_blank" rel="noreferrer noopener">{report.source}</a>
        </Text>
      )}
      {report.status === "pending" && (
        <Text size="sm" as="p">{t(report.review_failed ? "skillsec.report.failedDesc" : "skillsec.report.pendingDesc")}</Text>
      )}
      {report.status === "outdated" && <Text size="sm" as="p">{t("skillsec.report.outdatedDesc")}</Text>}
      <div className="claw-skillsec-box">
        <Text size="sm" as="p">
          {t(`skillsec.report.policy.${policy.reason}`, { scope: t(`skillsec.scope.${policy.scope}`) })}
        </Text>
        <Text size="sm" color="secondary" as="p">
          {t("skillsec.report.sharedAs", { visibility: t(`skillsec.visibility.${report.visibility}`) })}
          {!permitsVisibility(policy.scope, report.visibility) && policy.reason !== "pending" && ` · ${t("skillsec.status.suspended")}`}
        </Text>
      </div>
      <DecisionState report={report} />
      {report.status !== "pending" && (
        <section className="claw-skillsec-findings">
          <Text weight="semibold" display="block">{t("skillsec.report.findings", { count: String(real.length) })}</Text>
          {findings.length === 0 ? (
            <Text size="sm" color="secondary" as="p">{t("skillsec.severity.none")}</Text>
          ) : (
            <ul>{findings.map((f, i) => <FindingItem key={`${f.rule}-${f.file}-${f.line}-${i}`} finding={f} />)}</ul>
          )}
        </section>
      )}
      <div className="claw-skillsec-platform">
        {Object.entries(report.platform).map(([id, status]) => (
          <span key={id} className="claw-skillsec-chip">
            <span className="claw-skillsec-ast">{id}</span> {t(`skillsec.platform.${status}`)}
          </span>
        ))}
      </div>
      {report.status !== "pending" && quiet.length > 0 && (
        <Text size="sm" color="secondary" as="p">{t("skillsec.report.noFindingsFor", { codes: quiet.join(", ") })}</Text>
      )}
      {!admin && canRequest && canRequestException(report) && <OwnerRequestForm report={report} onReport={onReport} />}
      {admin && <AdminActions key={report.id} report={report} onReport={onReport} />}
      <Text size="sm" color="secondary" as="p" className="claw-skillsec-credit">
        {t("skillsec.report.creditPrefix")}{" "}
        <a href={OWASP_URL} target="_blank" rel="noreferrer noopener">OWASP Agentic Skills Top 10</a>{" "}
        (CC BY-SA 4.0). {t("skillsec.report.notSafe")}
      </Text>
    </div>
  );
}

/** The review of one skill. Admin mode reads the admin endpoint and adds the decision actions;
 * otherwise it is the owner/reader view (`canRequest` = the viewer owns it). */
export function SkillReportDialog({
  skillId,
  isOpen,
  onOpenChange,
  admin = false,
  canRequest = false,
  onChanged,
}: {
  skillId: string | null;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  admin?: boolean;
  canRequest?: boolean;
  onChanged?: () => void;
}) {
  const t = useT();
  const [report, setReport] = useState<SkillAssessmentReport | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!isOpen || !skillId) return;
    let live = true;
    setReport(null);
    setError("");
    (admin ? api.adminSkillSecurityReport(skillId) : api.skillAssessment(skillId))
      .then((r) => live && setReport(r))
      .catch((e) => live && setError(apiErrorMessage(e)));
    return () => {
      live = false;
    };
  }, [admin, isOpen, skillId]);
  const onReport = (r: SkillAssessmentReport) => {
    // The admin decision endpoints leave owner_name out; keep the one already shown.
    setReport((prev) => ({ ...r, owner_name: r.owner_name ?? prev?.owner_name }));
    onChanged?.();
  };
  return (
    // Kept mounted with only `isOpen` toggling, like SkillDetailModal in Settings.tsx.
    <Dialog isOpen={isOpen} onOpenChange={onOpenChange} width={720} purpose="info">
      <Layout
        header={
          <DialogHeader
            title={report?.name ?? t("skillsec.report.title")}
            subtitle={report?.owner_name ? t("skillsec.report.owner", { name: report.owner_name }) : t("skillsec.report.title")}
            startContent={<Icon icon={FileSearch} size="md" />}
            onOpenChange={onOpenChange}
          />
        }
        content={
          <LayoutContent>
            {error ? (
              <ErrorText>{error}</ErrorText>
            ) : report ? (
              <ReportBody report={report} admin={admin} canRequest={canRequest} onReport={onReport} />
            ) : (
              <Text color="secondary">{t("admin.common.loading")}</Text>
            )}
          </LayoutContent>
        }
        footer={
          <LayoutFooter hasDivider>
            <Button label={t("settings.common.close")} variant="ghost" clickAction={() => onOpenChange(false)} />
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}

/** Open/close state for one SkillReportDialog; the last id is kept so the dialog's close
 * transition still has its content. */
export function useReportDialog() {
  const [id, setId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const show = useCallback((skillId: string) => {
    setId(skillId);
    setOpen(true);
  }, []);
  return { id, open, setOpen, show };
}

// ---------------------------------------------------------------- import result step

const IMPORT_POLL_MS = 1000;
const IMPORT_POLL_LIMIT_MS = 10_000;

/** After a ZIP/GitHub import: each imported skill's review, polled while still pending (about a
 * second apart, ten seconds at most). Informational only — the import is already saved. */
export function ImportReviewStep({ skills, onDone }: { skills: { id: string; name: string }[]; onDone: () => void }) {
  const t = useT();
  const [reports, setReports] = useState<Record<string, SkillAssessmentReport | undefined>>({});
  const [timedOut, setTimedOut] = useState(false);
  const dialog = useReportDialog();
  const ids = skills.map((s) => s.id).join(",");
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const started = Date.now();
    const latest: Record<string, SkillAssessmentReport | undefined> = {};
    const poll = async () => {
      const due = ids.split(",").filter((id) => !latest[id] || latest[id]?.status === "pending");
      const results = await Promise.all(due.map((id) => api.skillAssessment(id).catch(() => undefined)));
      if (!live) return;
      due.forEach((id, i) => {
        if (results[i]) latest[id] = results[i];
      });
      setReports({ ...latest });
      const stillPending = ids.split(",").some((id) => !latest[id] || latest[id]?.status === "pending");
      if (!stillPending) return;
      if (Date.now() - started >= IMPORT_POLL_LIMIT_MS) setTimedOut(true);
      else timer = setTimeout(() => void poll(), IMPORT_POLL_MS);
    };
    void poll();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [ids]);
  return (
    <div className="claw-panel">
      <Text weight="semibold">{t("skillsec.import.title")}</Text>
      <Text size="sm" color="secondary" as="p">{t("skillsec.import.desc")}</Text>
      <div className="claw-skill-list">
        {skills.map((s) => {
          const r = reports[s.id];
          const pending = !r || r.status === "pending";
          const top = (r?.findings ?? []).filter((f) => f.severity !== "info").slice(0, 3);
          return (
            <Card key={s.id} padding={2}>
              <div className="claw-skillsec-import-card">
                <div className="claw-row">
                  <Text weight="semibold">{s.name}</Text>
                  {pending ? (
                    <Badge variant="neutral" label={timedOut ? t("skillsec.severity.pending") : t("skillsec.import.checking")} />
                  ) : (
                    <SkillSecurityBadge security={r.policy} />
                  )}
                </div>
                {r && !pending && (
                  <>
                    {top.length === 0 ? (
                      <Text size="sm" color="secondary" as="p">{t("skillsec.severity.none")}</Text>
                    ) : (
                      <ul className="claw-skillsec-import-findings">
                        {top.map((f, i) => (
                          <li key={`${f.rule}-${i}`}>
                            <span className="claw-skillsec-ast">{f.ast}</span> {severityLabel(t, f.severity)} — {f.title}
                          </li>
                        ))}
                      </ul>
                    )}
                    {r.policy.scope !== "public" && (
                      <Text size="sm" color="secondary" as="p">
                        {t(`skillsec.report.policy.${r.policy.reason}`, { scope: t(`skillsec.scope.${r.policy.scope}`) })}
                      </Text>
                    )}
                    <div className="claw-row">
                      <Button label={t("skillsec.viewReport")} size="sm" variant="ghost" icon={<Icon icon={FileSearch} size="sm" />} clickAction={() => dialog.show(s.id)} />
                    </div>
                  </>
                )}
              </div>
            </Card>
          );
        })}
      </div>
      {timedOut && <Text size="sm" color="secondary" as="p">{t("skillsec.import.timedOut")}</Text>}
      <div className="claw-row">
        <Button label={t("skillsec.import.done")} clickAction={onDone} />
      </div>
      <SkillReportDialog skillId={dialog.id} isOpen={dialog.open} onOpenChange={dialog.setOpen} canRequest />
    </div>
  );
}

// ---------------------------------------------------------------- admin: review table

type Filters = {
  severity: string[];
  ast: string;
  visibility: SkillSecurityListParams["visibility"];
  status: SkillSecurityListParams["status"];
};
const NO_FILTERS: Filters = { severity: [], ast: "", visibility: "", status: "" };

function SummaryTiles({
  summary,
  filters,
  onToggleSeverity,
  onToggleRequested,
}: {
  summary: Awaited<ReturnType<typeof api.adminSkillSecuritySummary>> | null;
  filters: Filters;
  onToggleSeverity: (severity: string) => void;
  onToggleRequested: () => void;
}) {
  const t = useT();
  const tiles: (SkillMaxSeverity | "pending")[] = [...SEVERITIES, "pending"];
  return (
    <div className="claw-skillsec-tiles">
      {tiles.map((s) => (
        <button
          key={s}
          type="button"
          className={`claw-skillsec-tile claw-skillsec-tile--${s}`}
          aria-pressed={filters.severity.includes(s)}
          onClick={() => onToggleSeverity(s)}
        >
          <span className="claw-skillsec-tile-count">{(summary?.by_severity[s] ?? 0).toLocaleString()}</span>
          <span className="claw-skillsec-tile-label">{severityLabel(t, s)}</span>
        </button>
      ))}
      <button
        type="button"
        className="claw-skillsec-tile claw-skillsec-tile--requested"
        aria-pressed={filters.status === "requested"}
        onClick={onToggleRequested}
      >
        <span className="claw-skillsec-tile-count">{(summary?.requested ?? 0).toLocaleString()}</span>
        <span className="claw-skillsec-tile-label">{t("skillsec.status.requested")}</span>
      </button>
    </div>
  );
}

// The label feeds the stacked phone layout (td::before); the div keeps a cell's content one flex item.
function Cell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <td data-label={label}>
      <div>{children}</div>
    </td>
  );
}

function ReviewTab() {
  const t = useT();
  const [summary, setSummary] = useState<Awaited<ReturnType<typeof api.adminSkillSecuritySummary>> | null>(null);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{ total: number; items: SkillSecurityRow[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // Bumped after a decision so the table and tiles refetch once, not per row.
  const [reloadKey, setReloadKey] = useState(0);
  const dialog = useReportDialog();
  const seq = useRef(0);

  useEffect(() => {
    void api.adminSkillSecuritySummary().then(setSummary).catch((e) => setError(apiErrorMessage(e)));
  }, [reloadKey]);

  // Debounced search; any filter change starts again from the first page.
  useEffect(() => {
    const next = search.trim();
    if (next === query) return;
    const timer = setTimeout(() => {
      setQuery(next);
      setOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [search, query]);
  const updateFilters = (fn: (f: Filters) => Filters) => {
    setFilters(fn);
    setOffset(0);
  };

  useEffect(() => {
    const mine = ++seq.current;
    setLoading(true);
    api
      .adminSkillSecurityList({ ...filters, q: query, offset, limit: PAGE_SIZE })
      .then((r) => {
        if (mine !== seq.current) return; // a newer filter's response wins
        setPage({ total: r.total, items: r.items });
        setError("");
      })
      .catch((e) => mine === seq.current && setError(apiErrorMessage(e)))
      .finally(() => mine === seq.current && setLoading(false));
  }, [filters, query, offset, reloadKey]);

  const toggleSeverity = (s: string) =>
    updateFilters((f) => ({ ...f, severity: f.severity.includes(s) ? f.severity.filter((x) => x !== s) : [...f.severity, s] }));
  const total = page?.total ?? 0;
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + PAGE_SIZE, total);
  const filtered = filters.severity.length > 0 || !!filters.ast || !!filters.visibility || !!filters.status || query !== "";

  return (
    <div className="claw-panel">
      <SummaryTiles
        summary={summary}
        filters={filters}
        onToggleSeverity={toggleSeverity}
        onToggleRequested={() => updateFilters((f) => ({ ...f, status: f.status === "requested" ? "" : "requested" }))}
      />
      <div className="claw-skillsec-filters">
        <TextInput label={t("skillsec.filter.search")} value={search} onChange={setSearch} placeholder={t("skillsec.filter.searchPlaceholder")} />
        <div className="claw-token-controls">
          <select
            className="claw-token-filter"
            aria-label={t("skillsec.filter.ast")}
            value={filters.ast}
            onChange={(e) => updateFilters((f) => ({ ...f, ast: e.target.value }))}
          >
            <option value="">{t("skillsec.filter.allAst")}</option>
            {AST_IDS.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
          <select
            className="claw-token-filter"
            aria-label={t("skillsec.filter.sharing")}
            value={filters.visibility}
            onChange={(e) => updateFilters((f) => ({ ...f, visibility: e.target.value as Filters["visibility"] }))}
          >
            <option value="">{t("skillsec.filter.allSharing")}</option>
            {(["private", "group", "public"] as const).map((v) => <option key={v} value={v}>{t(`skillsec.visibility.${v}`)}</option>)}
          </select>
          <select
            className="claw-token-filter"
            aria-label={t("skillsec.filter.status")}
            value={filters.status}
            onChange={(e) => updateFilters((f) => ({ ...f, status: e.target.value as Filters["status"] }))}
          >
            <option value="">{t("skillsec.filter.allStatus")}</option>
            {(["requested", "allow", "reject", "block"] as const).map((s) => <option key={s} value={s}>{t(`skillsec.filter.decision.${s}`)}</option>)}
          </select>
          {filtered && (
            <Button label={t("skillsec.filter.clear")} size="sm" variant="ghost" clickAction={() => { updateFilters(() => NO_FILTERS); setSearch(""); }} />
          )}
        </div>
        <div className="claw-row claw-skillsec-chips" role="group" aria-label={t("skillsec.filter.severity")}>
          {[...SEVERITIES, "pending" as const].map((s) => (
            <Button
              key={s}
              label={severityLabel(t, s)}
              size="sm"
              variant={filters.severity.includes(s) ? "primary" : "secondary"}
              clickAction={() => toggleSeverity(s)}
            />
          ))}
        </div>
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      {page && page.items.length === 0 ? (
        <EmptyState title={t("skillsec.table.empty")} description={t(filtered ? "skillsec.table.emptyFiltered" : "skillsec.table.emptyDesc")} />
      ) : (
        <div className={`claw-skillsec-table${loading ? " is-loading" : ""}`}>
          <table>
            <thead>
              <tr>
                <th>{t("skillsec.table.skill")}</th>
                <th>{t("skillsec.table.sharing")}</th>
                <th>{t("skillsec.table.severity")}</th>
                <th>{t("skillsec.table.status")}</th>
                <th>{t("skillsec.table.checked")}</th>
              </tr>
            </thead>
            <tbody>
              {(page?.items ?? []).map((row) => {
                const status = rowStatus(row);
                return (
                  <tr key={row.id} onClick={() => dialog.show(row.id)}>
                    <Cell label={t("skillsec.table.skill")}>
                      <button type="button" className="claw-skillsec-link" onClick={(e) => { e.stopPropagation(); dialog.show(row.id); }}>
                        {row.name}
                      </button>
                      <span className="claw-skillsec-sub">{row.owner_name || "—"}</span>
                    </Cell>
                    <Cell label={t("skillsec.table.sharing")}>
                      {t(`skillsec.visibility.${row.visibility}`)}
                      {row.subscribers > 0 && (
                        <span className="claw-skillsec-sub">
                          {t(row.subscribers === 1 ? "skillsec.table.subscriber" : "skillsec.table.subscribers", { count: row.subscribers.toLocaleString() })}
                        </span>
                      )}
                    </Cell>
                    <Cell label={t("skillsec.table.severity")}>
                      {row.review_failed ? (
                        <Badge variant="error" label={t("skillsec.severity.failed")} />
                      ) : (
                        <SkillSecurityBadge security={row.policy} />
                      )}
                    </Cell>
                    <Cell label={t("skillsec.table.status")}>
                      <Badge variant={STATUS_VARIANT[status]} label={t(`skillsec.status.${status}`)} />
                    </Cell>
                    <Cell label={t("skillsec.table.checked")}><span className="claw-skillsec-sub">{formatTime(row.checked_at)}</span></Cell>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {total > 0 && (
        <div className="claw-row claw-row-between claw-skillsec-pager">
          <Text size="sm" color="secondary">
            {t("skillsec.table.range", { first: first.toLocaleString(), last: last.toLocaleString(), total: total.toLocaleString() })}
          </Text>
          <div className="claw-row">
            <Button
              label={t("skillsec.table.previous")}
              icon={<Icon icon={ChevronLeft} size="sm" />}
              size="sm"
              variant="secondary"
              isDisabled={loading || offset === 0}
              clickAction={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            />
            <Button
              label={t("skillsec.table.next")}
              endContent={<Icon icon={ChevronRight} size="sm" />}
              size="sm"
              variant="secondary"
              isDisabled={loading || last >= total}
              clickAction={() => setOffset(offset + PAGE_SIZE)}
            />
          </div>
        </div>
      )}
      <SkillReportDialog
        skillId={dialog.id}
        isOpen={dialog.open}
        onOpenChange={dialog.setOpen}
        admin
        onChanged={() => setReloadKey((k) => k + 1)}
      />
    </div>
  );
}

// ---------------------------------------------------------------- admin: policy editor

/** Sets `level` to `scope` and moves the other levels just enough to keep the rule that a more
 * severe level is never shared wider than a less severe one (security_policy.parse_policy). */
function withLevel(policy: SkillSecurityPolicy, level: SkillPolicyLevel, scope: SkillShareScope): SkillSecurityPolicy {
  const order: SkillPolicyLevel[] = ["low", "medium", "high", "critical"];
  const at = order.indexOf(level);
  const next = { ...policy, [level]: scope };
  order.forEach((l, i) => {
    if (i > at && SCOPE_RANK[next[l]] < SCOPE_RANK[scope]) next[l] = scope;
    if (i < at && SCOPE_RANK[next[l]] > SCOPE_RANK[scope]) next[l] = scope;
  });
  return next;
}

function samePolicy(a: SkillSecurityPolicy, b: SkillSecurityPolicy): boolean {
  return POLICY_LEVELS.every((l) => a[l] === b[l]);
}

function ImpactSentence({ impact }: { impact: SkillPolicyImpact }) {
  const t = useT();
  const n = (v: number) => v.toLocaleString();
  return (
    <Text size="sm" as="p">
      {t("skillsec.policy.impact", {
        blocked: n(impact.blocked_skills),
        suspended: n(impact.suspended_shares),
        subscribers: n(impact.affected_subscribers),
        restored: n(impact.restored_shares),
      })}
    </Text>
  );
}

function PolicyTab() {
  const t = useT();
  const toast = useToast();
  const [saved, setSaved] = useState<SkillSecurityPolicy | null>(null);
  const [defaults, setDefaults] = useState<SkillSecurityPolicy | null>(null);
  const [draft, setDraft] = useState<SkillSecurityPolicy | null>(null);
  const [impact, setImpact] = useState<SkillPolicyImpact | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .adminSkillSecurityPolicy()
      .then((r) => {
        setSaved(r.policy);
        setDraft(r.policy);
        setDefaults(r.defaults);
      })
      .catch((e) => setError(apiErrorMessage(e)));
  }, []);

  const dirty = !!draft && !!saved && !samePolicy(draft, saved);
  // Debounced preview of the draft; a newer draft aborts the older request.
  useEffect(() => {
    setImpact(null);
    if (!draft || !dirty) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api
        .adminSkillSecurityPreview(draft, controller.signal)
        .then((r) => {
          setImpact(r);
          setError("");
        })
        .catch((e) => !controller.signal.aborted && setError(apiErrorMessage(e)));
    }, 400);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [draft, dirty]);

  if (!draft || !saved) return error ? <ErrorText>{error}</ErrorText> : <Text color="secondary">{t("admin.common.loading")}</Text>;

  return (
    <div className="claw-panel">
      <div className="claw-card-heading">
        <Text weight="semibold">{t("skillsec.policy.title")}</Text>
        <Text size="sm" color="secondary">{t("skillsec.policy.desc")}</Text>
      </div>
      <div className="claw-skillsec-matrix">
        <table>
          <thead>
            <tr>
              <th scope="col">{t("skillsec.policy.level")}</th>
              {SCOPES.map((s) => <th key={s} scope="col">{t(`skillsec.scope.${s}`)}</th>)}
            </tr>
          </thead>
          <tbody>
            {POLICY_LEVELS.map((level) => (
              <tr key={level}>
                <th scope="row">
                  <Badge variant={SEVERITY_VARIANT[level]} label={severityLabel(t, level)} />
                </th>
                {SCOPES.map((scope) => (
                  <td key={scope}>
                    <input
                      type="radio"
                      name={`skillsec-policy-${level}`}
                      aria-label={`${severityLabel(t, level)}: ${t(`skillsec.scope.${scope}`)}`}
                      checked={draft[level] === scope}
                      disabled={busy}
                      onChange={() => setDraft(withLevel(draft, level, scope))}
                    />
                  </td>
                ))}
              </tr>
            ))}
            <tr className="claw-skillsec-matrix-fixed">
              <th scope="row"><Badge variant={SEVERITY_VARIANT.none} label={severityLabel(t, "none")} /></th>
              <td colSpan={SCOPES.length}><Text size="sm" color="secondary">{t("skillsec.policy.noneAlwaysPublic")}</Text></td>
            </tr>
          </tbody>
        </table>
      </div>
      <Text size="sm" color="secondary" as="p">{t("skillsec.policy.orderRule")}</Text>
      <div className="claw-skillsec-box" aria-live="polite">
        {!dirty ? (
          <Text size="sm" color="secondary" as="p">{t("skillsec.policy.noChanges")}</Text>
        ) : impact ? (
          <ImpactSentence impact={impact} />
        ) : (
          <Text size="sm" color="secondary" as="p">{t("skillsec.policy.previewing")}</Text>
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
      <div className="claw-row">
        <Button
          label={t("skillsec.policy.save")}
          icon={<Icon icon="check" size="sm" />}
          isDisabled={busy || !dirty}
          clickAction={async () => {
            setBusy(true);
            try {
              const r = await api.adminSkillSecuritySavePolicy(draft);
              setSaved(r.policy);
              setDraft(r.policy);
              setError("");
              toast({
                body: t("skillsec.policy.saved", {
                  blocked: r.impact.blocked_skills.toLocaleString(),
                  suspended: r.impact.suspended_shares.toLocaleString(),
                  restored: r.impact.restored_shares.toLocaleString(),
                }),
                type: "info",
              });
            } catch (e) {
              setError(apiErrorMessage(e));
            } finally {
              setBusy(false);
            }
          }}
        />
        <Button label={t("skillsec.policy.discard")} variant="ghost" isDisabled={busy || !dirty} clickAction={() => setDraft(saved)} />
        {defaults && (
          <Button
            label={t("skillsec.policy.resetDefaults")}
            variant="secondary"
            isDisabled={busy || samePolicy(draft, defaults)}
            clickAction={() => setDraft(defaults)}
          />
        )}
      </div>
    </div>
  );
}

/** Control Plane › Skill Security. */
export function SkillSecurityAdminPanel() {
  const t = useT();
  const [tab, setTab] = useState<"review" | "policy">("review");
  return (
    <div className="claw-panel">
      <div className="claw-skillsec-intro">
        <Icon icon={ShieldAlert} size="sm" color="secondary" />
        <Text size="sm" color="secondary">{t("skillsec.admin.intro")}</Text>
      </div>
      <TabList value={tab} onChange={(v) => setTab(v as typeof tab)} hasDivider aria-label={t("admin.nav.skillSecurity")}>
        <Tab value="review" label={t("skillsec.admin.tabReview")} />
        <Tab value="policy" label={t("skillsec.admin.tabPolicy")} />
      </TabList>
      {tab === "review" ? <ReviewTab /> : <PolicyTab />}
    </div>
  );
}
