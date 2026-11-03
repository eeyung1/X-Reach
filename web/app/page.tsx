"use client";

import {
  ChangeEvent,
  DragEvent,
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type KeyframePreview = {
  preview: string;
  timestampSeconds: number;
  label: string;
};

type MediaItem = {
  file: File;
  preview: string;
  keyframe?: KeyframePreview;
};

type PlacementRecommendation = {
  surface: string;
  reasoning: string;
};

type Analysis = {
  algorithmic_viability_score: number;
  reach_potential_percentile: number | string | null;
  reach_calibration_status?: string;
  placement_recommendation: PlacementRecommendation;
  suggested_tweaks?: string[];
  semantic_ids?: unknown;
  semantic_id?: unknown;
  keyframes?: unknown;
  multimodal?: {
    semantic_ids?: unknown;
    semantic_id?: unknown;
    keyframes?: unknown;
  };
};

type RadarSeed = {
  tweet_id: string;
  conversation_id?: string;
  author_id?: string | null;
  screen_name?: string | null;
  text?: string | null;
  created_at?: string;
  age_minutes?: number;
  impressions: number;
  views_per_minute: number;
  replies: number;
  likes?: number | null;
  reposts?: number | null;
  quotes?: number | null;
  simclusters: string[];
  published_at?: string;
};

type RadarEntry = { seed: RadarSeed; receivedAt: number };
type RadarConnection = "connecting" | "live" | "reconnecting" | "offline";

const MAX_MEDIA = 4;
const MAX_FILE_BYTES = 50 * 1024 * 1024;

const sample: Analysis = {
  algorithmic_viability_score: 8.4,
  reach_potential_percentile: 82,
  placement_recommendation: {
    surface: "PROFILE POST",
    reasoning:
      "A standalone post gives this draft room to find its audience. Add a specific opening line before publishing.",
  },
  suggested_tweaks: [
    "Lead with the most useful detail.",
    "Keep external links in a follow-up when possible.",
  ],
};

function isSupported(file: File) {
  return (
    (file.type.startsWith("image/") || file.type.startsWith("video/")) &&
    file.size <= MAX_FILE_BYTES
  );
}

function numericCodes(value: unknown): number[] {
  if (Array.isArray(value)) {
    const codes = value.map((item) => Number(item));
    return codes.length === 6 && codes.every((item) => Number.isInteger(item) && item >= 0 && item <= 255)
      ? codes
      : [];
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return numericCodes(record.codes ?? record.levels ?? record.values);
  }
  return [];
}

function semanticIds(analysis: Analysis | null): number[] {
  if (!analysis) return [];
  return numericCodes(
    analysis.semantic_ids ??
      analysis.semantic_id ??
      analysis.multimodal?.semantic_ids ??
      analysis.multimodal?.semantic_id,
  );
}

function keyframeFromResponse(
  analysis: Analysis | null,
  localPreview: KeyframePreview | null,
): KeyframePreview | null {
  const candidate = analysis?.keyframes ?? analysis?.multimodal?.keyframes;
  if (!Array.isArray(candidate)) return null;
  for (const item of candidate) {
    if (!item || typeof item !== "object") continue;
    const frame = item as Record<string, unknown>;
    const preview =
      frame.preview_url ??
      frame.image_data_url ??
      frame.data_url ??
      frame.preview ??
      localPreview?.preview;
    if (typeof preview !== "string" || preview.length === 0) continue;
    const timestampMilliseconds = Number(frame.timestamp_ms);
    const timestamp = Number(
      frame.timestamp_seconds ??
        frame.time_seconds ??
        frame.timestamp ??
        (Number.isFinite(timestampMilliseconds) ? timestampMilliseconds / 1_000 : 0),
    );
    return {
      preview,
      timestampSeconds: Number.isFinite(timestamp) ? timestamp : 0,
      label: typeof frame.label === "string" ? frame.label : "MODEL KEYFRAME",
    };
  }
  return null;
}

function errorDetail(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const detail = (payload as Record<string, unknown>).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) =>
        item && typeof item === "object" && typeof (item as Record<string, unknown>).msg === "string"
          ? String((item as Record<string, unknown>).msg)
          : null,
      )
      .filter(Boolean);
    return messages.length ? messages.join("; ") : null;
  }
  return null;
}

