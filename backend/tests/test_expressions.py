from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageOps
from pydantic import ValidationError

from daybrief.anchors import anchor_character, bundled_anchor_path
from daybrief.config import Settings
from daybrief.expressions import AnchorRig, expression_profile, expression_state
from daybrief.video_api import VideoRequest


def brief(*sensitivities):
    return SimpleNamespace(selected_events=[SimpleNamespace(sensitivity=s) for s in sensitivities])


def test_default_and_environment(monkeypatch):
    monkeypatch.delenv('EXPRESSION_INTENSITY', raising=False)
    assert Settings(_env_file=None).expression_intensity == 0.7
    monkeypatch.setenv('EXPRESSION_INTENSITY', '0.4')
    assert Settings(_env_file=None).expression_intensity == 0.4


@pytest.mark.parametrize('value', [-0.01, 1.01, float('inf'), float('nan')])
def test_invalid_intensity(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, expression_intensity=value)
    with pytest.raises(ValidationError):
        VideoRequest(expression_intensity=value)


def test_bookends_and_serious_story_restraint():
    normal = brief('normal')
    assert expression_profile(normal, 0).intensity == pytest.approx(0.84)
    assert expression_profile(normal, 2).intensity == pytest.approx(0.84)
    assert expression_profile(normal, 1).intensity == 0.7
    mixed = brief('normal', 'critical')
    assert expression_profile(mixed, 1).intensity == 0.7
    for index in (0, 2, 3):
        profile = expression_profile(mixed, index)
        assert profile.restrained
        assert profile.intensity == pytest.approx(0.175)
        assert expression_state(1, 5, profile, 1)['ears'] == 0
    assert expression_profile(normal, 0, 1).intensity == 1


def test_motion_is_bounded_smooth_and_deterministic():
    profile = expression_profile(brief('normal'), 1)
    state = expression_state(1, 5, profile, 1)
    for key in ('brow', 'gaze', 'head_x', 'head_y', 'ears', 'body', 'mouth'):
        assert state[key] != 0
    assert state == expression_state(1, 5, profile, 1)
    nearby = expression_state(1.001, 5, profile, 1)
    assert all(abs(state[k] - nearby[k]) < 0.02 for k in state)
    assert expression_state(1.8, 5, profile)['blink'] == pytest.approx(1)
    assert expression_state(1, 5, profile)['mouth'] == 0
    for tick in range(120):
        state = expression_state(tick / 24, 5, profile, 1)
        assert 0 <= state['blink'] <= 1
        assert abs(state['head_y']) <= 4.2
        assert abs(state['body']) <= 1.75


@pytest.mark.parametrize('anchor', ['cat', 'dog', 'fox'])
def test_blink_only_changes_eye_regions_and_zero_is_still(anchor):
    with Image.open(bundled_anchor_path(anchor)) as source:
        base = ImageOps.fit(source.convert('RGB'), (1280, 720))
    character = anchor_character(anchor)
    rig = AnchorRig(base, character)
    zero = expression_state(1.8, 5, expression_profile(brief('normal'), 1, 0), 1)
    assert not any(zero.values())
    assert ImageChops.difference(base, rig.frame(zero)).getbbox() is None
    closed = rig.frame({**zero, 'blink': 1})
    assert ImageChops.difference(base, closed).getbbox() is not None
    for cx, cy in character.eye_centers:
        closed.paste(base.crop((cx-43, cy-29, cx+43, cy+32)), (cx-43, cy-29))
    assert ImageChops.difference(base, closed).getbbox() is None
