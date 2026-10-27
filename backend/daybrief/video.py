"""Saved narration -> five timed scenes. No news or language-model calls."""

import hashlib
import json
import math
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


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
    parts = brief.narration.split("\n\n")
    if len(parts) != 5 or parts[0] != brief.opening or parts[-1] != brief.closing:
        raise ValueError(
            "Saved narration must contain opening, three stories and closing"
        )
    if any(not p.strip() for p in parts):
        raise ValueError("Saved narration contains an empty scene")
    # Older or edited records must still consist of saved, verified claim text.
    for text, event in zip(parts[1:4], brief.selected_events, strict=True):
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


def card(settings, brief, index, subtitle, target):
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (1080, 1920), "#101d2d")
    draw = ImageDraw.Draw(im)
    draw.rectangle((65, 120, 155, 130), fill="#62e0bc")
    draw.text((65, 170), "DAYBRIEF", font=font(settings, 44), fill="#62e0bc")
    label = ["OPENING", "STORY 01", "STORY 02", "STORY 03", "CLOSING"][index]
    draw.text((65, 265), label, font=font(settings, 28), fill="#aab9ca")
    title = (
        "Your daily briefing"
        if index == 0
        else "Thanks for watching"
        if index == 4
        else brief.selected_events[index - 1].headline
    )
    size = 66
    while size > 30:
        face = font(settings, size)
        lines = wrap(draw, title, face, 940)
        if len(lines) * (size + 16) < 650:
            break
        size -= 2
    draw.multiline_text(
        (65, 370), "\n".join(lines), font=face, fill="white", spacing=16
    )
    if 0 < index < 4:
        event = brief.selected_events[index - 1]
        sources = sorted(
            {
                urlsplit(str(u)).hostname
                for c in event.claims
                if c.support_status == "supported"
                and c.classification == "confirmed_fact"
                for u in c.supporting_source_urls
            }
        )
        draw.text(
            (65, 1070), "VERIFIED SOURCES", font=font(settings, 25), fill="#62e0bc"
        )
        source_text = " / ".join(sources[:6])
        draw.multiline_text(
            (65, 1120),
            "\n".join(wrap(draw, source_text, font(settings, 27), 940)),
            font=font(settings, 27),
            fill="#c7d3df",
            spacing=10,
        )
    else:
        draw.text(
            (65, 1090),
            "Source links accompany the saved brief.",
            font=font(settings, 30),
            fill="#c7d3df",
        )
    draw.rounded_rectangle((45, 1420, 1035, 1760), radius=25, fill="#22344b")
    draw.multiline_text(
        (80, 1470),
        "\n".join(wrap(draw, subtitle, font(settings, 46), 900)),
        font=font(settings, 46),
        fill="white",
        spacing=16,
    )
    footer = (
        "FICTIONAL DEMO"
        if brief.mode == "mock"
        else brief.generated_at.strftime("%Y-%m-%d")
    )
    draw.text(
        (65, 1810),
        f"{footer}   /   {index + 1} OF 5",
        font=font(settings, 25),
        fill="#aab9ca",
    )
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
        manifest = {"brief_id": brief.id, "provider": job["provider"], "scenes": []}
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
            for j, (start, end, subtitle) in enumerate(cues):
                filename = f"{index}-{j}.png"
                card(settings, brief, index, subtitle, root / filename)
                entries.extend([f"file '{filename}'", f"duration {end - start:.8f}"])
            entries.append(f"file '{index}-{len(cues) - 1}.png'")
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
            "".join(f"file 'scene-{i}.mp4'\n" for i in range(5))
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
