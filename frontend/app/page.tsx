'use client';
import { useEffect, useState } from 'react';
type Claim = {
  text: string;
  classification: string;
  support_status: string;
  supporting_source_urls: string[];
};
type Event = {
  id: string;
  headline: string;
  category: string;
  what_happened: string;
  why_it_matters: string;
  uncertainties: string[];
  claims: Claim[];
  source_urls: string[];
  latest_published_at: string;
  selection_reason: string;
  sensitivity: string;
};
type Article = { url: string; original_url?: string; publisher: string; published_at: string };
type Brief = {
  id: string;
  mode: string;
  selected_events: Event[];
  sources: Article[];
  narration: string;
  estimated_duration_seconds: number;
};
const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';
export default function Page() {
  const [topic, setTopic] = useState('general'),
    [brief, setBrief] = useState<Brief | null>(null),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(''),
    [mode, setMode] = useState('');
  useEffect(() => {
    fetch(`${API}/health`)
      .then((r) => r.json())
      .then((d) => setMode(d.mode))
      .catch(() => setError('Backend unavailable. Start the API and try again.'));
  }, []);
  async function generate() {
    setLoading(true);
    setError('');
    setBrief(null);
    try {
      const r = await fetch(`${API}/api/briefs/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, lookback_hours: 24 }),
      });
      const d = await r.json();
      if (!r.ok) throw Error(d.error?.message || 'Generation failed');
      setBrief(d);
      setMode(d.mode);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Generation failed');
    } finally {
      setLoading(false);
    }
  }
  return (
    <main>
      <header>
        <span>DAYBRIEF / THE DAILY EDITION</span>
        <span>Sources before speculation</span>
      </header>
      <section className="intro">
        <p className="eyebrow">A CLEARER VIEW OF TODAY</p>
        <h1>
          The world moves fast.
          <br />
          <em>Get the essentials.</em>
        </h1>
        <p className="dek">
          Three developments. Independent sources. A briefing you can read in about a minute.
        </p>
      </section>
      <div className="controls">
        <label>
          YOUR EDITION
          <select value={topic} onChange={(e) => setTopic(e.target.value)} disabled={loading}>
            <option value="general">General</option>
            <option value="ai_technology">AI & Technology</option>
            <option value="science">Science</option>
            <option value="markets">Macro & Policy</option>
          </select>
        </label>
        <button onClick={generate} disabled={loading}>
          {loading ? 'Checking sources…' : 'Generate brief →'}
        </button>
        <span>Past 24 hours</span>
        {mode === 'mock' && <strong className="badge">MOCK · FICTIONAL DEMO STORIES</strong>}
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {loading && (
        <p role="status">Retrieving coverage, checking evidence, and selecting your edition…</p>
      )}
      {brief && (
        <>
          <div className="sectiontitle">
            <h2>Your three stories</h2>
            <span>EDITION {brief.id.slice(0, 8)}</span>
          </div>
          <section className="grid">
            {brief.selected_events.map((e, i) => (
              <article key={e.id}>
                <p className="eyebrow">
                  0{i + 1} / {e.category.replace('_', ' ')}
                </p>
                <h2>{e.headline}</h2>
                <time>{new Date(e.latest_published_at).toLocaleString()}</time>
                {e.sensitivity !== 'normal' && <p className="badge">Sensitive story</p>}
                <h3>What happened</h3>
                <p>{e.what_happened}</p>
                <h3>Confirmed facts</h3>
                {e.claims
                  .filter(
                    (c) =>
                      c.classification === 'confirmed_fact' && c.support_status === 'supported',
                  )
                  .map((c, j) => (
                    <p key={j}>
                      {c.text}{' '}
                      <small>
                        {c.supporting_source_urls.map((u, k) => (
                          <a key={u} href={u} target="_blank" rel="noreferrer">
                            {' '}
                            [{k + 1}]
                          </a>
                        ))}
                      </small>
                    </p>
                  ))}
                <h3>Why it matters · Analysis</h3>
                <p>{e.why_it_matters}</p>
                <h3>Uncertainty</h3>
                {e.uncertainties.map((u) => (
                  <p key={u}>{u}</p>
                ))}
                <details>
                  <summary>Sources & editorial note</summary>
                  <p>{e.selection_reason}</p>
                  {e.source_urls.map((u) => {
                    const a = brief.sources.find((a) => a.url === u);
                    return (
                      <p key={u}>
                        <a href={a?.original_url || u} target="_blank" rel="noreferrer">
                          {a?.publisher || u}
                        </a>
                        <br />
                        <time>{a && new Date(a.published_at).toLocaleString()}</time>
                      </p>
                    );
                  })}
                </details>
              </article>
            ))}
          </section>
          <section className="script">
            <p className="eyebrow">
              THE ONE-MINUTE READ · ~{brief.estimated_duration_seconds} SECONDS
            </p>
            <h2>Your narration</h2>
            <p>{brief.narration}</p>
          </section>
        </>
      )}
      <footer>DayBrief · A source-backed perspective, with room for uncertainty.</footer>
    </main>
  );
}
