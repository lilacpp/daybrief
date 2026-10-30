"""Deterministic, bounded 2D anchor rig. Bundled blink poses; no runtime provider calls."""

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

FPS = 24


@dataclass(frozen=True)
class ExpressionProfile:
    intensity: float
    restrained: bool
    bookend: bool


def expression_profile(brief, index, requested=0.7):
    if not math.isfinite(requested) or not 0 <= requested <= 1:
        raise ValueError("expression_intensity must be between 0 and 1")
    bookend = index in (0, len(brief.selected_events) + 1)
    serious = (any(e.sensitivity != "normal" for e in brief.selected_events) if bookend
               else brief.selected_events[index - 1].sensitivity != "normal")
    effective = requested * (0.25 if serious else 1.2 if bookend else 1.0)
    return ExpressionProfile(min(1.0, effective), serious, bookend)


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def expression_state(t, seconds, profile, speech=0):
    # Ease into/out of each scene; phases are fixed, never random frame jitter.
    envelope = smoothstep(t / 0.45) * smoothstep((seconds - t) / 0.45)
    amount = profile.intensity * envelope
    period = 4.8 if profile.restrained else 3.8
    distance = abs((t - 1.8 + period / 2) % period - period / 2)
    blink = 0.5 + 0.5 * math.cos(math.pi * distance / 0.18) if distance < 0.18 else 0
    # Blinks close naturally at any nonzero intensity; serious scenes blink less often.
    blink *= envelope * min(1, profile.intensity / 0.15)
    return {
        "blink": blink,
        "brow": amount * (0.5 + 0.5 * math.sin(t * 1.4)) * (0.5 if profile.restrained else 3.5),
        "gaze": amount * math.sin(t * 0.85) * 2,
        "head_x": amount * math.sin(t * 0.7) * 2.5,
        "head_y": amount * math.sin(t * 2.0) * (2 if profile.restrained else 6),
        "ears": 0 if profile.restrained else amount * math.sin(t * 1.7) * 3.5,
        "body": amount * math.sin(t * 1.3) * 2.5,
        "mouth": max(0, min(1, speech)) * profile.intensity,
    }


def window(x, y, cx, cy, rx, ry):
    """Compact smooth falloff: pixels outside the rig region never move."""
    dx, dy = abs(x - cx) / rx, abs(y - cy) / ry
    if dx >= 1 or dy >= 1:
        return 0.0
    return (0.5 + 0.5 * math.cos(math.pi * dx)) * (0.5 + 0.5 * math.cos(math.pi * dy))


class AnchorRig:
    def __init__(self, image, character):
        self.base = image.convert("RGB")
        self.character = character
        self.closed = None
        if character.blink_asset_path:
            with Image.open(Path(__file__).parent / "assets" / character.blink_asset_path) as closed:
                self.closed = ImageOps.fit(closed.convert("RGB"), self.base.size)
        # Only the presenter region deforms. Desk, captions and studio stay stable.
        self.box = (400, 0, 900, 590)
        self.xs = list(range(0, 500, 20)) + [500]
        self.ys = list(range(0, 590, 20)) + [590]

    def blink(self, image, center, amount):
        if amount <= 0.001 or self.closed is None:
            return
        cx, cy = center
        box = (cx - 43, cy - 29, cx + 43, cy + 32)
        mask = Image.new("L", (86, 61))
        ImageDraw.Draw(mask).rounded_rectangle((3, 3, 82, 57), radius=12, fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(2)).point(lambda v: round(v * amount))
        image.paste(self.closed.crop(box), box, mask)

    def frame(self, state):
        image = self.base.copy()
        if not any(state.values()):
            return image
        character = self.character
        for eye in character.eye_centers:
            self.blink(image, eye, state["blink"])
        mouth = state["mouth"]
        if mouth > 0.015:
            draw = ImageDraw.Draw(image)
            x, y = character.mouth_x, character.mouth_y
            half_width = 11 + 4 * mouth
            depth = 3 + 27 * mouth
            draw.ellipse((x-half_width, y, x+half_width, y+depth), fill="#39232c", outline="#503630", width=1)
            if depth > 12:
                draw.ellipse((x-5, y+depth-6, x+7, y+depth-2), fill="#b77780")
        def source(x, y):
            gx, gy = x + 400, y
            head = window(gx, gy, character.mouth_x, character.mouth_y-65, 190, 205)
            body = window(gx, gy, 640, 430, 220, 160)
            sx = x - state["head_x"] * head
            sy = y - state["head_y"] * head - state["body"] * body
            for ex, ey in character.eye_centers:
                sx -= state["gaze"] * window(gx, gy, ex, ey, 38, 30)
                sy += state["brow"] * window(gx, gy, ex, ey-36, 45, 22)
            for side, (ex, ey) in enumerate(character.ear_centers):
                sx -= state["ears"] * (1 if side else -1) * window(gx, gy, ex, ey, 65, 75)
            return sx, sy
        points = {(x,y): source(x,y) for x in self.xs for y in self.ys}
        mesh = []
        for y, bottom in zip(self.ys, self.ys[1:]):
            for x, right in zip(self.xs, self.xs[1:]):
                corners = [points[x,y], points[x,bottom], points[right,bottom], points[right,y]]
                mesh.append(((x,y,right,bottom), tuple(v for point in corners for v in point)))
        patch = image.crop(self.box)
        image.paste(patch.transform(patch.size, Image.Transform.MESH, mesh, Image.Resampling.BICUBIC), self.box)
        return image
