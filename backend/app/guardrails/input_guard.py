"""Input guard: runs on every question before retrieval or the LLM.

Order (first match decides the route):
1. length limit                      -> blocked
2. normalise (NFKC, zero-width and control characters removed, whitespace collapsed)
3. PII redaction (email, phone, TFN with checksum, BSB, account number) -> question continues
   with placeholders such as [EMAIL]; values are never logged
4. prompt-injection patterns         -> blocked  (OWASP LLM01)
5. evasion patterns                  -> blocked  ("without getting caught", fake timesheets)
6. personal advice / dispute         -> refer    (Fair Work Ombudsman)
7. injection classifier (step 16)    -> blocked  (app/guardrails/classifier.py: catches
   rephrased attacks the patterns miss, e.g. base64 or letter-spaced instructions; runs on
   the normalised, redacted text; score >= injection_threshold)
8. scope: no workplace words at all  -> out_of_scope

Patterns are deliberately narrow: a worker asking "Can my employer avoid paying penalty
rates?" or "Can my boss ignore my roster request?" must still get an answer. Every rule has
tests, and eval/redteam.jsonl holds attack rows plus look-alike legitimate questions.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

MAX_CHARS = 1000

InputRoute = Literal["pass", "blocked", "refer", "out_of_scope"]

_I = re.IGNORECASE


@dataclass
class InputResult:
    route: InputRoute
    text: str  # normalised, PII-redacted question (what retrieval and the LLM see)
    rule: str | None = None  # rule that set the route, e.g. "injection:ignore_instructions"
    redactions: dict[str, int] = field(default_factory=dict)  # PII type -> count
    score: float | None = None  # injection classifier score, when it ran


# --- normalisation ------------------------------------------------------------------------

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = "".join(ch if ch in "\n\t" or unicodedata.category(ch)[0] != "C" else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


# --- PII ----------------------------------------------------------------------------------

_TFN_WEIGHTS = (1, 4, 3, 7, 5, 8, 6, 9, 10)


def is_tfn(digits: str) -> bool:
    """Australian Tax File Number checksum (9 digits, weighted sum divisible by 11)."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    return sum(int(d) * w for d, w in zip(digits, _TFN_WEIGHTS, strict=True)) % 11 == 0


_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Mobiles (04xx xxx xxx, +61 4xx ...) and landlines (0[2378] xxxx xxxx). 13 13 94 is not matched.
_PHONE = re.compile(
    r"(?<![\w+])(?:\+?61[\s-]?|\(?0)(?:4\d{2}[\s-]?\d{3}[\s-]?\d{3}"
    r"|[2378]\)?[\s-]?\d{4}[\s-]?\d{4})(?!\d)"
)
_NINE_DIGITS = re.compile(r"(?<![\d.$])\d{3}[\s-]?\d{3}[\s-]?\d{3}(?!\d|\.\d)")
_BSB = re.compile(r"(?<![\d.$])\d{3}-\d{3}(?!\d|\.\d)")
_ACCOUNT = re.compile(r"\b(acc(?:oun)?t(?:\s*(?:no\.?|number|#))?:?\s*)(\d[\d\s-]{4,12}\d)", _I)


