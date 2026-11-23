"""Extract adult pay rates from a saved Modern Award page into rows for the `pay_rates` table.

    python -m app.ingestion.parse_rates --all                      # parse + validate
    python -m app.ingestion.parse_rates --award MA000009 --show "Level 2"

Every amount is read from a table cell, never computed. Two sources per award:
- the adult minimum-rates table (clause in MIN_RATES_CLAUSE): the minimum weekly rate of a
  full-time employee; its hourly column is used only to cross-check the schedule;
- the "Summary of Hourly Rates of Pay" schedule, adult tables only: the ordinary-hours column
  is the hourly rate; the other columns are penalty or overtime rates, with the column header
  and percentage as the condition ("Sunday (150%)"). Words in the table title that narrow who
  it covers ("casino gaming", "shiftworkers") go first in the condition.

Tables are read into a grid with rowspan/colspan expanded, so multi-row headers such as
"Monday to Friday > First 2 hours" keep their column.

TODO (later steps): junior, apprentice and managerial rates; allowances (Schedule C).
"""

import argparse
import re
import sys
from dataclasses import dataclass
from decimal import Decimal

import psycopg
from bs4 import BeautifulSoup, Tag

from app.ingestion.parse_award import _PATTERNS, _classify, _norm, _p_text, latest_raw

# Clause holding the adult minimum-rates table (weekly + hourly) in each award.
MIN_RATES_CLAUSE = {"MA000009": "18.1", "MA000004": "17.1", "MA000003": "15.1", "MA000002": "16.1"}

HOURS_PER_WEEK = Decimal(38)  # the hourly rate is 1/38th of the weekly rate

