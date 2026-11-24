"""Router: decides how a question that passed the input guard is answered.

    pay_rate  the question asks for a dollar amount, and the award, employment type and
              classification level are all known -> SQL on pay_rates (app/tools/pay_rates.py)
    clarify   it asks for an amount but one of those is missing -> ask for it (no LLM)
    rag       everything else -> cited clauses (search, re-rank, LLM)

Plain rules, no model: each decision can be tested and explained. The plan adds a small LLM
classifier only if the eval shows misroutes.

Amount questions about things the pay tool doesn't hold (overtime, evening and shift rates,
allowances, junior and apprentice rates) go to rag: the clauses explain the percentages,
and the model can't state an amount (amounts are masked in its sources and removed from its
answer).
"""

import re
from dataclasses import dataclass, field
from typing import Literal

AWARD_NAMES = {
    "MA000009": "Hospitality Industry (General) Award",
    "MA000004": "General Retail Industry Award",
    "MA000003": "Fast Food Industry Award",
    "MA000002": "Clerks—Private Sector Award",
}

# Industry words -> award. Fast food is checked before retail ("fast food store").
_AWARD_WORDS = [
    (
        "MA000003",
        (
            r"fast[- ]?food|takeaway|take-away|drive[- ]?thr(?:u|ough)|burgers?|fried chicken"
            r"|mcdonald'?s|maccas|kfc|hungry jack'?s"
        ),
    ),
    (
        "MA000009",
        (
            r"hospitality|hotels?|restaurants?|caf[eé]s?|\bbars?\b|\bpubs?\b|casinos?|motels?"
            r"|resorts?|waiters?|waitress|chefs?|cooks?|baristas?|bartenders?|kitchen hand"
        ),
    ),
    ("MA000004", r"retail|shops?\b|stores?\b|supermarkets?|checkout|sales assistant"),
    (
        "MA000002",
        (
            r"clerks?\b|clerical|office|admin(?:istration|istrative)?\b|receptionists?"
            r"|call cent(?:re|er)|data entry"
        ),
    ),
]
_AWARD_RE = [(award, re.compile(rf"\b(?:{words})", re.IGNORECASE)) for award, words in _AWARD_WORDS]

# Asks for an amount of money (not a percentage, a rule or a yes/no).
_PAY = re.compile(
    # "how much ... get/paid", but not "how much notice/leave/time off ..." ("full-time" is fine)
    r"\bhow\s+much\b(?:(?!\b(?:notice|leave|time\s+off|breaks?|hours?|overtime)\b).){0,60}"
    r"\b(?:get|getting|paid|earn|make|pay|per\s+hour|an\s+hour|a\s+week)\b"
    r"|\bearn(?:s|ing|ings)?\b"  # always money
    r"|\b(?:pay|hourly|weekly|base|minimum)\s+rates?\b"  # not "pay wages on a Saturday"
    r"|\b(?:hourly|weekly|base|minimum)\s+(?:wages?|pay)\b"
    r"|\brates?\s+of\s+pay\b"
    r"|\bminimum\s+(?:wage|pay|salary)\b"
    r"|\bwhat\s+(?:is|'s|are)\s+(?:the|my|a)\s+(?:\w+\s+){0,3}(?:wage|salary)s?\b"
    r"|\$\s?\d|\bdollars?\s+(?:an|per)\s+hour\b",
    re.IGNORECASE,
)
# Amounts the pay tool doesn't hold: answered from the clauses instead.
_NOT_IN_TOOL = re.compile(
    r"\b(?:overtime|evening|night|midnight|shift\s*work(?:er)?s?|late\s+(?:shift|work)"
    r"|early\s+morning"
    r"|allowances?|junior|apprentices?|trainees?|under\s+(?:1[5-9]|2[01])|aged?\s+1\d"
    r"|1\d[- ]year[- ]olds?|higher\s+duties|uniform|laundry|meal|tool|vehicle|travel"
    # NES and WorkSafe entitlements paid in other ways (step 15)
    r"|redundan\w*|severance|notice|leave|super(?:annuation)?|compensation|workcover"
    r"|weekly\s+payments)\b",
    re.IGNORECASE,
)
_TYPES = [
    ("casual", re.compile(r"\bcasuals?\b", re.IGNORECASE)),
    ("part_time", re.compile(r"\bpart[- ]?tim(?:e|er)s?\b", re.IGNORECASE)),
    ("full_time", re.compile(r"\bfull[- ]?tim(?:e|er)s?\b", re.IGNORECASE)),
]
_LEVEL = re.compile(r"\blevel\s*(\d{1,2}[ab]?)\b|\b(introductory)\b", re.IGNORECASE)
_DAYS = [
    ("saturday", re.compile(r"\bsaturdays?\b|\bweekends?\b", re.IGNORECASE)),
    ("sunday", re.compile(r"\bsundays?\b|\bweekends?\b", re.IGNORECASE)),
    ("public_holiday", re.compile(r"\bpublic\s+holidays?\b", re.IGNORECASE)),
]
_CASINO = re.compile(r"\bcasinos?\b|\bgaming\b", re.IGNORECASE)


