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
import AlertSettings from "./components/AlertSettings";

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

type PercentileBounds = {
  lower: number;
  upper: number;
  confidence_level: number;
  method: string;
};

type Analysis = {
  algorithmic_viability_score: number;
  model_score_percentile: number | string | null;
  model_score_percentile_bounds?: PercentileBounds | null;
  score_calibration_status?: string;
  predicted_probabilities?: Record<string, number | null> | null;
  placement_recommendation: PlacementRecommendation;
  suggested_tweaks?: string[];
  local_topic_codes?: unknown;
  model_limitations?: string[];
  keyframes?: unknown;
  multimodal?: {
    keyframes?: unknown;
  };
};

type ReplyTarget = {
  tweet_id: string;
  conversation_id: string;
  in_reply_to_tweet_id: string;
  author: string;
  text: string;
};

type PlacementCandidateScore = {
  weighted_score: number;
  final_score: number;
  author_diversity_multiplier: number;
  out_of_network_multiplier: number;
  dropped_by: string[];
};

type PlacementEvaluation = {
  recommended_surface: string;
  reasoning: string;
  model_score_percentile: number | null;
  model_score_percentile_bounds: PercentileBounds | null;
  score_calibration_status: string;
  model_limitations: string[];
  scores: {
    profile: PlacementCandidateScore;
    direct_root_reply: PlacementCandidateScore;
  };
};

