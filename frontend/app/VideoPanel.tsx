'use client';
import { useEffect, useState } from 'react';
type Job = { id: string; status: string; stage: string; progress: number; error: string | null; video_url: string | null };
const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';
export default function VideoPanel({ briefId }: { briefId: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const id = localStorage.getItem(`daybrief-video-${briefId}`);
    if (id) fetch(`${API}/api/videos/${id}`).then(r => r.ok ? r.json() : null).then(setJob).catch(() => {});
  }, [briefId]);
  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return;
    const id = job.id;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const r = await fetch(`${API}/api/videos/${id}`);
        if (!r.ok) throw Error('Cannot read video task status');
        const next = await r.json();
        if (!cancelled) { setJob(next); setError(''); }
      } catch { if (!cancelled) setError('Status connection lost; reconnecting…'); }
    }, 2000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [job?.id, job?.status]);
  async function start() {
    setBusy(true); setError('');
    try {
      const endpoint = job?.status === 'failed' ? `/api/videos/${job.id}/retry` : `/api/briefs/${briefId}/videos`;
      const r = await fetch(`${API}${endpoint}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ allow_paid: consent }) });
      const data = await r.json();
      if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'Video task could not be created');
      setJob(data); localStorage.setItem(`daybrief-video-${briefId}`, data.id);
    } catch (e) { setError(e instanceof Error ? e.message : 'Video generation failed'); }
    finally { setBusy(false); }
  }
  const active = busy || !!job && ['queued', 'running'].includes(job.status);
  return <section className="script">
    <h2>Your video briefing</h2>
    <p>Uses this saved narration. No new news search or fact generation.</p>
    <label><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} disabled={active} /> I authorize Azure Speech synthesis, which may incur charges.</label>
    <p><button onClick={start} disabled={!consent || active}>{active ? 'Generating video…' : job?.status === 'failed' ? 'Retry video' : 'Generate video'}</button></p>
    {job && <p role="status">{job.stage} · {job.progress}% <progress max={100} value={job.progress} /></p>}
    {job?.status === 'failed' && <p role="alert">Video failed at {job.stage}: {job.error}. Your brief is unchanged.</p>}
    {error && <p role="alert">{error}</p>}
    {job?.status === 'completed' && job.video_url && <>
      <video controls playsInline preload="metadata" src={`${API}${job.video_url}`} style={{ width: '100%', maxWidth: 960, aspectRatio: '16 / 9' }} />
      <p><a href={`${API}${job.video_url}?download=true`}>Download MP4</a></p>
    </>}
  </section>;
}
