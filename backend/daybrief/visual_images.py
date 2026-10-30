"""Article metadata extraction and raster validation. No language-model calls."""

import io
import json
import re
import warnings
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from .visual_http import VisualFetchError, public_url

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000


@dataclass
class Candidate:
    url: str
    priority: int
    width: int = 0
    height: int = 0
    credit: str | None = None


class ArticleMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.candidates = []
        self.jsonld = []
        self.script = None
        self.article_depth = 0
        self.blocked = False
        self.last_og = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "article":
            self.article_depth += 1
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self.script = []
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            value = a.get("content") or ""
            if key in ("robots", "googlebot") and "noimageindex" in value.lower():
                self.blocked = True
            if key in ("og:image", "og:image:url", "og:image:secure_url"):
                self.last_og = Candidate(value, 0)
                self.candidates.append(self.last_og)
            elif key in ("twitter:image", "twitter:image:src"):
                self.candidates.append(Candidate(value, 1))
            elif self.last_og and key in ("og:image:width", "og:image:height"):
                dimension = "width" if key.endswith("width") else "height"
                setattr(self.last_og, dimension, integer(value))
            elif self.last_og and key == "og:image:alt" and logo_like(value):
                self.candidates.remove(self.last_og)
                self.last_og = None
        if tag == "img" and self.article_depth and not logo_like(" ".join(str(v or "") for v in a.values())):
            self.candidates.append(Candidate(
                a.get("src") or a.get("data-src") or "", 3,
                integer(a.get("width")), integer(a.get("height")),
            ))

    def handle_endtag(self, tag):
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
        if tag == "script" and self.script is not None:
            self.jsonld.append("".join(self.script))
            self.script = None

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)


def integer(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        return 0


def logo_like(value):
    return bool(re.search(r"(?:^|[\W_])(logo\w*|avatar\w*|icon\w*|favicon|tracking|pixel|advert\w*|ads?)(?:[\W_]|$)", value, re.IGNORECASE))


def dimensions_ok(width, height):
    return width >= 640 and height >= 360 and 0.8 <= width / height <= 2.5 and width * height <= MAX_PIXELS


def article_candidates(html, page_url):
    parser = ArticleMetadata()
    parser.feed(html)

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            types = node.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if not isinstance(types, list):
                types = []
            if any(t in ("Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting") for t in types):
                if node.get("isAccessibleForFree") in (False, "false", "False"):
                    parser.blocked = True
                images = node.get("image", [])
                if not isinstance(images, list):
                    images = [images]
                for item in images:
                    if isinstance(item, str):
                        parser.candidates.append(Candidate(item, 2))
                    elif isinstance(item, dict):
                        credit = item.get("creditText") or item.get("copyrightNotice")
                        parser.candidates.append(Candidate(
                            item.get("contentUrl") or item.get("url") or "", 2,
                            integer(item.get("width")), integer(item.get("height")),
                            credit if isinstance(credit, str) else None,
                        ))
            for key, child in node.items():
                if isinstance(child, (dict, list)) and key != "image":
                    visit(child)

    for raw in parser.jsonld:
        try:
            visit(json.loads(raw))
        except (ValueError, RecursionError):
            continue
    if parser.blocked:
        return []
    candidates = {}
    for candidate in parser.candidates:
        if not isinstance(candidate.url, str) or not candidate.url or logo_like(candidate.url):
            continue
        try:
            candidate.url = public_url(urljoin(page_url, candidate.url))
        except VisualFetchError:
            continue
        if ((candidate.width and candidate.width < 640) or (candidate.height and candidate.height < 360)
                or (candidate.width and candidate.height and not dimensions_ok(candidate.width, candidate.height))):
            continue
        # Retain credit from JSON-LD even when the same image is also in og:image.
        old = candidates.get(candidate.url)
        if old:
            old.credit = old.credit or candidate.credit
            old.width = old.width or candidate.width
            old.height = old.height or candidate.height
            old.priority = min(old.priority, candidate.priority)
        else:
            candidates[candidate.url] = candidate
    return sorted(candidates.values(), key=lambda c: (
        c.priority, not (c.width > c.height > 0), -(c.width * c.height),
    ))[:4]


def validated_image(data, content_type=None):
    """Decode, bound dimensions, strip metadata, and return a normalized JPEG."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data or len(data) > MAX_IMAGE_BYTES:
        raise VisualFetchError("Invalid image size")
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime and mime not in ("image/jpeg", "image/png", "image/webp", "application/octet-stream"):
        raise VisualFetchError("Unsupported image MIME type")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("JPEG", "PNG", "WEBP") or not dimensions_ok(*image.size):
                    raise VisualFetchError("Unsuitable image dimensions or format")
                if getattr(image, "is_animated", False):
                    raise VisualFetchError("Animated article images are not supported")
                image.load()
                image = ImageOps.exif_transpose(image).convert("RGB")
                if not dimensions_ok(*image.size):
                    raise VisualFetchError("Unsuitable oriented image dimensions")
                image.thumbnail((1920, 1080))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=90)
                return output.getvalue(), image.width, image.height
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise VisualFetchError("Invalid or unsafe raster image") from None
