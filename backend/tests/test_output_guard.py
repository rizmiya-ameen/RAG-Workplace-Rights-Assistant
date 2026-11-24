"""Output guard: dollar amounts only from the pay tool, PII redacted."""

import pytest

from app.guardrails.output_guard import AMOUNT_REMOVED, check_amounts, check_output


@pytest.mark.parametrize(
    "answer",
    [
        "A Level 2 casual gets $47.39 an hour on Sunday.",
        "The allowance is $ 15.33 per week.",
        "It is A$1,029.10 a week.",
        "You get 33.85 dollars an hour.",
        "That is AUD 27.08.",
    ],
)
def test_amounts_not_from_the_tool_are_removed(answer):
    cleaned, removed = check_amounts(answer)
    assert removed == 1
    assert AMOUNT_REMOVED in cleaned
    for number in ("47.39", "15.33", "1,029.10", "33.85", "27.08"):
        assert number not in cleaned


def test_amounts_from_the_tool_are_kept():
    allowed = frozenset({"47.39", "1029.10"})
    answer = "Sunday is $47.39 an hour and the weekly rate is $1,029.10, not $50."
    cleaned, removed = check_amounts(answer, allowed)
    assert "$47.39" in cleaned and "$1,029.10" in cleaned
    assert removed == 1 and "$50" not in cleaned


@pytest.mark.parametrize(
    "answer",
    [
        "Casuals get a 25% loading (MA000009:11.1).",
        "Overtime is paid at 150% for the first 2 hours under clause 28.2.",
        "Clause 16.10 applies from 1 July 2026, 38 hours a week.",
    ],
)
def test_percentages_clauses_and_dates_are_untouched(answer):
    assert check_amounts(answer) == (answer, 0)


def test_check_output_reports_events():
    answer, events = check_output("Pay is $30.00. Email jane.citizen@example.com for help.")
    assert "$30.00" not in answer and "jane.citizen@example.com" not in answer
    assert events == [("amount_removed", {"count": 1}), ("pii_redacted", {"EMAIL": 1})]


def test_clean_answer_has_no_events():
    text = "Casuals get a 25% loading (MA000009:11.1)."
    assert check_output(text) == (text, [])