function radarSeedFromEvent(value: unknown): RadarSeed | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const velocity = Number(record.views_per_minute);
  const impressions = Number(record.impressions);
  const replies = Number(record.replies);
  if (
    typeof record.tweet_id !== "string" ||
    !Number.isFinite(velocity) ||
    !Number.isFinite(impressions) ||
    !Number.isFinite(replies)
  ) {
    return null;
  }
  const simclusters = Array.isArray(record.simclusters)
    ? record.simclusters.filter((item): item is string => typeof item === "string")
    : [];
  return {
    tweet_id: record.tweet_id,
    conversation_id:
      typeof record.conversation_id === "string" ? record.conversation_id : undefined,
    author_id: typeof record.author_id === "string" ? record.author_id : null,
    screen_name:
      typeof record.screen_name === "string" ? record.screen_name : null,
    text: typeof record.text === "string" ? record.text : null,
    created_at: typeof record.created_at === "string" ? record.created_at : undefined,
    age_minutes: Number.isFinite(Number(record.age_minutes))
      ? Number(record.age_minutes)
      : undefined,
    impressions,
    views_per_minute: velocity,
    replies,
    likes: Number.isFinite(Number(record.likes)) ? Number(record.likes) : null,
    reposts: Number.isFinite(Number(record.reposts)) ? Number(record.reposts) : null,
    quotes: Number.isFinite(Number(record.quotes)) ? Number(record.quotes) : null,
    simclusters,
    published_at:
      typeof record.published_at === "string" ? record.published_at : undefined,
  };
}

