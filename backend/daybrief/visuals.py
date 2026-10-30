"""Resolve source images or a canonical cartoon anchor, separately from narration."""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import ProviderError
from .models import AnchorAsset, VisualAsset, VisualPlan, VisualScene
from .narrative import validate_saved_narrative
from .visual_http import PublicWeb, VisualFetchError, public_url
from .visual_images import MAX_IMAGE_BYTES, article_candidates, validated_image


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def visual_root(settings):
    # Existing named Docker volume already persists VIDEO_OUTPUT_DIR.
    return Path(settings.video_output_dir).resolve() / "visuals"


def asset_path(root, local_path):
    path = (root / local_path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Visual path is outside the asset directory")
    return path


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def usable_claims(event, claim_ids=None):
    lookup = {f"c{i}": c for i, c in enumerate(event.claims)}
    ids = claim_ids if claim_ids is not None else [
        key for key, c in lookup.items()
        if c.support_status == "supported" and c.classification == "confirmed_fact"
    ]
    if (not ids or len(ids) != len(set(ids)) or any(key not in lookup for key in ids)
            or any(lookup[key].support_status != "supported" or lookup[key].classification != "confirmed_fact" for key in ids)
            or not any(lookup[key].is_core for key in ids)):
        raise ProviderError("Visuals require supported confirmed evidence with a core fact")
    return [(key, lookup[key]) for key in ids]


def ranked_sources(event, claims, sources):
    strengths = {}
    for _, claim in claims:
        for url in claim.supporting_source_urls:
            strengths[str(url)] = strengths.get(str(url), 0) + (2 if claim.is_core else 1)
    articles = {str(a.url): a for a in sources}
    ordered = sorted({str(u) for u in event.source_urls}, key=lambda u: (-strengths.get(u, 0), u))
    result, seen = [], set()
    for url in ordered:
        article = articles.get(url)
        original = str(article.original_url or article.url) if article else url
        if original not in seen:
            result.append((original, article.publisher if article else urlsplit(original).hostname))
            seen.add(original)
    return result


class VisualResolver:
    def __init__(self, settings, *, web=None, mock=False):
        self.settings = settings
        self.root = visual_root(settings)
        self.web = web or PublicWeb(settings.request_timeout_seconds)
        self.mock = mock

    def cached_image(self, relative):
        path = asset_path(self.root, relative)
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        try:
            _, width, height = validated_image(path.read_bytes(), "image/jpeg")
            return width, height
        except VisualFetchError:
            return None

    async def resolve_article_image(self, article_url, event_id, publisher=None):
        try:
            article_url = public_url(article_url)
            response = await self.web.fetch(article_url, limit=2 * 1024 * 1024)
            if response.status != 200:
                return None
            mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if mime and mime not in ("text/html", "application/xhtml+xml"):
                return None
            if "noimageindex" in response.headers.get("x-robots-tag", "").lower():
                return None
            candidates = article_candidates(response.body.decode("utf-8", errors="replace"), response.url)
            for candidate in candidates:
                relative = f"{digest(event_id)[:24]}/article-{digest(candidate.url)[:24]}.jpg"
                cached = self.cached_image(relative)
                if cached:
                    width, height = cached
                else:
                    try:
                        image = await self.web.fetch(candidate.url, limit=MAX_IMAGE_BYTES)
                        if image.status != 200:
                            continue
                        content, width, height = validated_image(image.body, image.headers.get("content-type"))
                        atomic_write(asset_path(self.root, relative), content)
                    except VisualFetchError:
                        continue
                credit = candidate.credit[:500] if candidate.credit else None
                return VisualAsset(
                    type="article_image", url=candidate.url, local_path=relative,
                    source_url=article_url, publisher=publisher or urlsplit(article_url).hostname,
                    attribution=credit or "Image from the linked source article; rights remain with its owner.",
                    width=width, height=height,
                )
        except (VisualFetchError, ValueError, RecursionError):
            return None
        return None

    async def resolve_visual(self, event, sources=(), claim_ids=None):
        claims = usable_claims(event, claim_ids)
        if not self.mock:
            for article_url, publisher in ranked_sources(event, claims, sources)[:self.settings.visual_max_sources]:
                asset = await self.resolve_article_image(article_url, event.id, publisher)
                if asset:
                    return asset
        # One canonical cat replaces per-story image generation.
        return None


def read_bundled_anchor():
    """Validate the packaged asset without creating runtime directories."""
    bundled = Path(__file__).parent / "assets" / "anchor_neutral.png"
    data = bundled.read_bytes()
    validated_image(data, "image/png")
    return bundled, data


def canonical_anchor(settings):
    bundled, data = read_bundled_anchor()
    # Preserve the canonical PNG unchanged; all scenes reference the same file.
    sha = hashlib.sha256(data).hexdigest()
    relative = f"anchors/daybrief-cat-v1-{sha[:16]}.png"
    path = asset_path(visual_root(settings), relative)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        atomic_write(path, data)
    from PIL import Image

    with Image.open(bundled) as image:
        width, height = image.size
    return AnchorAsset(image_path=relative, style_id="daybrief-cat-v1", sha256=sha, width=width, height=height)


def existing_audio_timing(settings, brief):
    """Reuse completed audio only when every scene text matches the saved narration."""
    import math
    import sqlite3
    from uuid import UUID

    from .video import Jobs

    jobs = Jobs(settings.database_url)
    parts = [brief.opening, *(s.text for s in brief.narrative_segments or []), brief.closing]
    with sqlite3.connect(jobs.path) as db:
        rows = db.execute("SELECT payload FROM videos ORDER BY rowid DESC").fetchall()
    for (payload,) in rows:
        job = json.loads(payload)
        if job["brief_id"] != brief.id or job["status"] != "completed":
            continue
        try:
            UUID(job["id"])
            root = Path(settings.video_output_dir).resolve()
            manifest_path = (root / job["id"] / "manifest.json").resolve()
            if not manifest_path.is_relative_to(root):
                continue
            manifest = json.loads(manifest_path.read_text())
            if manifest["brief_id"] != brief.id or [s["text"] for s in manifest["scenes"]] != parts:
                continue
            durations = [float(s["duration"]) for s in manifest["scenes"]]
            if all(math.isfinite(d) and 0 < d <= 600 for d in durations):
                return job["id"], durations
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None, None


def brief_fingerprint(brief, anchor, audio_job_id=None, durations=None):
    return digest(json.dumps({
        "brief": brief.model_dump(mode="json"), "anchor": anchor.model_dump(),
        "audio_job_id": audio_job_id, "durations": durations,
        "visual_policy": "cat-only-v1",
    }, sort_keys=True))


async def build_visual_plan(brief, resolver, anchor=None, audio_job_id=None, durations=None):
    if brief.narrative_segments is None:
        raise ValueError("Visual planning requires a brief with validated narrative segments")
    validate_saved_narrative(brief)
    anchor = anchor or canonical_anchor(resolver.settings)
    parts = [brief.opening, *(s.text for s in brief.narrative_segments), brief.closing]
    if durations is not None and len(durations) != len(parts):
        raise ValueError("Audio timing must match the narration scene count")
    scenes = []
    for index, text in enumerate(parts):
        event_id = None
        kind = "intro" if index == 0 else "outro" if index == len(parts) - 1 else "story"
        if kind == "story":
            event = brief.selected_events[index - 1]
            segment = brief.narrative_segments[index - 1]
            event_id = event.id
            usable_claims(event, segment.supporting_claim_ids)
        scenes.append(VisualScene(
            kind=kind, event_id=event_id, narration_text=text,
            visual_mode="anchor_fullscreen",
            article_image=None, anchor_asset=anchor,
            duration_seconds=durations[index] if durations else round(len(text.split()) / 2.3, 2),
            duration_source="audio_manifest" if durations else "estimated_from_text",
            motion="anchor_breathe",
        ))
    return VisualPlan(
        brief_id=brief.id, brief_fingerprint=brief_fingerprint(brief, anchor, audio_job_id, durations),
        audio_job_id=audio_job_id, created_at=datetime.now(UTC), scenes=scenes,
    )
