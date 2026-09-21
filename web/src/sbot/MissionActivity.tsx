import { useEffect, useState, type ReactNode } from 'react';
import type { MissionActivityData, MissionHandoff } from './api';
import { useT } from '../branding';

function durationText(milliseconds: number | null) {
  if (milliseconds == null) return '—';
  const seconds = Math.max(0, milliseconds) / 1000;
  if (seconds < 1) return '<1s';
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function MissionActivityCard({ activity, handoff, id, artifacts, result }: {
  activity?: MissionActivityData; handoff?: MissionHandoff; id?: string; artifacts?: ReactNode; result?: ReactNode;
}) {
  const t = useT();
  const hasRunningTool = activity?.steps.some((step) => step.status === 'running') ?? false;
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    if (!hasRunningTool) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasRunningTool]);
  if (handoff) return <div className="sbot-mission-activity" id={`activity-${id}`}>
    <strong>{handoff.title}</strong>
    <a href={`/chat/sbot/${encodeURIComponent(handoff.session_id)}#activity-${handoff.activity_id}`}>
      {t('chat.activity.open')} · {handoff.bot_name}
    </a>
  </div>;
  if (!activity) return null;
  const a = activity;
  return <section className="sbot-mission-activity" id={`activity-${id}`} aria-label={a.title}>
    <header><strong>{a.title}</strong><span role="status">{t(`chat.activity.${a.status}`)}</span></header>
    <small>{a.bot_name} · {t('chat.activity.attempt')} {a.attempt}</small>
    <details>
      <summary>{t('chat.activity.assignment')} · {a.leader_name}</summary>
      <p className="sbot-mission-prose">{a.instruction}</p>
      {a.inputs.length > 0 && <p>{t('chat.activity.inputs')}: {a.inputs.join(', ')}</p>}
      {a.required_files.length > 0 && <p>{t('chat.activity.outputs')}: {a.required_files.join(', ')}</p>}
      {a.depends_on.length > 0 && <p>{t('chat.activity.dependencies')}: {a.depends_on.join(', ')}</p>}
    </details>
    {a.text && <details open={a.status === 'running'}>
      <summary title={t('chat.activity.recent')}>{a.bot_name} · {t('chat.activity.updates')}</summary>
      <p className="sbot-mission-prose">{a.text}</p>
    </details>}
    {a.steps.length > 0 && <details open={a.status === 'running'}>
      <summary title={t('chat.activity.recent')}>{t('chat.activity.tools')} · {a.steps.length} · {a.steps[a.steps.length - 1].tool}</summary>
      <ol className="sbot-mission-tools">{a.steps.map((s, i) => {
        const startedAt = s.started_at || s.at;
        const startedMs = new Date(startedAt).getTime();
        const elapsed = s.duration_ms ?? (
          s.status === 'running' && Number.isFinite(startedMs) ? clock - startedMs : null
        );
        return <li key={`${startedAt}-${i}`} className={`sbot-mission-tool sbot-mission-tool--${s.status}`}>
          <details open={s.status === 'running' || s.status === 'error'}>
            <summary>
              <span className="sbot-mission-tool-name">{s.tool}</span>
              <span className="sbot-mission-tool-status">{t(`chat.activity.${s.status}`)}</span>
              <span className="sbot-mission-tool-duration">{durationText(elapsed)}</span>
            </summary>
            <div className="sbot-mission-tool-body">
              {s.args_preview && <div>
                <span>{t('chat.activity.parameters')}</span>
                <code>{s.args_preview}</code>
              </div>}
              {s.result_preview && <div>
                <span>{t('chat.activity.toolResult')}</span>
                <code>{s.result_preview}</code>
              </div>}
              <time dateTime={startedAt}>{t('chat.activity.started')} {new Date(startedAt).toLocaleTimeString()}</time>
            </div>
          </details>
        </li>;
      })}</ol>
    </details>}
    {a.result && <div className="sbot-mission-result">{result}</div>}
    {a.result_truncated && <small>{t('chat.activity.resultTruncated')}</small>}
    {artifacts}
    <footer><time dateTime={a.updated_at}>{t('chat.activity.updated')} {new Date(a.updated_at).toLocaleString()}</time>
      {a.origin_session && <a href={`/chat/sbot/${encodeURIComponent(a.origin_session)}`}>{t('chat.activity.leader')}</a>}
    </footer>
  </section>;
}