function freshnessLabel(receivedAt: number, now: number): string {
  const seconds = Math.max(0, Math.floor((now - receivedAt) / 1_000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

function postedAgeLabel(createdAt: string | undefined, now: number): string | null {
  if (!createdAt) return null;
  const timestamp = Date.parse(createdAt);
  if (!Number.isFinite(timestamp)) return null;
  const minutes = Math.max(0, Math.floor((now - timestamp) / 60_000));
  return minutes < 1 ? "posted just now" : `posted ${minutes}m ago`;
}

async function captureHookFrame(videoUrl: string): Promise<KeyframePreview> {
  const video = document.createElement("video");
  video.muted = true;
  video.preload = "metadata";
  video.src = videoUrl;

  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(
      () => reject(new Error("Video preview timed out.")),
      8_000,
    );
    const complete = () => {
      window.clearTimeout(timeout);
      resolve();
    };
    video.onloadedmetadata = () => {
      const target = Math.min(2, Math.max(0, video.duration * 0.08));
      video.currentTime = Number.isFinite(target) ? target : 0;
    };
    video.onloadeddata = () => {
      if (video.currentTime === 0) complete();
    };
    video.onseeked = complete;
    video.onerror = () => {
      window.clearTimeout(timeout);
      reject(new Error("Video preview could not be decoded."));
    };
  });

  const sourceWidth = video.videoWidth || 640;
  const sourceHeight = video.videoHeight || 360;
  const width = Math.min(sourceWidth, 640);
  const height = Math.max(1, Math.round((sourceHeight / sourceWidth) * width));
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  canvas.getContext("2d")?.drawImage(video, 0, 0, width, height);
  return {
    preview: canvas.toDataURL("image/jpeg", 0.82),
    timestampSeconds: video.currentTime,
    label: "LOCAL HOOK PREVIEW",
  };
}

async function toMediaItem(file: File): Promise<MediaItem> {
  const preview = URL.createObjectURL(file);
  if (!file.type.startsWith("video/")) return { file, preview };
  try {
    return { file, preview, keyframe: await captureHookFrame(preview) };
  } catch {
    return { file, preview };
  }
}

export default function Home() {
  const [text, setText] = useState("");
  const [media, setMedia] = useState<MediaItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [radarEntries, setRadarEntries] = useState<RadarEntry[]>([]);
  const [radarConnection, setRadarConnection] = useState<RadarConnection>("connecting");
  const [radarFilterDraft, setRadarFilterDraft] = useState("");
  const [radarFilter, setRadarFilter] = useState("");
  const [radarFilterRevision, setRadarFilterRevision] = useState(0);
  const [radarNotice, setRadarNotice] = useState("");
  const [radarNow, setRadarNow] = useState(0);
  const [notice, setNotice] = useState(
    "Add a draft to request live multimodal analysis. The report preview is illustrative until then.",
  );
  const inputRef = useRef<HTMLInputElement>(null);
  const mediaRef = useRef<MediaItem[]>([]);

  useEffect(() => {
    mediaRef.current = media;
  }, [media]);

  useEffect(
    () => () => mediaRef.current.forEach((item) => URL.revokeObjectURL(item.preview)),
    [],
  );

  useEffect(() => {
    const timer = window.setInterval(() => setRadarNow(Date.now()), 5_000);
    setRadarNow(Date.now());
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const query = new URLSearchParams();
    if (radarFilter) query.set("simcluster", radarFilter);
    const source = new EventSource(
      `/api/v1/radar/stream${query.size ? `?${query.toString()}` : ""}`,
    );
    setRadarConnection("connecting");
    setRadarNotice("");

    source.onopen = () => setRadarConnection("live");
    source.onerror = () =>
      setRadarConnection(
        source.readyState === EventSource.CLOSED ? "offline" : "reconnecting",
      );
    source.addEventListener("seed", (event: Event) => {
      const message = event as MessageEvent<string>;
      try {
        const seed = radarSeedFromEvent(JSON.parse(message.data));
        if (!seed) return;
        const receivedAt = Date.now();
        setRadarEntries((current) => {
          const updated = new Map(current.map((entry) => [entry.seed.tweet_id, entry]));
          updated.set(seed.tweet_id, { seed, receivedAt });
          return Array.from(updated.values())
            .sort((left, right) => right.seed.views_per_minute - left.seed.views_per_minute)
            .slice(0, 30);
        });
        setRadarConnection("live");
      } catch {
        setRadarNotice("A radar update could not be read; the stream will keep listening.");
      }
    });
    source.addEventListener("heartbeat", () => setRadarConnection("live"));
    source.addEventListener("reset", () => {
      setRadarEntries([]);
      setRadarNotice("Some older opportunities expired from the live buffer.");
    });
    return () => source.close();
  }, [radarFilter, radarFilterRevision]);

  function applyRadarFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setRadarEntries([]);
    setRadarFilter(radarFilterDraft.trim());
    setRadarFilterRevision((revision) => revision + 1);
  }

  async function addFiles(files: FileList | File[]) {
    const incoming = Array.from(files);
    const slots = Math.max(0, MAX_MEDIA - media.length);
    const accepted = incoming.filter(isSupported).slice(0, slots);
    const rejected = incoming.length - accepted.length;
    if (accepted.length) {
      setNotice("Preparing media and video hook previews…");
      const prepared = await Promise.all(accepted.map(toMediaItem));
      setMedia((current) => [...current, ...prepared].slice(0, MAX_MEDIA));
      setAnalysis(null);
    }
    setNotice(
      rejected
        ? "Some files were skipped. Use up to four image or video files, each 50 MB or smaller."
        : "Media is ready for multimodal analysis.",
    );
  }

  function onFileChange(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) void addFiles(event.target.files);
    event.target.value = "";
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void addFiles(event.dataTransfer.files);
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
    setAnalysis(null);
    setNotice("Uploading the draft and extracting multimodal signals…");
    try {
      const body = new FormData();
      body.set("text", text.trim());
      media.forEach(({ file }) => body.append("media", file, file.name));
      const response = await fetch("/api/v1/analyze", {
        method: "POST",
        body,
        headers: { Accept: "application/json" },
      });
      const payload = (await response.json().catch(() => null)) as Analysis | null;
      if (!response.ok) {
        throw new Error(errorDetail(payload) ?? `Analysis failed with status ${response.status}.`);
      }
      if (
        !payload ||
        !Number.isFinite(payload.algorithmic_viability_score) ||
        !payload.placement_recommendation?.surface ||
        !payload.placement_recommendation?.reasoning
      ) {
        throw new Error("Analysis response is incomplete.");
      }
      setAnalysis(payload);
      setNotice(
        semanticIds(payload).length === 6
          ? "Live multimodal analysis complete."
          : "Scoring complete; Semantic IDs were not included in this API response.",
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The analysis request failed.");
    } finally {
      setBusy(false);
    }
  }

  const shown = analysis ?? sample;
  const ids = semanticIds(analysis);
  const localKeyframe = media.find((item) => item.keyframe)?.keyframe ?? null;
  const keyframe = useMemo(
    () => keyframeFromResponse(analysis, localKeyframe) ?? localKeyframe,
    [analysis, localKeyframe],
  );
  const reachValue = shown.reach_potential_percentile;
  const hasCalibratedReach = reachValue !== null && reachValue !== undefined;
  const reachPercent = hasCalibratedReach
    ? Math.min(100, Math.max(0, Number.parseFloat(String(reachValue)) || 0))
    : 0;
  const peakVelocity = Math.max(
    1,
    ...radarEntries.map((entry) => entry.seed.views_per_minute),
  );
  const latestRadarUpdate = radarEntries.reduce<number | null>(
    (latest, entry) => Math.max(latest ?? 0, entry.receivedAt),
    null,
  );

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="X-Reach home">
          <span className="brand-mark">x</span>
          <span>X-Reach</span>
        </a>
        <div className="topbar-right">
          <span className="live-dot" /> DRAFT LAB <span className="version">MILESTONE 03</span>
        </div>
      </header>

      <section className="intro" id="top">
        <div className="eyebrow">
          <span>01</span> MULTIMODAL PRE-FLIGHT
        </div>
        <h1>
          Make every post
          <br />
          <span>earn its first look.</span>
        </h1>
        <p>Upload the real draft to score its text, visuals, video pacing, and placement.</p>
      </section>

      <div className="workspace">
        <form className="composer panel" onSubmit={analyzeDraft}>
          <div className="panel-heading">
            <div>
              <span className="step">01 / COMPOSE</span>
              <h2>Your draft</h2>
            </div>
            <span className="draft-badge">UNPUBLISHED</span>
          </div>

          <label className="sr-only" htmlFor="post-text">
            Write or paste your post
          </label>
          <textarea
            id="post-text"
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setAnalysis(null);
            }}
            placeholder="Start with the idea you want people to remember…"
            maxLength={280}
          />
          <div className="compose-meta">
            <span>{text.length} / 280 characters</span>
            <span>MULTIPART · TEXT + MEDIA</span>
          </div>

          <div
            className={`dropzone${dragging ? " is-dragging" : ""}`}
            onDragEnter={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false);
            }}
            onDrop={onDrop}
          >
            <input
              ref={inputRef}
              className="sr-only"
              type="file"
              accept="image/*,video/*"
              multiple
              onChange={onFileChange}
              aria-label="Choose image or video files"
            />
            <div className="upload-icon" aria-hidden="true">
              ＋
            </div>
            <div>
              <strong>Drop images or a short video here</strong>
              <span>PNG, JPG, GIF, WEBP, MP4 · 50 MB each · up to 4 files</span>
            </div>
            <button className="browse-button" type="button" onClick={() => inputRef.current?.click()}>
              Browse files
            </button>
          </div>

          {media.length > 0 && (
            <div className="media-grid" aria-label="Attached media">
              {media.map((item, index) => (
                <div className="media-card" key={`${item.file.name}-${item.file.lastModified}-${index}`}>
                  {item.file.type.startsWith("video/") ? (
                    <video src={item.preview} controls aria-label={item.file.name} />
                  ) : (
                    <img src={item.preview} alt={`Preview of ${item.file.name}`} />
                  )}
                  <span className="media-name">{item.file.name}</span>
                  <button
                    className="remove-media"
                    type="button"
                    onClick={() => removeMedia(index)}
                    aria-label={`Remove ${item.file.name}`}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          {keyframe && (
            <div className="keyframe-card composer-keyframe">
              <img src={keyframe.preview} alt="Video keyframe preview used for analysis" />
              <div>
                <span>{keyframe.label}</span>
                <strong>{keyframe.timestampSeconds.toFixed(1)}s</strong>
                <p>Preview of the early frame used to assess hook strength and pacing.</p>
              </div>
            </div>
          )}

          <div className="form-footer">
            <span className="privacy-note">
              <span aria-hidden="true">◈</span> Files upload only when you select Analyze.
            </span>
            <button className="analyze-button" type="submit" disabled={busy}>
              {busy ? "Analyzing…" : "Analyze draft"}
              <span aria-hidden="true">↗</span>
            </button>
          </div>
        </form>

        <aside className="report panel" aria-live="polite" aria-busy={busy}>
          <div className="panel-heading">
            <div>
              <span className="step">02 / READOUT</span>
              <h2>Reach outlook</h2>
            </div>
            <span className="sparkle" aria-hidden="true">
              ✳
            </span>
          </div>
          <div className={`sample-alert${analysis ? " live-analysis" : ""}`}>
            <span className="sample-dot" /> {analysis ? "LIVE MULTIMODAL ANALYSIS" : "ILLUSTRATIVE SAMPLE"}
          </div>

          <div className="score-row">
            <div
              className="score-ring"
              style={{
                background: `radial-gradient(closest-side, #111916 84%, transparent 86% 99%), conic-gradient(var(--green) 0 ${reachPercent}%, #334137 ${reachPercent}% 100%)`,
              }}
            >
              <div>
                <strong>{shown.algorithmic_viability_score.toFixed(1)}</strong>
                <span>VIABILITY</span>
              </div>
            </div>
            <div className="reach-stat">
              <span>REACH POTENTIAL</span>
              <strong>
                {hasCalibratedReach ? String(reachValue).replace(/%$/, "") : "—"}
                {hasCalibratedReach && <small>%</small>}
              </strong>
              <span className="reach-caption">
                {hasCalibratedReach
                  ? analysis
                    ? "model percentile"
                    : "sample percentile"
                  : "calibration pending"}
              </span>
            </div>
          </div>

          <section className="semantic-section" aria-label="Semantic IDs">
            <div className="semantic-title">
              <span>SEMANTIC ID · 6 LEVEL RVQ</span>
              <span>{ids.length === 6 ? "ALIGNED" : "PENDING"}</span>
            </div>
            {ids.length === 6 ? (
              <div className="sid-list">
                {ids.map((code, index) => (
                  <span className="sid-badge" key={`${index}-${code}`}>
                    <small>L{index + 1}</small>
                    {code.toString().padStart(3, "0")}
                  </span>
                ))}
              </div>
            ) : (
              <p>Semantic IDs appear here when the multimodal API returns all six quantizer levels.</p>
            )}
          </section>

          <div className="placement-card">
            <div className="placement-top">
              <span>PLACEMENT ADVISOR</span>
              <span className="placement-icon">↗</span>
            </div>
            <strong>{shown.placement_recommendation.surface.replaceAll("_", " ")}</strong>
            <p>{shown.placement_recommendation.reasoning}</p>
          </div>

          <div className="tweak-section">
            <div className="tweak-title">
              <span>QUICK TUNES</span>
              <span>{String((shown.suggested_tweaks ?? []).length).padStart(2, "0")}</span>
            </div>
            <ul>
              {(shown.suggested_tweaks ?? []).map((tweak) => (
                <li key={tweak}>
                  <span>↗</span>
                  {tweak}
                </li>
              ))}
            </ul>
          </div>
          <p className="notice" role="status">
            {notice}
          </p>
          <div className="report-foot">
            <span>MODEL OUTPUTS ARE ESTIMATES</span>
            <span>↗</span>
          </div>
        </aside>
      </div>

      <section className="radar-panel panel" aria-labelledby="radar-title">
        <div className="panel-heading radar-heading">
          <div>
            <span className="step">03 / LIVE SIGNALS</span>
            <h2 id="radar-title">Radar opportunities</h2>
          </div>
          <span className={`radar-connection ${radarConnection}`}>
            <span className="radar-connection-dot" />
            {radarConnection === "live"
              ? "LIVE"
              : radarConnection === "reconnecting"
                ? "RECONNECTING"
                : radarConnection === "offline"
                  ? "OFFLINE"
                  : "CONNECTING"}
          </span>
        </div>

        <div className="radar-toolbar">
          <p>
            Fresh root posts from active SimClusters, ranked by observed view velocity.
          </p>
          <form className="radar-filter" onSubmit={applyRadarFilter}>
            <label className="sr-only" htmlFor="radar-cluster-filter">
              Filter radar by SimCluster
            </label>
            <input
              id="radar-cluster-filter"
              value={radarFilterDraft}
              onChange={(event) => setRadarFilterDraft(event.target.value)}
              placeholder="Filter by SimCluster"
            />
            <button type="submit">Apply</button>
          </form>
        </div>

        <div className="radar-stream-meta" aria-live="polite">
          <span>
            {radarFilter ? `FILTER · ${radarFilter}` : "ALL SIMCLUSTERS"}
          </span>
          <span>
            {latestRadarUpdate === null
              ? "WAITING FOR FIRST SIGNAL"
              : `LAST SIGNAL ${freshnessLabel(latestRadarUpdate, radarNow)}`}
          </span>
        </div>

        {radarNotice && <p className="radar-notice" role="status">{radarNotice}</p>}

        {radarEntries.length ? (
          <div className="radar-list">
            {radarEntries.map(({ seed, receivedAt }, index) => {
              const velocityShare = Math.max(
                4,
                (seed.views_per_minute / peakVelocity) * 100,
              );
              const postedAge = postedAgeLabel(seed.created_at, radarNow);
              const author = seed.screen_name
                ? `@${seed.screen_name}`
                : seed.author_id
                  ? `Author ${seed.author_id}`
                  : "X post";
              return (
                <article className="radar-card" key={seed.tweet_id}>
                  <div className="radar-card-topline">
                    <span className="radar-rank">{String(index + 1).padStart(2, "0")}</span>
                    <span className="radar-freshness">
                      Updated {freshnessLabel(receivedAt, radarNow)}
                    </span>
                  </div>
                  <div className="radar-author-line">
                    <strong>{author}</strong>
                    {postedAge && <span>{postedAge}</span>}
                  </div>
                  <p className="radar-post-text">
                    {seed.text?.trim() || "Post text was not present in this capture."}
                  </p>
                  <div className="radar-velocity-heading">
                    <span>VIEW VELOCITY</span>
                    <strong>{Math.round(seed.views_per_minute).toLocaleString()}/min</strong>
                  </div>
                  <div
                    className="radar-velocity-track"
                    role="img"
                    aria-label={`${Math.round(seed.views_per_minute).toLocaleString()} views per minute`}
                  >
                    <span style={{ width: `${velocityShare}%` }} />
                  </div>
                  <div className="radar-metrics">
                    <span>{seed.impressions.toLocaleString()} views</span>
                    <span>{seed.replies.toLocaleString()} replies</span>
                  </div>
                  <div className="radar-card-bottom">
                    <div className="radar-clusters">
                      {seed.simclusters.length ? (
                        seed.simclusters.slice(0, 3).map((cluster) => (
                          <span key={cluster}>{cluster}</span>
                        ))
                      ) : (
                        <span>Unlabeled cluster</span>
                      )}
                    </div>
                    <a
                      href={`https://x.com/i/web/status/${encodeURIComponent(seed.tweet_id)}`}
                      target="_blank"
                      rel="noreferrer"
                      aria-label={`Open post by ${author} on X`}
                    >
                      OPEN POST <span aria-hidden="true">↗</span>
                    </a>
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="radar-empty" role="status">
            <span className="radar-empty-mark" aria-hidden="true">↗</span>
            <strong>No live opportunities yet</strong>
            <p>
              {radarConnection === "offline"
                ? "The radar stream is offline. It will reconnect automatically when the API is available."
                : radarFilter
                  ? `Waiting for fresh roots tagged ${radarFilter}.`
                  : "The stream will show fresh root posts when the radar receives a new capture."}
            </p>
          </div>
        )}
      </section>

      <footer className="page-footer">
        <span>
          X-REACH <i>·</i> CREATOR TOOLKIT
        </span>
        <span>THINK CLEAR. SHIP WITH INTENT.</span>
      </footer>
    </main>
  );
}