type ReplyEvaluation = {
  placement: PlacementEvaluation;
  comparativeAdvantage: number;
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
  model_score_percentile: 82,
  model_score_percentile_bounds: {
    lower: 68,
    upper: 94,
    confidence_level: 0.95,
    method: "dkw",
  },
  placement_recommendation: {
    surface: "PROFILE POST",
    reasoning:
      "A standalone post gives this draft room to find its audience. Add a specific opening line before publishing.",
  },
  suggested_tweaks: [
    "Lead with the most useful detail.",
    "Keep external links in a follow-up when possible.",
  ],
  model_limitations: [
    "Model scores compare weighted action propensities; they do not predict impressions, virality, or distribution.",
    "Percentiles rank scores within a configured reference set; they are not reach probability.",
    "Local Topic Codes are installation-specific and are not official X Phoenix Semantic IDs.",
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

function localTopicCodes(analysis: Analysis | null): number[] {
  if (!analysis) return [];
  return numericCodes(analysis.local_topic_codes);
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
  const [activeReplyTarget, setActiveReplyTarget] = useState<ReplyTarget | null>(null);
  const [replyText, setReplyText] = useState("");
  const [replyBusy, setReplyBusy] = useState(false);
  const [replyNotice, setReplyNotice] = useState("");
  const [replyEvaluation, setReplyEvaluation] = useState<ReplyEvaluation | null>(null);
  const [notice, setNotice] = useState(
    "Add a draft to request live multimodal analysis. The report preview is illustrative until then.",
  );
  const inputRef = useRef<HTMLInputElement>(null);
  const replyComposerRef = useRef<HTMLElement>(null);
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

  function selectReplyTarget(seed: RadarSeed) {
    setActiveReplyTarget({
      tweet_id: seed.tweet_id,
      conversation_id: seed.conversation_id ?? "",
      in_reply_to_tweet_id: seed.tweet_id,
      author: seed.screen_name ? `@${seed.screen_name}` : seed.author_id ? `Author ${seed.author_id}` : "X post",
      text: seed.text?.trim() || "Post text was not present in this capture.",
    });
    setReplyText("");
    setReplyEvaluation(null);
    setReplyNotice("");
    window.setTimeout(() => replyComposerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
  }

  async function evaluateReply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!activeReplyTarget) return;
    if (!activeReplyTarget.conversation_id || activeReplyTarget.in_reply_to_tweet_id !== activeReplyTarget.conversation_id) {
      setReplyNotice("Target verification failed: the reply must point to the root conversation post.");
      return;
    }
    if (!replyText.trim()) {
      setReplyNotice("Write a reply before evaluating placement.");
      return;
    }

    setReplyBusy(true);
    setReplyEvaluation(null);
    setReplyNotice("Analyzing the reply and comparing placement scores…");
    try {
      const analysisResponse = await fetch("/api/v1/analyze", {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ text: replyText.trim() }),
      });
      const analysisPayload = (await analysisResponse.json().catch(() => null)) as Analysis | null;
      if (!analysisResponse.ok) {
        throw new Error(errorDetail(analysisPayload) ?? `Analysis failed with status ${analysisResponse.status}.`);
      }
      const predictions = analysisPayload?.predicted_probabilities;
      if (!predictions || Object.keys(predictions).length === 0) {
        throw new Error("Analysis did not return action predictions required for placement comparison.");
      }

      const placementResponse = await fetch("/api/v1/placement", {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          text: replyText.trim(),
          profile_predictions: predictions,
          direct_root_reply_predictions: predictions,
        }),
      });
      const placementPayload = (await placementResponse.json().catch(() => null)) as PlacementEvaluation | null;
      if (!placementResponse.ok) {
        throw new Error(errorDetail(placementPayload) ?? `Placement comparison failed with status ${placementResponse.status}.`);
      }
      const profileScore = placementPayload?.scores?.profile?.final_score;
      const replyScore = placementPayload?.scores?.direct_root_reply?.final_score;
      if (
        !placementPayload?.recommended_surface ||
        typeof profileScore !== "number" ||
        !Number.isFinite(profileScore) ||
        typeof replyScore !== "number" ||
        !Number.isFinite(replyScore)
      ) {
        throw new Error("Placement response is incomplete.");
      }
      setReplyEvaluation({ placement: placementPayload, comparativeAdvantage: replyScore - profileScore });
      setReplyNotice("Comparison complete. No post was published.");
    } catch (error) {
      setReplyNotice(error instanceof Error ? error.message : "The reply comparison request failed.");
    } finally {
      setReplyBusy(false);
    }
  }

  const replyTargetVerified = Boolean(
    activeReplyTarget?.conversation_id &&
      activeReplyTarget.in_reply_to_tweet_id === activeReplyTarget.conversation_id,
  );

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
        localTopicCodes(payload).length === 6
          ? "Live multimodal analysis complete."
          : "Scoring complete; Local Topic Codes were not included in this API response.",
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The analysis request failed.");
    } finally {
      setBusy(false);
    }
  }

  const shown = analysis ?? sample;
  const topicCodes = localTopicCodes(analysis);
  const localKeyframe = media.find((item) => item.keyframe)?.keyframe ?? null;
  const keyframe = useMemo(
    () => keyframeFromResponse(analysis, localKeyframe) ?? localKeyframe,
    [analysis, localKeyframe],
  );
  const scorePercentile = shown.model_score_percentile;
  const hasScorePercentile = scorePercentile !== null && scorePercentile !== undefined;
  const scorePercent = hasScorePercentile
    ? Math.min(100, Math.max(0, Number.parseFloat(String(scorePercentile)) || 0))
    : 0;
  const percentileBounds = shown.model_score_percentile_bounds;
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
              <h2>Model score readout</h2>
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
                background: `radial-gradient(closest-side, #111916 84%, transparent 86% 99%), conic-gradient(var(--green) 0 ${scorePercent}%, #334137 ${scorePercent}% 100%)`,
              }}
            >
              <div>
                <strong>{shown.algorithmic_viability_score.toFixed(1)}</strong>
                <span>VIABILITY</span>
              </div>
            </div>
            <div className="reach-stat">
              <span>MODEL SCORE PERCENTILE</span>
              <strong>
                {hasScorePercentile ? String(scorePercentile).replace(/%$/, "") : "—"}
                {hasScorePercentile && <small>%</small>}
              </strong>
              <span className="reach-caption">
                {hasScorePercentile
                  ? analysis
                    ? analysis.score_calibration_status === "log_normal"
                      ? "parametric score rank · bounds unavailable"
                      : "rank in configured score reference"
                    : "illustrative score rank"
                  : "calibration pending"}
              </span>
              {percentileBounds && (
                <span className="reach-bounds">
                  {Math.round(percentileBounds.confidence_level * 100)}% band · {percentileBounds.lower.toFixed(1)}–{percentileBounds.upper.toFixed(1)}
                </span>
              )}
            </div>
          </div>

          <section className="semantic-section" aria-label="Local Topic Codes">
            <div className="semantic-title">
              <span>LOCAL TOPIC CODES · 6 LEVEL RVQ</span>
              <span>{topicCodes.length === 6 ? "AVAILABLE" : "PENDING"}</span>
            </div>
            {topicCodes.length === 6 ? (
              <div className="sid-list">
                {topicCodes.map((code, index) => (
                  <span className="sid-badge" key={`${index}-${code}`}>
                    <small>L{index + 1}</small>
                    {code.toString().padStart(3, "0")}
                  </span>
                ))}
              </div>
            ) : (
              <p>Local Topic Codes appear here when the local quantizer returns all six levels.</p>
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
          <section className="limitation-section" aria-labelledby="model-limitations-title">
            <div className="tweak-title" id="model-limitations-title">
              <span>MODEL LIMITATIONS</span>
              <span>READ BEFORE USE</span>
            </div>
            <ul>
              {(shown.model_limitations ?? sample.model_limitations ?? []).map((limitation) => (
                <li key={limitation}>{limitation}</li>
              ))}
            </ul>
          </section>
          <p className="notice" role="status">
            {notice}
          </p>
          <div className="report-foot">
            <span>MODEL OUTPUTS ARE ESTIMATES · NOT A REACH FORECAST</span>
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
                    <button type="button" className="radar-reply-action" onClick={() => selectReplyTarget(seed)}>
                      WRITE REPLY
                    </button>
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

      <AlertSettings />

      {activeReplyTarget && (
        <section className="reply-panel panel" ref={replyComposerRef} aria-labelledby="reply-heading">
          <div className="panel-heading">
            <div>
              <span className="step">05 / REPLY TARGETING</span>
              <h2 id="reply-heading">Reply to a live opportunity</h2>
            </div>
            <button type="button" className="reply-close" onClick={() => setActiveReplyTarget(null)} aria-label="Close reply composer">×</button>
          </div>

          <div className="reply-context">
            <strong>{activeReplyTarget.author}</strong>
            <p>{activeReplyTarget.text}</p>
          </div>
          <div className="reply-target-check" role="status">
            <span className={replyTargetVerified ? "target-verified" : "target-mismatch"}>
              {replyTargetVerified ? "TARGET VERIFIED" : "TARGET NEEDS REVIEW"}
            </span>
            <span>Reply to {activeReplyTarget.in_reply_to_tweet_id || "missing"}</span>
            <span>Conversation {activeReplyTarget.conversation_id || "missing"}</span>
          </div>
          {!replyTargetVerified && (
            <p className="reply-validation">Direct reply evaluation requires in_reply_to_tweet_id to match conversation_id.</p>
          )}

          <form onSubmit={evaluateReply}>
            <label className="sr-only" htmlFor="reply-text">Write your reply</label>
            <textarea
              id="reply-text"
              value={replyText}
              onChange={(event) => {
                setReplyText(event.target.value);
                setReplyEvaluation(null);
              }}
              placeholder="Write a useful, specific reply to this post…"
              maxLength={280}
            />
            <div className="reply-form-footer">
              <span>{replyText.length} / 280 characters</span>
              <button className="analyze-button" type="submit" disabled={replyBusy || !replyTargetVerified || !replyText.trim()}>
                {replyBusy ? "Evaluating…" : "Evaluate comparative advantage"}<span aria-hidden="true">↗</span>
              </button>
            </div>
          </form>

          <p className="reply-analysis-only">Analysis only — this does not publish or send a post.</p>
          {replyNotice && <p className="reply-notice" role="status">{replyNotice}</p>}
          {replyEvaluation && (
            <div className="reply-result" aria-live="polite">
              <div className="reply-result-heading">
                <span>COMPARATIVE ADVANTAGE</span>
                <strong className={replyEvaluation.comparativeAdvantage >= 0 ? "advantage-positive" : "advantage-negative"}>
                  {replyEvaluation.comparativeAdvantage > 0 ? "+" : ""}{replyEvaluation.comparativeAdvantage.toFixed(3)}
                </strong>
              </div>
              <div className="reply-score-grid">
                <div><span>PROFILE SCORE</span><strong>{replyEvaluation.placement.scores.profile.final_score.toFixed(3)}</strong></div>
                <div><span>DIRECT REPLY SCORE</span><strong>{replyEvaluation.placement.scores.direct_root_reply.final_score.toFixed(3)}</strong></div>
                <div><span>RECOMMENDED</span><strong>{replyEvaluation.placement.recommended_surface}</strong></div>
              </div>
              <p>{replyEvaluation.placement.reasoning}</p>
            </div>
          )}
        </section>
      )}

      <footer className="page-footer">
        <span>
          X-REACH <i>·</i> CREATOR TOOLKIT
        </span>
        <span>THINK CLEAR. SHIP WITH INTENT.</span>
      </footer>
    </main>
  );
}
