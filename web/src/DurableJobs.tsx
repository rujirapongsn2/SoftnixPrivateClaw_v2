import { useEffect, useRef, useState } from 'react';
import { ApiError, api, type DurableJob } from './api';

const labels: Record<string, [string, string]> = {
  queued: ['Queued', 'รอทำงาน'], running: ['Working', 'กำลังทำงาน'],
  waiting_dependency: ['Waiting for service recovery', 'รอบริการกลับมาพร้อมใช้งาน'],
  awaiting_input: ['Needs your input', 'รอข้อมูลจากคุณ'], paused: ['Paused', 'พักงาน'],
  completed: ['Completed', 'เสร็จแล้ว'], failed: ['Failed', 'ทำงานไม่สำเร็จ'], cancelled: ['Cancelled', 'ยกเลิกแล้ว'],
  dependency_unavailable: ['Service unavailable', 'บริการยังไม่พร้อม'],
  resource_limit: ['Organization safety limit reached', 'ถึงขอบเขตความปลอดภัยขององค์กร'],
  recovery_contract_missing: ['Recovery needs a verified plan', 'ต้องตรวจแผนก่อนกู้คืนงาน'],
  delivery_verification_required: ['Delivery needs verification', 'ต้องตรวจสอบการส่งผลลัพธ์'],
  uncertain_effect: ['Action outcome needs verification', 'ต้องตรวจสอบผลคำสั่งก่อนทำต่อ'],
  missing_deliverable: ['Output file was not created', 'ยังไม่ได้สร้างไฟล์ผลลัพธ์'],
  validation_failed: ['Result did not pass validation', 'ผลลัพธ์ยังไม่ผ่านการตรวจสอบ'],
  approval_required: ['Action requires approval', 'คำสั่งต้องได้รับอนุมัติ'],
  no_progress: ['No verified progress', 'ยังไม่พบความคืบหน้าที่ตรวจสอบได้'],
  dependency_wait_expired: ['Service unavailable for too long', 'บริการไม่พร้อมเกินระยะเวลารอ'],
  source_changed: ['Source changed; results need review', 'ต้นฉบับเปลี่ยนแปลง ต้องตรวจผลใหม่'],
  source_coverage_incomplete: ['Source has not been fully read', 'ยังอ่านต้นฉบับไม่ครบ'],
  permission_denied: ['Access is no longer available', 'ไม่มีสิทธิ์ดำเนินงานต่อ'],
  executor_error: ['Execution needs investigation', 'ต้องตรวจสอบข้อผิดพลาดการทำงาน'],
  strategy_exhausted: ['Repeated failure; approach needs review', 'วิธีเดิมล้มเหลวซ้ำ ต้องทบทวนแนวทาง'],
  recovery_exhausted: ['Automatic recovery did not succeed', 'กู้คืนอัตโนมัติไม่สำเร็จ'],
  checkpoint_expired: ['Recovery data expired', 'ข้อมูลสำหรับกู้คืนหมดอายุ'],
  unsupported_checkpoint: ['Worker update required', 'ต้องปรับรุ่น worker เพื่อทำงานต่อ'],
  slice_timeout: ['Continuing from checkpoint', 'ทำต่อจากจุดที่บันทึกไว้'],
  worker_lost: ['Recovering interrupted work', 'กำลังกู้คืนงานที่หยุดชะงัก'],
  job_suspended: ['Waiting for the job to resume', 'รอให้งานหลักกลับมาทำต่อ'],
  user_cancelled: ['Cancelled by you', 'คุณยกเลิกงานแล้ว'],
};