@dataclass
class Decision:
    route: Literal["rag", "pay_rate", "clarify"]
    award: str | None = None
    employment_type: str | None = None
    level: str | None = None
    days: tuple[str, ...] = ("ordinary",)
    casino: bool = False
    missing: list[str] = field(default_factory=list)  # for clarify: award, employment_type, level
    reason: str = ""


def detect_award(text: str) -> str | None:
    """The award named by industry words, or None if none or more than one match."""
    found = [award for award, rx in _AWARD_RE if rx.search(text)]
    if "MA000003" in found and "MA000004" in found:  # "fast food store"
        found.remove("MA000004")
    return found[0] if len(found) == 1 else None


def detect_employment_type(text: str) -> str | None:
    found = [t for t, rx in _TYPES if rx.search(text)]
    return found[0] if len(found) == 1 else None


def detect_level(text: str) -> str | None:
    found = {(m.group(1) or m.group(2)).lower() for m in _LEVEL.finditer(text)}
    return found.pop() if len(found) == 1 else None


_YES_NO = re.compile(
    r"^\s*(?:is|are|do|does|did|can|could|should|must|will|would|has|have)\b", re.IGNORECASE
)


_MONEY_WORD = re.compile(r"\b(?:pay|paid|rates?|wages?|salary)\b", re.IGNORECASE)


def is_pay_question(text: str) -> bool:
    """Asks for an amount: an amount phrase, or a money word next to both a level and an
    employment type ("pay for a part-time level 3 cook on Saturday?"). Yes/no questions ("Is
    the pay rate higher on Sundays?") ask about a rule, so they go to the clauses."""
    if _YES_NO.match(text):
        return False
    if _PAY.search(text):
        return True
    return bool(_MONEY_WORD.search(text) and detect_level(text) and detect_employment_type(text))


def route_question(text: str, award: str | None = None) -> Decision:
    """`text` is the guarded (normalised, redacted) question; `award` the one picked in the
    UI or API request, which wins over words in the question."""
    if not is_pay_question(text):
        return Decision("rag", award=award, reason="not an amount question")
    if _NOT_IN_TOOL.search(text):
        return Decision("rag", award=award, reason="amount the pay tool doesn't hold")

    d = Decision(
        "pay_rate",
        award=award or detect_award(text),
        employment_type=detect_employment_type(text),
        level=detect_level(text),
        days=tuple(day for day, rx in _DAYS if rx.search(text)) or ("ordinary",),
        casino=bool(_CASINO.search(text)),
    )
    d.missing = [name for name in ("award", "employment_type", "level") if getattr(d, name) is None]
    if d.missing:
        d.route = "clarify"
        d.reason = "missing " + ", ".join(d.missing)
    return d


def clarify_text(d: Decision, levels: list[str] | None = None) -> str:
    """The clarifying question, written by code (no LLM call)."""
    asks = []
    if "award" in d.missing:
        names = ", ".join(AWARD_NAMES.values())
        asks.append(f"which award covers your job (I have pay rates for the {names})")
    if "employment_type" in d.missing:
        asks.append("whether you are casual, part-time or full-time")
    if "level" in d.missing:
        if levels:
            asks.append(f"your classification level ({', '.join(levels)})")
        else:
            asks.append("your classification level (it is on your payslip or contract)")
    if len(asks) > 1:
        asks[-1] = "and " + asks[-1]
    sep = "; " if len(asks) > 2 else " "
    return f"To give you the exact rate I need to know {sep.join(asks)}."
