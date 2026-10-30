"""Presentation registry. Character preferences never enter news-model inputs."""

from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


class AnchorCharacter(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    asset_path: str
    blink_asset_path: str | None = None
    style_id: str
    # Mouth center/top in the 1280x720 video canvas, calibrated per asset.
    mouth_x: int
    mouth_y: int
    eye_centers: tuple[tuple[int, int], tuple[int, int]]
    ear_centers: tuple[tuple[int, int], tuple[int, int]]


ANCHORS = {
    key: AnchorCharacter(id=key, name=name, asset_path=filename,
                         blink_asset_path=f"{key}_blink.png",
                         style_id=f"daybrief-{key}-v1", mouth_x=x, mouth_y=y,
                         eye_centers=eyes, ear_centers=ears)
    for key, name, filename, x, y, eyes, ears in [
        ("cat", "Cat", "anchor_neutral.png", 626, 260, ((580, 192), (676, 196)), ((536, 68), (741, 78))),
        ("dog", "Dog", "dog_anchor.png", 624, 256, ((576, 167), (671, 168)), ((486, 178), (754, 178))),
        ("fox", "Fox", "fox_anchor.png", 626, 268, ((580, 192), (676, 196)), ((535, 50), (754, 69))),
    ]
}


def validate_anchor(value):
    if value not in ANCHORS:
        raise ValueError("Unknown anchor character")
    return value


AnchorId = Annotated[str, AfterValidator(validate_anchor)]


def anchor_character(anchor_id="cat"):
    return ANCHORS[validate_anchor(anchor_id)]


def bundled_anchor_path(anchor_id="cat"):
    return Path(__file__).parent / "assets" / anchor_character(anchor_id).asset_path


def presentation_lines(events):
    """Five-word bookends: no story-specific humor or model-generated banter."""
    if any(event.sensitivity != "normal" for event in events):
        return "Welcome. Here is your DayBrief.", "Thank you for joining us."
    options = [
        ("Your little newsroom is ready.", "Stay curious. See you soon!"),
        ("Hello! Your pocket newsroom awaits.", "Until next time, stay curious!"),
    ]
    return options[(len(events) - 1) % len(options)]