def redact_pii(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    def sub(pattern: re.Pattern, label: str, s: str, check=None, group: int = 0) -> str:
        def repl(m: re.Match) -> str:
            value = m.group(group)
            if check and not check(value):
                return m.group(0)
            counts[label] = counts.get(label, 0) + 1
            return m.group(0).replace(value, f"[{label}]")

        return pattern.sub(repl, s)

    text = sub(_EMAIL, "EMAIL", text)
    text = sub(_ACCOUNT, "ACCOUNT", text, group=2)
    text = sub(_PHONE, "PHONE", text)
    text = sub(_NINE_DIGITS, "TFN", text, check=lambda v: is_tfn(re.sub(r"\D", "", v)))
    text = sub(_BSB, "BSB", text)
    return text, counts


# --- intent patterns ----------------------------------------------------------------------

_INJECTION = [
    (
        "ignore_instructions",
        # "your/previous/system instructions", not "ignore the rules about breaks"
        (
            r"\b(ignore|disregard|forget|override|bypass)\b.{0,20}\b(previous|prior|above|earlier|"
            r"preceding|your|system)\b.{0,15}\b(instructions?|rules|prompts?|guidelines|"
            r"directions|context)\b"
        ),
    ),
    (
        "reveal_prompt",
        (
            r"\b(reveal|show|print|repeat|output|display|tell me|give me|what (is|are)|leak|dump)\b"
            r".{0,30}\b(system prompt|(your|hidden|initial|original|secret) (prompt|instructions)"
            r"|canary|confidential reference)\b"
        ),
    ),
    ("system_prompt", r"\bsystem\s*prompt\b"),
    (
        "role_override",
        (
            r"\b(you are now|from now on,? you|act as|pretend (to be|you are)|roleplay as|"
            r"you('re| are) no longer)\b.{0,40}\b(assistant|ai|model|dan|unrestricted|unfiltered|"
            r"jailbr\w*|evil|lawyer|developer|admin)"
        ),
    ),
    ("jailbreak", r"\b(jailbreak\w*|developer mode|dan mode|do anything now)\b"),
    (
        "fake_markup",
        (
            r"<\|?/?\s*(system|im_start|im_end|source|sources|question|assistant)\b|"
            r"^\s*#{2,}\s*(system|instructions?)\b|\[/?(inst|system)\]"
        ),
    ),
    ("new_instructions", r"\bnew (instructions?|rules|task)\s*:"),
]

_EVASION = [
    (
        "without_detection",
        (
            r"\bwithout\b.{0,30}\b(getting caught|being caught|anyone (knowing|noticing|finding out)|"
            r"(them|fair work|the ombudsman|staff|workers|employees) (knowing|noticing|finding out))"
        ),
    ),
    (
        "avoid_obligation",
        (
            r"\bhow (can|do|could|would|should) (i|we)\b.{0,20}\b(avoid|get (around|out of)|dodge|"
            r"evade|skip|not have to)\b.{0,15}\b(pay(ing)?|the award|penalt(y|ies)|overtime|super"
            r"(annuation)?|loading|minimum wage)"
        ),
    ),
    (
        "underpay_intent",
        (
            r"\bhow (can|do|could) (i|we)\b.{0,25}\b(underpay|pay (them|staff|workers|employees) less"
            r"|pay less than the award)"
        ),
    ),
    (
        "loophole",
        (
            r"\bloopholes?\b.{0,40}\b(so (i|we)|for (me|us)|(i|we) (can|could))|"
            r"\b(i|we)\b.{0,30}\b(use|find|exploit)\b.{0,10}\bloopholes?"
        ),
    ),
    (
        "falsify_records",
        (
            r"\b(fake|falsify|falsifying|faking|doctor|backdate|fudge)\b.{0,15}\b(time ?sheets?|"
            r"records?|pay ?slips?|rosters?|hours)\b"
        ),
    ),
    (
        # employer intent only: "My boss pays me cash in hand, is that legal?" is a fair question
        "cash_off_books",
        r"\bhow (can|do|could) (i|we)\b.{0,30}\b(cash in hand|off the books|under the table)\b",
    ),
]

_ADVICE = [
    (
        "happened_to_me",
        (
            r"\b(i (was|got|have been|'ve been|am being)|i'm being)\s+(unfairly\s+)?(fired|sacked|"
            r"dismissed|terminated|let go|underpaid|bullied|harassed|discriminated|demoted)"
        ),
    ),
    (
        "employer_did",
        (
            r"\b(my|our) (boss|employer|manager|company|supervisor|workplace)\b.{0,20}\b(fired|sacked|"
            r"dismissed|terminated|underpaid|owes me|hasn't paid|has not paid|didn't pay|did not pay|"
            r"won't pay|refuses to pay|is refusing to pay|stole)\b"
        ),
    ),
    (
        "should_i",
        (
            r"\bshould i\b.{0,20}\b(sue|quit|resign|sign|take (them|my employer|my boss)|lodge|"
            r"complain|report|accept|go to)\b"
        ),
    ),
    ("legal_action", r"\b(can|could) i (sue|take (them|my (boss|employer)) to court)\b|\blawyer\b"),
    ("unfair_dismissal", r"\bunfair(ly)? dismiss(al|ed)\b|\bgeneral protections\b"),
    ("my_case", r"\b(my|our) (case|claim|dispute|legal options|rights in this situation)\b"),
    ("amount_owed", r"\bhow much (am i|is my employer|are they)\b.{0,15}\bowed?\b"),
]

# Positive scope check: an in-scope question mentions work in some way. Anything without a
# single workplace word ("write me a poem", "best pizza in Melbourne") is out of scope.
_SCOPE = re.compile(
    r"\b(work\w*|employ\w*|job|jobs|staff|boss|manager|supervisor|business|compan(y|ies)|"
    r"pa(y|id|ys|ying)|payslip|wages?|salar(y|ies)|rates?|award\w*|loading|penalt(y|ies)|"
    r"overtime|shifts?|roster\w*|hours?|breaks?|leave|holidays?|day off|days off|sick|carer|"
    r"casual|part[- ]?time\w*|full[- ]?time\w*|permanent|contract\w*|apprentice\w*|trainee\w*|"
    r"junior|classification|level \d|allowances?|super\w*|notice|resign\w*|redundan\w*|"
    r"dismiss\w*|fired|sacked|terminat\w*|roster|uniform|tools?|meal|rest|public holiday|"
    r"weekend|saturday|sunday|night|annual|parental|maternity|union|ombudsman|fair work|nes|"
    r"hospitality|retail|fast food|clerks?|clerical|office|restaurant|hotel|cafe|bar|pub|shop|"
    r"store|kitchen|cook|chef|waiter|barista|cashier|receptionist|minimum wage|entitle\w*|"
    r"conditions|disconnect|after hours|training|unpaid|underpa\w*|owed?|allowed|legal|"
    # WorkSafe Victoria guidance (step 15)
    r"safe\w*|unsafe|ohs|hsrs?|health and safety|hazard\w*|injur\w*|accident\w*|incident\w*|"
    r"worksafe|workcover|compensation|bully\w*|harass\w*|aggression|violence|heat|stress)\b",
    _I,
)

_COMPILED = [
    (kind, name, re.compile(pattern, _I | re.MULTILINE))
    for kind, rules in (("injection", _INJECTION), ("evasion", _EVASION), ("advice", _ADVICE))
    for name, pattern in rules
]
_ROUTE_FOR = {"injection": "blocked", "evasion": "blocked", "advice": "refer"}


def find_injection(text: str) -> str | None:
    """Name of the first injection pattern found in text (also used on retrieved sources)."""
    for kind, name, pattern in _COMPILED:
        if kind == "injection" and pattern.search(text):
            return name
    return None


def _default_classifier() -> Callable[[str], float] | None:
    from app.config import get_settings

    if not get_settings().injection_classifier:
        return None
    from app.guardrails.classifier import injection_score

    return injection_score


def check_input(question: str, *, classifier: Callable[[str], float] | None = None) -> InputResult:
    """`classifier` returns an injection probability; default: the ONNX classifier when
    settings.injection_classifier is on (tests pass a fake)."""
    if len(question) > MAX_CHARS:
        return InputResult("blocked", "", rule="length")
    text = normalise(question)
    if not text:
        return InputResult("blocked", "", rule="empty")
    text, redactions = redact_pii(text)
    for kind, name, pattern in _COMPILED:
        if pattern.search(text):
            return InputResult(_ROUTE_FOR[kind], text, f"{kind}:{name}", redactions)
    scorer = classifier or _default_classifier()
    score = None
    if scorer is not None:
        from app.config import get_settings

        score = round(scorer(text), 4)
        if score >= get_settings().injection_threshold:
            return InputResult("blocked", text, "injection:classifier", redactions, score)
    if not _SCOPE.search(text):
        return InputResult("out_of_scope", text, "scope:no_workplace_terms", redactions, score)
    return InputResult("pass", text, None, redactions, score)
