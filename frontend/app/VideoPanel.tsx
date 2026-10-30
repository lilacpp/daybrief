'use client';
import { useEffect, useState } from 'react';
type Anchor = { id: string; name: string; image_url: string };
type Job = { anchor?: string; id: string; status: string; stage: string; progress: number; error: string | null; video_url: string | null };
const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';
export default function VideoPanel({ briefId }: { briefId: string }) {
  return <VideoPanelState key={briefId} briefId={briefId} />;
}
function VideoPanelState({ briefId }: { briefId: string }) {
  const [anchors, setAnchors] = useState<Anchor[]>([]);
  const [anchor, setAnchor] = useState("cat");
  const [ready, setReady] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/api/anchors`).then(r => {
      if (!r.ok) throw Error('Cannot load anchors');
      return r.json();
    }).then((options: Anchor[]) => {
      if (cancelled) return;
      setAnchors(options);
      const saved = localStorage.getItem(`daybrief-anchor-${briefId}`);
      setAnchor(options.some(a => a.id === saved) ? saved! : 'cat');
      setReady(true);
    }).catch(() => { if (!cancelled) setError('Cannot load anchors. Please refresh to retry.'); });
    return () => { cancelled = true; };
  }, [briefId]);
  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    localStorage.setItem(`daybrief-anchor-${briefId}`, anchor);
    const id = localStorage.getItem(`daybrief-video-${briefId}-${anchor}`)
      ?? (anchor === 'cat' ? localStorage.getItem(`daybrief-video-${briefId}`) : null);
    if (id) fetch(`${API}/api/videos/${id}`).then(r => r.ok ? r.json() : null).then(data => {
      if (!cancelled && data && (data.anchor ?? 'cat') === anchor) setJob(data);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [briefId, anchor, ready]);
  const jobId = job?.id;
  const jobStatus = job?.status;
  useEffect(() => {
    if (!jobId || !jobStatus || !['queued', 'running'].includes(jobStatus)) return;
    const id = jobId;
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
  }, [jobId, jobStatus]);
  async function start() {
    setBusy(true); setError('');
    try {
      const endpoint = job?.status === 'failed' ? `/api/videos/${job.id}/retry` : `/api/briefs/${briefId}/videos`;
      const r = await fetch(`${API}${endpoint}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ allow_paid: true, anchor }) });
      const data = await r.json();
      if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'Video task could not be created');
      setJob(data); localStorage.setItem(`daybrief-video-${briefId}-${anchor}`, data.id);
    } catch (e) { setError(e instanceof Error ? e.message : 'Video generation failed'); }
    finally { setBusy(false); }
  }
  const active = busy || !!job && ['queued', 'running'].includes(job.status);
  return <section className="script">
    <h2>Your video briefing</h2>
    <fieldset aria-label="Newsroom host" disabled={active || !ready} style={{ border: 0, padding: 0, margin: '16px 0' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginTop: 10 }}>
        {anchors.map(option => <button key={option.id} type="button" aria-pressed={anchor === option.id}
          onClick={() => { setJob(null); setAnchor(option.id); }}
          className="anchor-choice">
          {/* Backend-owned canonical assets; no remote image configuration required. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={`${API}${option.image_url}`} alt={`${option.name} cartoon anchor`} width={114} height={64} style={{ borderRadius: 8, objectFit: 'cover' }} />
          <span>{option.name}{anchor === option.id ? ' ✓' : ''}</span>
        </button>)}
      </div>
    </fieldset>
    <p><button onClick={start} disabled={active || !ready}>{active ? 'Generating video…' : job?.status === 'failed' ? 'Retry video' : 'Generate video'}</button></p>
    {active && <p><progress aria-label="Video generation progress" max={100} value={job && ['queued', 'running'].includes(job.status) ? job.progress : undefined} style={{ width: '100%', maxWidth: 960 }} /></p>}
    {job?.status === 'failed' && <p role="alert">Video generation failed: {job.error || 'Please try again.'}</p>}
    {error && <p role="alert">{error}</p>}
    {job?.status === 'completed' && job.video_url && <>
      <video controls playsInline preload="metadata" src={`${API}${job.video_url}`} style={{ width: '100%', maxWidth: 960, aspectRatio: '16 / 9' }} />
      <p><a href={`${API}${job.video_url}?download=true`}>Download MP4</a></p>
    </>}
  </section>;
}
