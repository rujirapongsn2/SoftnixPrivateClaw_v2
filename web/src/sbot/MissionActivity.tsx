import type { ReactNode } from 'react';
import type { MissionActivityData, MissionHandoff } from './api';
import { useT } from '../branding';

export function MissionActivityCard({ activity, handoff, id, artifacts, result }: {
  activity?: MissionActivityData; handoff?: MissionHandoff; id?: string; artifacts?: ReactNode; result?: ReactNode;
}) {
  const t = useT();
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
    {a.steps.length > 0 && <details>
      <summary title={t('chat.activity.recent')}>{t('chat.activity.tools')} · {a.steps.length} · {a.steps[a.steps.length - 1].tool}</summary>
      <ul>{a.steps.map((s, i) => <li key={i}>
        <time dateTime={s.at}>{new Date(s.at).toLocaleTimeString()}</time> · {s.tool} · {t(`chat.activity.${s.status}`)}
      </li>)}</ul>
    </details>}
    {a.result && <div className="sbot-mission-result">{result}</div>}
    {a.result_truncated && <small>{t('chat.activity.resultTruncated')}</small>}
    {artifacts}
    <footer><time dateTime={a.updated_at}>{t('chat.activity.updated')} {new Date(a.updated_at).toLocaleString()}</time>
      {a.origin_session && <a href={`/chat/sbot/${encodeURIComponent(a.origin_session)}`}>{t('chat.activity.leader')}</a>}
    </footer>
  </section>;
}
