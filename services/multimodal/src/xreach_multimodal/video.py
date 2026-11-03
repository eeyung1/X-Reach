"""Bounded keyframe decoding through PyAV."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image


@dataclass(frozen=True)
class DecodedKeyframe:
    image: Image.Image
    label: str
    timestamp_seconds: float


def extract_keyframes(
    data: bytes,
    *,
    max_frames: int = 4,
    max_duration_seconds: float = 180.0,
) -> tuple[list[DecodedKeyframe], float]:
    try:
        import av
    except ImportError as error:
        raise RuntimeError("Install the inference extra to decode videos (PyAV).") from error

    try:
        container = av.open(BytesIO(data), mode="r")
        with container:
            stream = next(iter(container.streams.video), None)
            if stream is None:
                raise ValueError("video contains no video stream")
            duration = _duration_seconds(container, stream)
            if duration <= 0:
                raise ValueError("video duration could not be determined")
            if duration > max_duration_seconds:
                raise ValueError(f"video exceeds {max_duration_seconds:g} second limit")
            targets = _targets(duration, max_frames)
            frames: list[DecodedKeyframe] = []
            target_index = 0
            last_image: Image.Image | None = None
            last_time = 0.0
            for frame in container.decode(stream):
                if target_index >= len(targets):
                    break
                timestamp = float(frame.time or 0.0)
                if timestamp < targets[target_index][1]:
                    continue
                last_image = frame.to_image().convert("RGB")
                last_time = timestamp
                while target_index < len(targets) and timestamp >= targets[target_index][1]:
                    label, target = targets[target_index]
                    frames.append(DecodedKeyframe(last_image.copy(), label, timestamp))
                    target_index += 1
            while target_index < len(targets) and last_image is not None:
                label, _ = targets[target_index]
                frames.append(DecodedKeyframe(last_image.copy(), label, last_time))
                target_index += 1
            if not frames:
                raise ValueError("video contains no decodable frames")
            return frames, duration
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"could not decode video: {error}") from error


def _duration_seconds(container, stream) -> float:
    if stream.duration is not None and stream.time_base is not None:
        return float(stream.duration * stream.time_base)
    if container.duration is not None:
        return float(container.duration / av_time_base())
    return 0.0


def av_time_base() -> int:
    # PyAV expresses container.duration in AV_TIME_BASE microseconds.
    return 1_000_000


def _targets(duration: float, max_frames: int) -> list[tuple[str, float]]:
    requested = [
        ("opening", 0.0),
        ("hook", min(2.0, duration * 0.15)),
        ("midpoint", duration * 0.5),
        ("climax", duration * 0.8),
    ]
    distinct: list[tuple[str, float]] = []
    for label, timestamp in requested:
        timestamp = min(max(timestamp, 0.0), max(duration - 0.001, 0.0))
        if all(abs(timestamp - prior) > 0.05 for _, prior in distinct):
            distinct.append((label, timestamp))
    return distinct[:max_frames]
