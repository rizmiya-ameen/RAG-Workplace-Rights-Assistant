"""Router: pay_rate / clarify / rag decisions and the slot detectors."""

import json

import pytest

from app.config import REPO_DIR
from app.guardrails.input_guard import check_input
from app.router import (
    Decision,
    clarify_text,
    detect_award,
    detect_employment_type,
    detect_level,
    is_pay_question,
    route_question,
)


@pytest.mark.parametrize(
    ("text", "award"),
    [
        ("I work in a hotel bar", "MA000009"),
        ("casual barista at a cafe", "MA000009"),
        ("checkout operator at a supermarket", "MA000004"),
        ("crew member at a fast food store", "MA000003"),  # not retail, despite "store"
        ("receptionist in an office", "MA000002"),
        ("call centre worker", "MA000002"),
        ("I am a nurse", None),
        ("a cook in a shop", None),  # two awards named: don't guess
    ],
)
def test_detect_award(text, award):
    assert detect_award(text) == award


def test_detect_employment_type_and_level():
    assert detect_employment_type("casuals on Sunday") == "casual"
    assert detect_employment_type("a part-timer") == "part_time"
    assert detect_employment_type("full time staff") == "full_time"
    assert detect_employment_type("casual or part-time?") is None
    assert detect_level("Level 2 casual") == "2"
    assert detect_level("level 3A at a casino") == "3a"
    assert detect_level("an introductory level casual") == "introductory"
    assert detect_level("level 1 or level 2") is None
    assert detect_level("a cook") is None


@pytest.mark.parametrize(
    "text",
    [
        "How much does a level 2 casual get?",
        "What's the minimum wage for retail?",
        "What is the hourly rate for a cook?",
        "What's my pay rate as a casual?",
        "How much does a full-time clerk earn per week?",
        "What is the weekly wage for a level 1 clerk?",
    ],
)
def test_amount_questions(text):
    assert is_pay_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "Can a hospitality employer pay wages on a Saturday?",  # golden hosp-11
        "Is the pay rate higher on Sundays?",  # yes/no: a rule, not an amount
        "How much notice do I have to give?",
        "How much annual leave do I get?",
        "What is the casual loading?",
        "What is the Saturday rate for clerks?",  # a percentage, from the clauses
    ],
)
def test_not_amount_questions(text):
    assert not is_pay_question(text)


def test_pay_route_with_all_slots():
    d = route_question("How much does a level 2 casual get in a hotel on weekends?")
    assert d == Decision(
        "pay_rate",
        award="MA000009",
        employment_type="casual",
        level="2",
        days=("saturday", "sunday"),
    )


def test_request_award_wins_over_words():
    d = route_question("How much does a level 1 casual get in a hotel?", "MA000004")
    assert d.award == "MA000004"


def test_missing_slots_clarify():
    d = route_question("What's the minimum wage for a full-time worker?")
    assert d.route == "clarify" and d.missing == ["award", "level"]


def test_amounts_the_tool_does_not_hold_go_to_the_clauses():
    for text in (
        "How much overtime pay does a level 2 casual get in a hotel?",
        "How much is a junior casual paid in retail?",
        "How much do I get paid for working past midnight in fast food?",
        "What is the hourly rate for a level 1 apprentice cook?",
    ):
        assert route_question(text).route == "rag", text


def test_clarify_text():
    d = Decision("clarify", award="MA000003", missing=["employment_type", "level"])
    text = clarify_text(d, ["Level 1", "Level 2", "Level 3"])
    assert "casual, part-time or full-time" in text and "(Level 1, Level 2, Level 3)" in text
    d = Decision("clarify", missing=["award", "employment_type", "level"])
    text = clarify_text(d)
    assert "Fast Food Industry Award" in text and "payslip" in text


def test_golden_questions_keep_their_route():
    """Answerable golden questions stay on rag, except hosp-23, which asks for an exact rate
    (the pay tool cites B.2.3, one of its expected clauses). Decline questions never get a pay
    answer."""
    for line in (REPO_DIR / "eval" / "golden_set.jsonl").read_text(encoding="utf-8").splitlines():
        g = json.loads(line)
        checked = check_input(g["question"])
        if checked.route != "pass":
            continue
        route = route_question(checked.text, g["award"]).route
        if g["id"] == "hosp-23":
            assert route == "pay_rate"
        elif g["expected"]:
            assert route == "rag", g["id"]
        else:
            assert route in ("rag", "clarify"), g["id"]