export function DurableJobs({ sessionId, onDelivery }: {
  sessionId: string | null; onDelivery: () => Promise<boolean>;
}) {
  const [jobs, setJobs] = useState<DurableJob[]>([]);
  const [error, setError] = useState('');
  const [pending, setPending] = useState<string[]>([]);
  const currentSession = useRef(sessionId);
  currentSession.current = sessionId;
  const callback = useRef(onDelivery);
  callback.current = onDelivery;
  useEffect(() => {
    setJobs([]); setError(''); setPending([]);
    if (!sessionId) return;
    let disposed = false;
    let delivered = '';
    let consecutiveFailures = 0;
    let lastKnownActive = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const rows = await api.listDurableJobs(sessionId);
        if (disposed) return;
        consecutiveFailures = 0;
        setJobs(rows); setError('');
        lastKnownActive = rows.some(j => ['queued', 'running', 'waiting_dependency'].includes(j.status));
        const signature = rows.filter(j => j.deliveries > 0).map(j => `${j.job_id}:${j.deliveries}`).join(',');
        if (signature && signature !== delivered && await callback.current()) delivered = signature;
      } catch (caught) {
        if (!disposed) {
          // During a staged rollout an older API may serve the new static UI.
          // The durable-jobs endpoint simply does not exist in that version;
          // stop polling until the next navigation/reload instead of creating
          // permanent background traffic for a feature that is not available.
          if (caught instanceof ApiError && caught.status === 404) return;
          consecutiveFailures += 1;
          // Status polling is advisory. A proxy handoff or server reload does
          // not prove the job or chat failed, even after several misses. Keep
          // the last verified card and retry quietly; real terminal job states
          // and failed user actions are surfaced through their own paths.
        }
      }
      if (!disposed) timer = setTimeout(poll,
        consecutiveFailures > 0 || lastKnownActive ? 2500 : 10000);
    };
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [sessionId]);
  if (!jobs.length && !error) return null;
  const terminal = (j: DurableJob) => ['completed', 'failed', 'cancelled'].includes(j.status);
  const visible = [...jobs.filter(j => !terminal(j)), ...jobs.filter(terminal).slice(0, 3)];
  const act = async (job: DurableJob, operation: () => Promise<DurableJob>) => {
    const session = sessionId;
    setPending(prev => [...prev, job.job_id]);
    try {
      const updated = await operation();
      if (currentSession.current === session) {
        setJobs(prev => prev.map(j => j.job_id === job.job_id ? updated : j));
        setError('');
      }
    } catch {
      if (currentSession.current === session) setError(job.locale === 'th'
        ? 'ดำเนินการไม่สำเร็จ กรุณารอสถานะล่าสุดแล้วลองใหม่' : 'Action failed. Wait for the latest status and try again.');
    } finally {
      if (currentSession.current === session) setPending(prev => prev.filter(id => id !== job.job_id));
    }
  };
  return <section className="claw-durable-jobs" aria-label="Background jobs">
    {visible.map(job => {
      const th = job.locale === 'th';
      const label = (value: string) => labels[value]?.[th ? 1 : 0] ?? (th ? 'ดูรายละเอียดสถานะ' : 'See status details');
      const done = job.steps.filter(s => s.status === 'completed').length;
      return <div className="claw-durable-job" key={job.job_id}>
        <div role="status" aria-live="polite"><strong>{label(job.status)}</strong> <span>{done}/{job.steps.length}</span></div>
        {!terminal(job) && <button type="button" disabled={pending.includes(job.job_id)}
          onClick={() => void act(job, () => api.controlDurableJob(job.job_id, 'cancel'))}>{th ? 'ยกเลิก' : 'Cancel'}</button>}
        {job.steps.filter(step => step.approval).map(step => <div key={step.id} className="claw-durable-approval">
          <strong>{step.approval!.tool}</strong>
          <pre>{step.approval!.arguments}</pre>
          {[true, false].map(approved => <button type="button" key={String(approved)} disabled={pending.includes(job.job_id)}
            onClick={() => void act(job, () => api.approveDurableStep(job.job_id, step.id, step.approval!.key, approved))}
          >{approved ? (th ? 'อนุมัติคำสั่ง' : 'Approve action') : (th ? 'ไม่อนุมัติ' : 'Decline')}</button>)}
        </div>)}
        <details><summary>{th ? 'รายละเอียด' : 'Details'}</summary>
          {job.reason && <p>{label(job.reason)}</p>}
          <ul>{job.steps.map(step => <li key={step.id}>{step.id} · {label(step.status)}</li>)}</ul>
          <small>{job.job_id}</small>
        </details>
      </div>;
    })}
    {error && <span role="alert">{error}</span>}
  </section>;
}
