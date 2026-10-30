import pytest

from daybrief.pipeline import normalize_text, snippet_contains_quote


@pytest.mark.parametrize("character", ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"])
def test_invisible_formatting_does_not_break_exact_quote(character):
    quote = "Two high-speed trains have collided in Spain"
    snippet = f"Two high-speed {character}trains have collided in Spain"
    assert snippet_contains_quote(snippet, quote)
    assert snippet_contains_quote(quote, snippet)
    assert normalize_text(f"{character}30{character} trains") == "30 trains"


@pytest.mark.parametrize("quote", [
    "Three high-speed trains have collided in Spain",
    "Two high-speed trains have collided in France",
    "Two high-speed trains have not collided in Spain",
    "Two high-speed trains in Spain have collided",
    "Two high speed trains have collided in Spain",
])
def test_real_wording_differences_are_not_normalized_away(quote):
    assert not snippet_contains_quote(
        "Two high-speed \u200btrains have collided in Spain", quote
    )


def test_invisible_character_removal_does_not_invent_word_boundaries():
    assert not snippet_contains_quote("30\u200bpeople", "30 people")
    assert normalize_text("30,000 people did not leave Madrid.") == (
        "30,000 people did not leave madrid."
    )
