"""Saved narration -> opening, story scenes and closing. No news or language-model calls."""

import hashlib
import json
import math
import sqlite3
import subprocess
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

from .narrative import validate_saved_narrative


def command(args):
    return subprocess.run(args, check=True, capture_output=True, timeout=600)


def duration(path):
    return float(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ]
        ).stdout
    )


def split_brief(brief):
    if brief.narrative_segments is not None:
        return validate_saved_narrative(brief)
    if brief.narrative_evidence_digest is not None:
        raise ValueError("Missing validated narrative segments")
    parts = brief.narration.split("\n\n")
    if len(parts) != len(brief.selected_events) + 2 or parts[0] != brief.opening or parts[-1] != brief.closing:
        raise ValueError(
            "Saved narration must contain opening, selected stories and closing"
        )
    if any(not p.strip() for p in parts):
        raise ValueError("Saved narration contains an empty scene")
    # Older or edited records must still consist of saved, verified claim text.
    for text, event in zip(parts[1:-1], brief.selected_events, strict=True):
        remaining = text
        claims = sorted(
            (
                c.text
                for c in event.claims
                if c.classification == "confirmed_fact"
                and c.support_status == "supported"
            ),
            key=len,
            reverse=True,
        )
        while remaining:
            match = next(
                (c for c in claims if remaining == c or remaining.startswith(c + " ")),
                None,
            )
            if match is None:
                raise ValueError("Story narration does not match saved verified claims")
            remaining = remaining[len(match) :].lstrip()
    return parts


class Jobs:
    def __init__(self, database_url):
        self.path = database_url.removeprefix("sqlite:///")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def get(self, job_id):
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM videos WHERE id=?", (job_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, job):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO videos VALUES (?, ?)",
                (job["id"], json.dumps(job)),
            )

    def create(self, brief_id, provider):
        job = {
            "id": str(uuid4()),
            "brief_id": brief_id,
            "provider": provider,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "error": None,
            "video_url": None,
        }
        self.save(job)
        return job

    def recover(self):
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT payload FROM videos").fetchall()
        for row in rows:
            job = json.loads(row[0])
            if job["status"] in ("queued", "running"):
                job.update(
                    status="failed", error="Worker interrupted. Retry this task."
                )
                self.save(job)


def synthesize(text, path, settings, provider):
    if provider == "local":
        command(
            [
                "say",
                "-v",
                "Samantha",
                "-r",
                "165",
                "-o",
                str(path.with_suffix(".aiff")),
                text,
            ]
        )
        command(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(path.with_suffix(".aiff")),
                "-ar",
                "24000",
                "-ac",
                "1",
                str(path),
            ]
        )
        return []
    if not settings.azure_speech_enabled:
        raise ValueError("Azure Speech is disabled")
    import azure.cognitiveservices.speech as sdk

    config = sdk.SpeechConfig(
        subscription=settings.azure_speech_key, region=settings.azure_speech_region
    )
    config.speech_synthesis_voice_name = settings.azure_speech_voice
    config.set_speech_synthesis_output_format(
        sdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm
    )
    synthesizer = sdk.SpeechSynthesizer(speech_config=config, audio_config=None)
    boundaries = []

    def boundary(event):
        if event.boundary_type == sdk.SpeechSynthesisBoundaryType.Word:
            boundaries.append(
                {
                    "start": event.audio_offset / 10_000_000,
                    "offset": event.text_offset,
                    "length": event.word_length,
                }
            )

    synthesizer.synthesis_word_boundary.connect(boundary)
    result = synthesizer.speak_text_async(text).get()
    if result.reason != sdk.ResultReason.SynthesizingAudioCompleted:
        # Provider details can contain URLs or sensitive data; do not expose them.
        raise ValueError(
            "Azure speech synthesis failed; check backend credentials and region"
        )
    path.write_bytes(result.audio_data)
    return boundaries


def captions(text, seconds, boundaries):
    if boundaries:
        # Use offsets to retain original punctuation rather than reconstructing words.
        points = sorted(boundaries, key=lambda b: b["offset"])
        chunks = []
        for i in range(0, len(points), 7):
            end = points[i + 7]["offset"] if i + 7 < len(points) else len(text)
            start = points[i]["start"] if i else 0
            chunks.append((start, text[points[i]["offset"] if i else 0 : end].strip()))
        if any(t < 0 or t >= seconds for t, _ in chunks):
            raise ValueError("Invalid TTS word timing")
        return [
            (t, chunks[i + 1][0] if i + 1 < len(chunks) else seconds, words)
            for i, (t, words) in enumerate(chunks)
        ]
    words = text.split()
    return [
        (
            seconds * i / len(words),
            seconds * min(i + 7, len(words)) / len(words),
            " ".join(words[i : i + 7]),
        )
        for i in range(0, len(words), 7)
    ]


