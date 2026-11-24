"""Pay-rate tool: SQL on pay_rates (the awards' own rate tables, parsed in step 7).

The LLM never reads pay amounts from text: the router sends amount questions here, and the
output guard only lets through dollar amounts this tool returned.

    python -m app.tools.pay_rates MA000009 --level 2 --type casual --day sunday

Scope (adult minimum rates): ordinary hourly rate (plus the weekly rate for full-time), and
the Saturday, Sunday and public-holiday penalty rates. casino=True uses the casino-gaming
tables (Hospitality Schedule B.6) instead of the general ones. Shiftworker variants, overtime
and evening rates are left to the cited-clause answer (rag route).
"""

import argparse
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import psycopg

from app.db.conn import connect

DAYS = ("ordinary", "saturday", "sunday", "public_holiday")
EMPLOYMENT_TYPES = ("full_time", "part_time", "casual")

_DAY_PATTERN = {
    "saturday": re.compile(r"\bsaturday\b", re.IGNORECASE),
    "sunday": re.compile(r"\bsunday\b", re.IGNORECASE),
    "public_holiday": re.compile(r"\bpublic holiday\b", re.IGNORECASE),
}
# Variants outside the scope above (their rows sit next to the general ones).
_SHIFTWORKERS = re.compile(r"^shiftworkers\b", re.IGNORECASE)
_CASINO = re.compile(r"^casino gaming\b", re.IGNORECASE)

_RATES_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (source_id) id, source_id
        FROM documents WHERE source_id = %(award)s
        ORDER BY source_id, effective_date DESC
    )
    SELECT l.source_id, p.classification, p.employment_type, p.rate_type, p.condition,
           p.amount, p.unit, p.source_clause, p.effective_date
    FROM pay_rates p JOIN latest l ON l.id = p.document_id
    WHERE p.employment_type = %(type)s
      AND p.rate_type IN ('weekly', 'hourly', 'penalty')
    ORDER BY p.id"""

_CLASSIFICATIONS_SQL = """
    SELECT p.classification
    FROM pay_rates p
    JOIN (SELECT DISTINCT ON (source_id) id FROM documents WHERE source_id = %s
          ORDER BY source_id, effective_date DESC) l ON l.id = p.document_id
    GROUP BY p.classification ORDER BY min(p.id)"""


@dataclass(frozen=True)
class Rate:
    award: str
    classification: str
    employment_type: str
    rate_type: str  # weekly | hourly | penalty
    condition: str | None
    amount: Decimal
    unit: str
    source_clause: str
    effective_date: date

    @property
    def citation(self) -> str:
        return f"{self.award}:{self.source_clause}"

    @property
    def amount_text(self) -> str:
        return f"${self.amount:,.2f}"


def level_matches(classification: str, level: str) -> bool:
    """ "2" matches "Level 2", "Retail Employee Level 2", "Level 2—Year 1" and
    "Fast food employee level 2", not "Level 20"; "introductory" matches
    "Introductory level"; "3a" matches "Level 3A"."""
    if level == "introductory":
        return classification.lower().startswith("introductory")
    return (
        re.search(rf"\blevel {re.escape(level)}(?![\w])", classification, re.IGNORECASE) is not None
    )


def _day_matches(rate: Rate, day: str, casino: bool = False) -> bool:
    condition = rate.condition or ""
    if _SHIFTWORKERS.match(condition) or bool(_CASINO.match(condition)) != casino:
        return False
    if day == "ordinary":
        return rate.rate_type in ("hourly", "weekly")
    return rate.rate_type == "penalty" and bool(_DAY_PATTERN[day].search(rate.condition or ""))


def classifications(conn: psycopg.Connection, award: str) -> list[str]:
    return [r[0] for r in conn.execute(_CLASSIFICATIONS_SQL, (award,)).fetchall()]


def lookup(
    conn: psycopg.Connection,
    award: str,
    *,
    level: str,
    employment_type: str,
    days: tuple[str, ...] = ("ordinary",),
    casino: bool = False,
) -> list[Rate]:
    """Rates for every classification matching `level` (clerks' "Level 1" has Year 1-3), in
    table order, ordinary rates first."""
    if employment_type not in EMPLOYMENT_TYPES:
        raise ValueError(f"employment_type must be one of {EMPLOYMENT_TYPES}")
    rows = conn.execute(_RATES_SQL, {"award": award, "type": employment_type}).fetchall()
    return select([Rate(*r) for r in rows], level=level, days=days, casino=casino)


def select(
    rates: list[Rate], *, level: str, days: tuple[str, ...] = ("ordinary",), casino: bool = False
) -> list[Rate]:
    """The filtering behind lookup(), on rates of one award and employment type (pure, so
    tests run it on the parsed award pages without a database)."""
    return [
        r
        for day in days
        for r in rates
        if level_matches(r.classification, level) and _day_matches(r, day, casino)
    ]


def short_levels(classifications: list[str]) -> list[str]:
    """ "Retail Employee Level 1" ... -> "Level 1" ...; clerks' "Level 1—Year 1/2/3" -> one
    "Level 1"; "Introductory level" -> "Introductory". For the clarifying question."""
    out: list[str] = []
    for c in classifications:
        m = re.search(r"\blevel\s*(\d{1,2}[a-z]?)\b", c, re.IGNORECASE)
        label = f"Level {m.group(1).upper()}" if m else c
        if c.lower().startswith("introductory"):
            label = "Introductory"
        if label not in out:
            out.append(label)
    return out


def describe(rate: Rate) -> str:
    """One line for the answer and the LLM: "Level 2, casual, Sunday (175%): $47.39 per hour
    (clause B.2.3, from 2026-07-01)"."""
    kind = {"weekly": "weekly", "hourly": "ordinary hours"}.get(rate.rate_type)
    what = re.sub(r"^(?:other than shiftworkers|casino gaming)(?:; )?", "", rate.condition or "")
    if rate.rate_type != "penalty":
        what = f"{kind}, {what}" if what else kind
    if _CASINO.match(rate.condition or ""):
        what = f"casino gaming, {what}"
    return (
        f"{rate.classification}, {rate.employment_type.replace('_', '-')}, {what}: "
        f"{rate.amount_text} {rate.unit} (clause {rate.source_clause}, from "
        f"{rate.effective_date.isoformat()})"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("award")
    ap.add_argument("--level", required=True, help='e.g. 2, 3a, "introductory"')
    ap.add_argument("--type", choices=EMPLOYMENT_TYPES, default="full_time")
    ap.add_argument("--day", choices=DAYS, nargs="+", default=["ordinary"])
    ap.add_argument("--casino", action="store_true", help="casino-gaming tables (MA000009)")
    args = ap.parse_args()
    with connect() as conn:
        rates = lookup(
            conn,
            args.award,
            level=args.level.lower(),
            employment_type=args.type,
            days=tuple(args.day),
            casino=args.casino,
        )
        for r in rates:
            print(describe(r))
        if not rates:
            print("no rates; classifications:", classifications(conn, args.award))


if __name__ == "__main__":
    main()
