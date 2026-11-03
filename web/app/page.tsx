"use client";

import { ChangeEvent, DragEvent, FormEvent, useEffect, useRef, useState } from "react";

type MediaItem = { file: File; preview: string };
type Analysis = {
  algorithmic_viability_score: number;
  reach_potential_percentile: number | string;
  placement_recommendation: { surface: string; reasoning: string };
  suggested_tweaks?: string[];
};

const sample: Analysis = {
  algorithmic_viability_score: 8.4,
  reach_potential_percentile: 82,
  placement_recommendation: {
    surface: "PROFILE POST",
    reasoning: "A standalone post gives this draft room to find its audience. Add a specific opening line before publishing.",
  },
  suggested_tweaks: ["Lead with the most useful detail.", "Keep external links in a follow-up when possible."],
};

function isSupported(file: File) {
  return file.type.startsWith("image/") || file.type.startsWith("video/");
}

export default function Home() {
  const [text, setText] = useState("");
  const [media, setMedia] = useState<MediaItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [notice, setNotice] = useState("Sample report shown. Connect the analysis API for draft-specific results.");
  const inputRef = useRef<HTMLInputElement>(null);
  const mediaRef = useRef<MediaItem[]>([]);

  useEffect(() => { mediaRef.current = media; }, [media]);
  useEffect(() => () => mediaRef.current.forEach((item) => URL.revokeObjectURL(item.preview)), []);

  function addFiles(files: FileList | File[]) {
    const incoming = Array.from(files);
    const rejected = incoming.filter((file) => !isSupported(file));
    const accepted = incoming.filter(isSupported).slice(0, Math.max(0, 4 - media.length));
    if (accepted.length) {
      setMedia((current) => [...current, ...accepted.map((file) => ({ file, preview: URL.createObjectURL(file) }))]);
      setAnalysis(null);
    }
    setNotice(rejected.length ? "Choose image or video files to preview." : "Media added to your draft.");
  }

  function onFileChange(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) addFiles(event.target.files);
    event.target.value = "";
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    addFiles(event.dataTransfer.files);
  }

  function removeMedia(index: number) {
    setMedia((current) => {
      const removed = current[index];
      if (removed) URL.revokeObjectURL(removed.preview);
      return current.filter((_, itemIndex) => itemIndex !== index);
    });
    setAnalysis(null);
  }

  async function analyzeDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!text.trim() && media.length === 0) {
      setNotice("Add some text or media before asking for an analysis.");
      return;
    }
    setBusy(true);
    setNotice("Checking for a connected analysis service…");
    try {
      const body = new FormData();
      body.set("text", text);
      media.forEach(({ file }) => body.append("media", file));
      const response = await fetch("/api/v1/analyze", { method: "POST", body });
      if (!response.ok) throw new Error("Analysis service is unavailable.");
      const result = (await response.json()) as Analysis;
      if (!Number.isFinite(result.algorithmic_viability_score) || !result.placement_recommendation?.surface || !result.placement_recommendation?.reasoning) {
        throw new Error("Analysis response is incomplete.");
      }
      setAnalysis(result);
      setNotice("Draft analysis complete.");
    } catch {
      setAnalysis(null);
      setNotice("The analysis API is not connected yet. The sample report below is illustrative, not a score for this draft.");
    } finally {
      setBusy(false);
    }
  }

  const shown = analysis ?? sample;
  const reachPercent = Math.min(100, Math.max(0, Number.parseFloat(String(shown.reach_potential_percentile)) || 0));

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="X-Reach home"><span className="brand-mark">x</span><span>X-Reach</span></a>
        <div className="topbar-right"><span className="live-dot" /> DRAFT LAB <span className="version">MILESTONE 01</span></div>
      </header>

      <section className="intro" id="top">
        <div className="eyebrow"><span>01</span> PRE-FLIGHT STUDIO</div>
        <h1>Make every post<br /><span>earn its first look.</span></h1>
        <p>Shape the draft, check the media, and get a clearer read on how it could travel.</p>
      </section>

      <div className="workspace">
        <form className="composer panel" onSubmit={analyzeDraft}>
          <div className="panel-heading"><div><span className="step">01 / COMPOSE</span><h2>Your draft</h2></div><span className="draft-badge">UNPUBLISHED</span></div>

          <label className="sr-only" htmlFor="post-text">Write or paste your post</label>
          <textarea id="post-text" value={text} onChange={(event) => { setText(event.target.value); setAnalysis(null); }} placeholder="Start with the idea you want people to remember…" maxLength={280} />
          <div className="compose-meta"><span>{text.length} / 280 characters</span><span>TEXT + MEDIA</span></div>

          <div
            className={`dropzone${dragging ? " is-dragging" : ""}`}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
            onDrop={onDrop}
          >
            <input ref={inputRef} className="sr-only" type="file" accept="image/*,video/*" multiple onChange={onFileChange} aria-label="Choose image or video files" />
            <div className="upload-icon" aria-hidden="true">＋</div>
            <div><strong>Drop an image or video here</strong><span>PNG, JPG, GIF, WEBP, MP4 · up to 4 files</span></div>
            <button className="browse-button" type="button" onClick={() => inputRef.current?.click()}>Browse files</button>
          </div>

          {media.length > 0 && <div className="media-grid" aria-label="Attached media">{media.map((item, index) => (
            <div className="media-card" key={`${item.file.name}-${index}`}>
              {item.file.type.startsWith("video/") ? <video src={item.preview} controls aria-label={item.file.name} /> : <img src={item.preview} alt={`Preview of ${item.file.name}`} />}
              <span className="media-name">{item.file.name}</span>
              <button className="remove-media" type="button" onClick={() => removeMedia(index)} aria-label={`Remove ${item.file.name}`}>×</button>
            </div>
          ))}</div>}

          <div className="form-footer"><span className="privacy-note"><span aria-hidden="true">◈</span> Draft stays in this browser until you analyze it.</span><button className="analyze-button" type="submit" disabled={busy}>{busy ? "Checking…" : "Analyze draft"}<span aria-hidden="true">↗</span></button></div>
        </form>

        <aside className="report panel" aria-live="polite">
          <div className="panel-heading"><div><span className="step">02 / READOUT</span><h2>Reach outlook</h2></div><span className="sparkle" aria-hidden="true">✳</span></div>
          <div className={`sample-alert${analysis ? " live-analysis" : ""}`}><span className="sample-dot" /> {analysis ? "LIVE ANALYSIS" : "ILLUSTRATIVE SAMPLE"}</div>

          <div className="score-row">
            <div className="score-ring" style={{ background: `radial-gradient(closest-side, #111916 84%, transparent 86% 99%), conic-gradient(var(--green) 0 ${reachPercent}%, #334137 ${reachPercent}% 100%)` }}><div><strong>{shown.algorithmic_viability_score.toFixed(1)}</strong><span>VIABILITY</span></div></div>
            <div className="reach-stat"><span>REACH POTENTIAL</span><strong>{String(shown.reach_potential_percentile).replace(/%$/, "")}<small>%</small></strong><span className="reach-caption">{analysis ? "model percentile" : "sample percentile"}</span></div>
          </div>

          <div className="placement-card"><div className="placement-top"><span>PLACEMENT ADVISOR</span><span className="placement-icon">↗</span></div><strong>{shown.placement_recommendation.surface.replaceAll("_", " ")}</strong><p>{shown.placement_recommendation.reasoning}</p></div>

          <div className="tweak-section"><div className="tweak-title"><span>QUICK TUNES</span><span>02</span></div><ul>{(shown.suggested_tweaks ?? sample.suggested_tweaks)?.map((tweak) => <li key={tweak}><span>↗</span>{tweak}</li>)}</ul></div>
          <p className="notice" role="status">{notice}</p>
          <div className="report-foot"><span>MODEL OUTPUTS ARE ESTIMATES</span><span>↗</span></div>
        </aside>
      </div>

      <footer className="page-footer"><span>X-REACH <i>·</i> CREATOR TOOLKIT</span><span>THINK CLEAR. SHIP WITH INTENT.</span></footer>
    </main>
  );
}
