"""The segmenter releases whole sentences or lines, never empty pieces (FR-016, research §10)."""

import pytest

from ecommerce_ai.agents.assistant.segmenter import Segmenter


def _segments(*deltas: str) -> list[str]:
    segmenter = Segmenter()
    segments = [segment for delta in deltas for segment in segmenter.feed(delta)]
    rest = segmenter.finish()
    return segments if rest is None else [*segments, rest]


@pytest.mark.parametrize("end", [".", "!", "?"])
def test_should_split_after_a_sentence_end_followed_by_a_space(end: str) -> None:
    assert _segments(f"First{end} Second{end}") == [f"First{end} ", f"Second{end}"]


def test_should_split_after_a_newline_and_keep_list_lines_apart() -> None:
    text = "Your orders:\n- A is SHIPPED\n- B is PAID"

    assert _segments(text) == ["Your orders:\n", "- A is SHIPPED\n", "- B is PAID"]


def test_should_release_a_sentence_only_once_the_next_text_starts() -> None:
    segmenter = Segmenter()

    assert segmenter.feed("It is SHIPPED.") == []
    assert segmenter.feed(" ") == []
    assert segmenter.feed("  ") == []
    assert segmenter.feed("It left") == ["It is SHIPPED.   "]
    assert segmenter.finish() == "It left"


def test_should_join_sentences_split_across_many_deltas() -> None:
    deltas = ["Yo", "ur ord", "er is PA", "ID", ".", " ", "Tot", "al 10", ".", "50 EUR."]

    assert _segments(*deltas) == ["Your order is PAID. ", "Total 10.50 EUR."]


@pytest.mark.parametrize(
    "text",
    [
        "The total is 10.50 EUR",
        "Order 3f2a.b1c9-77 is PAID",
        "Version 1.2.3 here",
        "Really?!",
        "Wait...",
    ],
)
def test_should_not_split_inside_a_token_or_at_the_text_end(text: str) -> None:
    assert _segments(text) == [text]


def test_should_never_release_an_empty_or_blank_segment() -> None:
    segments = _segments("\n\n  ", "Hello.\n\n\n", "  \n", "World.  ", "\n")

    assert segments == ["\n\n  Hello.\n\n\n  \n", "World.  \n"]
    assert all(segment.strip() for segment in segments)


@pytest.mark.parametrize("deltas", [(), ("",), ("  ", "\n")])
def test_should_return_no_rest_when_the_turn_had_only_blank_text(deltas: tuple[str, ...]) -> None:
    assert _segments(*deltas) == []


def test_should_concatenate_back_to_the_exact_text() -> None:
    text = "  Hi! Your order is SHIPPED.\n- Item A: 2.00\n\nAnything else? Ask me. "
    deltas = [text[i : i + 3] for i in range(0, len(text), 3)]

    assert "".join(_segments(*deltas)) == text


def test_should_start_empty_again_after_finish() -> None:
    segmenter = Segmenter()
    segmenter.feed("First answer")

    assert segmenter.finish() == "First answer"
    assert segmenter.finish() is None
    assert segmenter.feed("Next. Turn") == ["Next. "]