def font(settings, size):
    from PIL import ImageFont

    candidates = [
        settings.video_font_path,
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise ValueError("Set VIDEO_FONT_PATH to an installed TrueType font")


def wrap(draw, text, face, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=face) > width and line:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


def mouth_timing(audio, seconds, fps=12):
    """Small audio-energy-driven pose changes; silence keeps the mouth closed."""
    import array
    import sys
    import wave

    with wave.open(str(audio), "rb") as stream:
        if stream.getsampwidth() != 2 or stream.getnchannels() != 1:
            raise ValueError("Mouth animation requires mono 16-bit PCM audio")
        rate = stream.getframerate()
        samples = array.array("h", stream.readframes(stream.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    energies = []
    for frame in range(math.ceil(seconds * fps)):
        chunk = samples[int(frame * rate / fps):int((frame + 1) * rate / fps)]
        energies.append(math.sqrt(sum(v * v for v in chunk) / max(1, len(chunk))))
    peak = max(energies, default=0)
    return [(i / fps, min((i + 1) / fps, seconds),
             0 if energy < max(180, peak * 0.08) else 1 if energy < peak * 0.5 else 2)
            for i, energy in enumerate(energies)]


def card(settings, brief, index, subtitle, target, mouth_open=0):
    """Render the canonical cat behind captions in the actual video path."""
    from PIL import Image, ImageDraw, ImageOps

    from .visuals import asset_path, canonical_anchor, visual_root

    anchor = canonical_anchor(settings)
    with Image.open(asset_path(visual_root(settings), anchor.image_path)) as source:
        im = ImageOps.fit(source.convert("RGB"), (1280, 720))
    draw = ImageDraw.Draw(im)
    if mouth_open:
        # A restrained vector mouth overlay aligned to the canonical cat-v1 muzzle.
        # The original closed smile remains unchanged for silent frames.
        depth = 7 if mouth_open == 1 else 15
        draw.ellipse((615, 260, 637, 260 + depth), fill="#39232c", outline="#503630", width=1)
        if mouth_open == 2:
            draw.ellipse((621, 269, 633, 273), fill="#b77780")
    closing_index = len(brief.selected_events) + 1
    label = "OPENING" if index == 0 else "CLOSING" if index == closing_index else f"STORY {index:02d}"
    draw.rectangle((0, 0, 1280, 54), fill="#101d2d")
    draw.text((28, 14), f"DAYBRIEF / {label}", font=font(settings, 24), fill="#62e0bc")
    footer = "FICTIONAL DEMO" if brief.mode == "mock" else brief.generated_at.strftime("%Y-%m-%d")
    draw.text((830, 16), f"{footer} / {index + 1} OF {closing_index + 1}", font=font(settings, 18), fill="#aab9ca")
    size = 32
    while True:
        face = font(settings, size)
        lines = wrap(draw, subtitle, face, 1160)
        if len(lines) <= 3 or size <= 18:
            break
        size -= 2
    height = len(lines) * (size + 8) + 28
    top = 700 - height
    draw.rounded_rectangle((28, top, 1252, 708), radius=16, fill="#101d2d")
    draw.multiline_text((60, top + 14), "\n".join(lines), font=face, fill="white", spacing=8)
    im.save(target)


def preflight(settings):
    # Detect render prerequisites before any potentially billable request.
    import shutil

    try:
        import PIL  # noqa: F401
    except ImportError:
        raise ValueError("Install backend video extras: Pillow is missing") from None

    if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
        raise ValueError("Install ffmpeg and ffprobe before creating a video")
    font(settings, 30)
    from .visuals import read_bundled_anchor

    read_bundled_anchor()


def render(job_id, brief, settings, jobs):
    job = jobs.get(job_id)
    root = Path(settings.video_output_dir).resolve() / job_id

    def update(stage, progress):
        job.update(status="running", stage=stage, progress=progress)
        jobs.save(job)

    try:
        root.mkdir(parents=True, exist_ok=True)
        update("validate", 1)
        parts = split_brief(brief)
        preflight(settings)
        manifest = {"brief_id": brief.id, "provider": job["provider"],
                    "visual_style": "cat-only-v1", "mouth_animation": "audio_energy_v1", "width": 1280, "height": 720, "scenes": []}
        (root / "brief.json").write_text(brief.model_dump_json(indent=2))
        for index, text in enumerate(parts):
            update(f"speech_{index + 1}", 5 + index * 17)
            cache = Path(settings.video_output_dir).resolve() / "audio"
            cache.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(
                json.dumps(
                    [job["provider"], settings.azure_speech_voice, text]
                ).encode()
            ).hexdigest()
            audio, timing = cache / f"{key}.wav", cache / f"{key}.json"
            if audio.exists() and timing.exists():
                boundaries = json.loads(timing.read_text())
            else:
                boundaries = synthesize(text, audio, settings, job["provider"])
                timing.write_text(json.dumps(boundaries))
            seconds = duration(audio)
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("Invalid audio duration")
            cues = captions(text, seconds, boundaries)
            update(f"cards_{index + 1}", 10 + index * 17)
            entries = []
            poses = mouth_timing(audio, seconds)
            # Split at both mouth and subtitle boundaries, preserving exact cue times.
            cuts = sorted({0.0, seconds, *(t for start, end, _ in poses for t in (start, end)),
                           *(t for start, end, _ in cues for t in (start, end))})
            rendered = set()
            cue_index = pose_index = 0
            for start, end in pairwise(cuts):
                if end <= start:
                    continue
                while cue_index + 1 < len(cues) and cues[cue_index][1] <= start:
                    cue_index += 1
                while pose_index + 1 < len(poses) and poses[pose_index][1] <= start:
                    pose_index += 1
                pose = poses[pose_index][2]
                filename = f"{index}-{cue_index}-{pose}.png"
                if filename not in rendered:
                    card(settings, brief, index, cues[cue_index][2], root / filename, pose)
                    rendered.add(filename)
                entries.extend([f"file '{filename}'", f"duration {end - start:.8f}"])
            entries.append(f"file '{filename}'")
            (root / f"{index}.txt").write_text("\n".join(entries) + "\n")
            update(f"encode_{index + 1}", 14 + index * 17)
            command(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(root / f"{index}.txt"),
                    "-i",
                    str(audio),
                    "-t",
                    str(seconds),
                    "-r",
                    "30",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "23",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    str(root / f"scene-{index}.mp4"),
                ]
            )
            manifest["scenes"].append(
                {
                    "text": text,
                    "audio": str(audio),
                    "duration": seconds,
                    "timing": "azure_words" if boundaries else "estimated",
                    "cues": cues,
                }
            )
        update("assemble", 93)
        (root / "scenes.txt").write_text(
            "".join(f"file 'scene-{i}.mp4'\n" for i in range(len(manifest["scenes"])))
        )
        command(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(root / "scenes.txt"),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(root / "video.mp4"),
            ]
        )
        update("verify", 98)
        manifest["duration"] = duration(root / "video.mp4")
        if (
            abs(manifest["duration"] - sum(s["duration"] for s in manifest["scenes"]))
            > 0.6
        ):
            raise ValueError("Encoded duration does not match audio")
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
        job.update(
            status="completed",
            stage="completed",
            progress=100,
            video_url=f"/api/videos/{job_id}/file",
        )
        jobs.save(job)
    except Exception as exc:  # noqa: BLE001 -- persist worker failures for retry
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else f"{type(exc).__name__}: video processing failed"
        )
        job.update(status="failed", error=message)
        jobs.save(job)


def main():
    """Explicit, non-Azure smoke test using installed macOS speech."""
    import argparse

    from .config import Settings
    from .storage import Storage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief-id", required=True)
    parser.add_argument(
        "--local-test",
        action="store_true",
        required=True,
        help="Use macOS say; no Azure calls; estimated subtitle timing",
    )
    args = parser.parse_args()
    settings = Settings()
    brief = Storage(settings.database_url).get(args.brief_id)
    if brief is None:
        parser.error("Saved brief not found")
    jobs = Jobs(settings.database_url)
    job = jobs.create(brief.id, "local")
    render(job["id"], brief, settings, jobs)
    result = jobs.get(job["id"])
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