_AMOUNT_CELL = re.compile(r"\d+(?:,\d{3})*\.\d{2}")
_PERCENT = re.compile(r"\d+(?:\.\d+)?%")
_FOOTNOTE = re.compile(r"(?<=[A-Za-z])\d+$")  # "Ordinary hours1" -> "Ordinary hours"
# Exact column labels that hold the ordinary hourly rate ("Monday to Friday after 6.00 pm" is a
# penalty column; shiftworker tables have no ordinary column).
_ORDINARY_COLUMNS = {"ordinary hours", "monday to friday"}
_TITLE_WORDS = re.compile(
    r"\b(?:adult|all|casual|full-time and part-time|full-time|part-time|employees?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PayRate:
    classification: str
    employment_type: str  # full_time | part_time | casual
    rate_type: str  # weekly | hourly | penalty | overtime
    condition: str | None
    amount: Decimal
    unit: str  # "per week" | "per hour"
    source_clause: str


@dataclass
class RatesResult:
    rates: list[PayRate]
    min_hourly: dict[str, Decimal]  # classification -> hourly rate from the minimum-rates table
    tables_read: list[str]  # source clauses that produced rows


def _cell_text(td: Tag) -> str:
    paras = td.find_all("p")
    return " ".join(_p_text(p) for p in paras) if paras else _norm(td.get_text())


def _grid(table: Tag) -> list[list[str]]:
    """Table rows as lists of cell texts, with rowspan/colspan cells repeated in each slot."""
    rows: list[list[str]] = []
    carry: dict[int, tuple[int, str]] = {}  # column -> (rows still covered, text)
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        row: dict[int, str] = {}
        next_carry: dict[int, tuple[int, str]] = {}
        for col, (left, text) in carry.items():
            row[col] = text
            if left > 1:
                next_carry[col] = (left - 1, text)
        col = 0
        for td in tr.find_all(["td", "th"], recursive=False):
            while col in row:
                col += 1
            text = _cell_text(td)
            colspan, rowspan = int(td.get("colspan", 1)), int(td.get("rowspan", 1))
            for c in range(col, col + colspan):
                row[c] = text
                if rowspan > 1:
                    next_carry[c] = (rowspan - 1, text)
            col += colspan
        carry = next_carry
        rows.append([row.get(i, "") for i in range(max(row) + 1)] if row else [])
    return rows


def _is_amount(text: str) -> bool:
    return bool(_AMOUNT_CELL.fullmatch(text))


def _amount(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _split_header(grid: list[list[str]]) -> tuple[list[list[str]], list[list[str]]]:
    """Header rows come before the first data row (a label, then at least one amount)."""
    for i, row in enumerate(grid):
        if row and row[0] and any(_is_amount(c) for c in row[1:]):
            return grid[:i], grid[i:]
    return grid, []


def _labels(header: list[list[str]], ncols: int) -> list[tuple[str, str]]:
    """(label, percentage) per column, e.g. ("Monday to Friday > First 2 hours", "150%")."""
    labels = []
    for i in range(ncols):
        parts, pct = [], ""
        for row in header:
            text = _FOOTNOTE.sub("", row[i] if i < len(row) else "").strip()
            if not text or text == "$" or text.startswith("% of"):
                continue
            if _PERCENT.fullmatch(text):
                pct = text
            elif text not in parts:
                parts.append(text)
        labels.append((" > ".join(parts), pct))
    return labels


def _tables(html: bytes | str) -> list[tuple[str, str, str, Tag]]:
    """(clause ref, clause title, schedule/clause-1 title, table) for each table in the body."""
    soup = BeautifulSoup(html, "html.parser")
    first = soup.find("p", class_="Level1")
    if first is None:
        raise ValueError("no Level1 paragraph: not an award page?")
    out = []
    ref, title, top, started = None, "", "", False
    for el in first.parent.find_all(["p", "table"]):
        if el.find_parent("table") is not None:
            continue
        if el.name == "table":
            if ref:
                out.append((ref, title, top, el))
            continue
        kind = _classify(el)
        if not started:
            if kind != "level1":
                continue
            started = True
        if " ".join(el.get("class") or []).startswith("Footer"):
            break
        if kind not in _PATTERNS:
            continue
        text = _p_text(el)
        m = _PATTERNS[kind].match(text)
        if not m:
            continue
        if kind in ("level1", "schedule"):
            top = text
        ref, title = m["num"], text[m.start("num") :]
    return out


def _min_rates(table: Tag, ref: str) -> tuple[list[PayRate], dict[str, Decimal]]:
    header, data = _split_header(_grid(table))
    labels = _labels(header, max(len(r) for r in header + data))
    weekly = next(i for i, (lab, _) in enumerate(labels) if "weekly" in lab.lower())
    hourly = next(i for i, (lab, _) in enumerate(labels) if "hourly" in lab.lower())
    rates, min_hourly = [], {}
    for row in data:
        if _is_amount(row[weekly]):
            rates.append(
                PayRate(row[0], "full_time", "weekly", None, _amount(row[weekly]), "per week", ref)
            )
        if _is_amount(row[hourly]):
            min_hourly[row[0]] = _amount(row[hourly])
    return rates, min_hourly


def _schedule_table(table: Tag, ref: str, title: str) -> list[PayRate]:
    """Rows from one adult table of the hourly-rates summary schedule."""
    desc, _, what = title.removeprefix(ref).strip().partition("—")
    desc = desc.replace("part- time", "part-time")
    lowered = desc.lower()
    types = ["casual"] if "casual" in lowered else ["full_time", "part_time"]
    overtime = "overtime" in what.lower()
    qualifier = " ".join(_TITLE_WORDS.sub(" ", desc).split())

    header, data = _split_header(_grid(table))
    labels = _labels(header, max(len(r) for r in header + data))
    rates = []
    for row in data:
        for i, cell in enumerate(row[1:], 1):
            if not _is_amount(cell):
                continue
            label, pct = labels[i]
            if not overtime and label.lower() in _ORDINARY_COLUMNS:
                # Clerks labels its ordinary column "Monday to Friday": keep that label.
                rate_type, column = "hourly", "" if label.lower() == "ordinary hours" else label
            else:
                rate_type, column = ("overtime" if overtime else "penalty"), label
                column += f" ({pct})" if pct else ""
            condition = "; ".join(p for p in (qualifier, column) if p) or None
            for employment_type in types:
                rates.append(
                    PayRate(
                        row[0],
                        employment_type,
                        rate_type,
                        condition,
                        _amount(cell),
                        "per hour",
                        ref,
                    )
                )
    return rates


def _is_adult_table(title: str) -> bool:
    t = title.lower()
    return "adult" in t and not any(w in t for w in ("junior", "apprentice"))


def parse_rates_html(html: bytes | str, award_id: str) -> RatesResult:
    min_ref = MIN_RATES_CLAUSE[award_id]
    rates: list[PayRate] = []
    min_hourly: dict[str, Decimal] = {}
    tables_read: list[str] = []
    for ref, title, top, table in _tables(html):
        if ref == min_ref and not min_hourly:
            found, min_hourly = _min_rates(table, ref)
        elif "Summary of Hourly Rates" in top and _is_adult_table(title):
            found = _schedule_table(table, ref, title)
        else:
            continue
        rates.extend(found)
        if found:
            tables_read.append(ref)
    return RatesResult(rates, min_hourly, tables_read)


def validate_rates(result: RatesResult) -> list[str]:
    """Cross-checks between the award's own tables; any mismatch means a parsing error."""
    problems = []
    weekly = {r.classification: r.amount for r in result.rates if r.rate_type == "weekly"}
    if not weekly or not result.min_hourly:
        problems.append("minimum-rates table not found or empty")
    for name, week in weekly.items():
        hour = result.min_hourly.get(name)
        if hour is None or abs(week / HOURS_PER_WEEK - hour) > Decimal("0.01"):
            problems.append(f"{name}: weekly {week} / 38 does not match hourly {hour}")
    # The schedule repeats the minimum hourly rate as the full-time ordinary-hours column
    # (possibly qualified, e.g. "other than shiftworkers"): a shifted column breaks this.
    ft_hourly: dict[str, set[Decimal]] = {}
    for r in result.rates:
        if (r.rate_type, r.employment_type) == ("hourly", "full_time"):
            ft_hourly.setdefault(r.classification, set()).add(r.amount)
    for name, hour in result.min_hourly.items():
        if hour not in ft_hourly.get(name, set()):
            problems.append(
                f"{name}: minimum hourly {hour} not in schedule full-time hourly "
                f"{sorted(ft_hourly.get(name, set()))}"
            )
    keys = [(r.classification, r.employment_type, r.rate_type, r.condition) for r in result.rates]
    dupes = sorted({k for k in keys if keys.count(k) > 1}, key=str)
    if dupes:
        problems.append(f"duplicate rows: {dupes[:5]}")
    if any(r.amount <= 0 for r in result.rates):
        problems.append("amount <= 0")
    return problems


def load_rates(
    conn: psycopg.Connection, document_id: int, effective_date: str, rates: list[PayRate]
) -> int:
    """Replace the pay rates of one document version. Doesn't commit."""
    conn.execute("DELETE FROM pay_rates WHERE document_id = %s", (document_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO pay_rates (document_id, classification, employment_type, rate_type,
                                      condition, amount, unit, source_clause, effective_date)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    document_id,
                    r.classification,
                    r.employment_type,
                    r.rate_type,
                    r.condition,
                    r.amount,
                    r.unit,
                    r.source_clause,
                    effective_date,
                )
                for r in rates
            ],
        )
    return len(rates)


def main() -> None:
    from app.ingestion.fetch import STARTING_AWARDS

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--award", action="append")
    group.add_argument("--all", action="store_true")
    ap.add_argument("--show", help="print the rows whose classification contains this text")
    args = ap.parse_args()

    failed = False
    for award_id in STARTING_AWARDS if args.all else args.award:
        path, meta = latest_raw(award_id)
        result = parse_rates_html(path.read_bytes(), award_id)
        problems = validate_rates(result)
        print(
            f"{award_id} {meta['effective_date']}: {len(result.rates)} rates from "
            f"{len(result.tables_read)} tables ({', '.join(result.tables_read)}), "
            f"{len(problems)} problems"
        )
        for p in problems:
            print("  PROBLEM:", p)
        failed |= bool(problems)
        if args.show:
            for r in result.rates:
                if args.show.lower() in r.classification.lower():
                    print(
                        f"  {r.source_clause:6} {r.classification[:40]:40} {r.employment_type:9} "
                        f"{r.rate_type:8} {r.amount:>9} {r.unit:8} {r.condition or ''}"
                    )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
