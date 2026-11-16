"""Parse a Modern Award page (awards.fairwork.gov.au, a Word export) into one chunk per clause.

    python -m app.ingestion.parse_award --all              # parse + validate the saved awards
    python -m app.ingestion.parse_award --award MA000009 --show 28.2

Layout rules (see CLAUDE.md):
- text is read per <p> (spans joined with "": Word splits words) or per table row (cells
  joined with " "), never per text node;
- paragraph classes carry the structure: Level1 "28. Overtime", Level2* "28.1",
  Subdocument "Schedule A—…", SubLevel1* "A.1", SubLevel2* "A.1.1"; everything else inside a
  clause (Level3+, Block*, Bullet*, SubLevel3+, unclassed notes, footnotes) is body text;
- TOC*, History, hisk, Partheading, Header and Footer are skipped, and so are bracketed
  amendment notes like "[… inserted by PR774714 …]";
- inserted clauses carry a stale Word number first ("13.9 13A.1 …"): keep the second;
- reading starts at the first Level1 (clause 1) and stops at the first Footer after it;
- clauses longer than MAX_CHARS are split as "28.2#1", "28.2#2", … at line boundaries.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from app.config import get_settings

MAX_CHARS = 1800

_SKIP_PREFIXES = ("TOC", "History", "hisk", "Partheading", "Header", "Footer")

# Optional stale number first ("13.9 13A.1 …"); the named group is the real one.
_LEVEL1 = re.compile(r"(?P<num>\d+[A-Z]?)\.\s+\S")
_LEVEL2 = re.compile(r"(?:\d+[A-Z]?\.\d+[A-Z]?\s+)?(?P<num>\d+[A-Z]?\.\d+[A-Z]?)\s+\S")
_SUBLEVEL = re.compile(r"(?P<num>[A-Z]{1,2}\.\d+[A-Z]?(?:\.\d+[A-Z]?)*)\s+\S")
_SCHEDULE = re.compile(r"Schedule\s+(?P<num>[A-Z]{1,2})\s*[—–-]\s*\S")
_AMENDMENT_NOTE = re.compile(r"\[[^\]]*\bPR\d{6}\b[^\]]*\]")
_RATE = re.compile(r"\$\s?\d|\d%")
_AMOUNT = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")  # schedule tables omit the "$"


def _is_rate_row(row: str) -> bool:
    return bool(_RATE.search(row)) or len(_AMOUNT.findall(row)) >= 2


@dataclass
class Chunk:
    clause_ref: str
    heading: str
    content: str
    is_rate_table: bool = False


@dataclass
class ParseResult:
    chunks: list[Chunk]
    clause_starts: list[str]  # every clause number seen, in order
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Clause:
    ref: str
    heading: str
    lines: list[str]
    is_rate_table: bool = False


def _norm(text: str) -> str:
    return " ".join(text.replace("", "•").split())


def _p_text(p: Tag) -> str:
    return _norm(p.get_text())  # get_text() joins spans with ""


def _row_text(tr: Tag) -> str:
    cells = []
    for cell in tr.find_all(["td", "th"], recursive=False):
        paras = cell.find_all("p")
        text = " ".join(_p_text(p) for p in paras) if paras else _norm(cell.get_text())
        if text:
            cells.append(text)
    return " ".join(cells)


def _classify(p: Tag) -> str:
    """Return 'skip', 'level1', 'level2', 'sub1', 'sub2', 'schedule' or 'body'."""
    cls = " ".join(p.get("class") or [])
    if cls.startswith(_SKIP_PREFIXES):
        return "skip"
    if cls == "Level1":
        return "level1"
    if cls.startswith("Level2"):
        return "level2"
    if cls.startswith("SubLevel1"):
        return "sub1"
    if cls.startswith("SubLevel2"):
        return "sub2"
    if cls == "Subdocument":
        return "schedule"
    return "body"


_PATTERNS = {
    "level1": _LEVEL1,
    "level2": _LEVEL2,
    "sub1": _SUBLEVEL,
    "sub2": _SUBLEVEL,
    "schedule": _SCHEDULE,
}


def _hard_split(line: str, limit: int) -> list[str]:
    """Split one over-long line at sentence/clause ends, falling back to spaces."""
    parts, rest = [], line
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("; "), window.rfind(". "), window.rfind(": "))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit - 1
        parts.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :].strip()
    if rest:
        parts.append(rest)
    return parts


def _split(lines: list[str], limit: int = MAX_CHARS) -> list[str]:
    pieces: list[str] = []
    for line in lines:
        pieces.extend(_hard_split(line, limit) if len(line) > limit else [line])
    parts, current = [], ""
    for piece in pieces:
        candidate = f"{current}\n{piece}" if current else piece
        if len(candidate) > limit and current:
            parts.append(current)
            current = piece
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _iter_blocks(container: Tag):
    """Yield <p> (outside tables) and <tr> elements in document order."""
    for el in container.find_all(["p", "tr"]):
        if el.name == "p" and el.find_parent("table") is not None:
            continue
        yield el


def parse_award_html(html: bytes | str) -> ParseResult:
    soup = BeautifulSoup(html, "html.parser")
    first = soup.find("p", class_="Level1")
    if first is None:
        raise ValueError("no Level1 paragraph: not an award page?")
    container = first.parent

    chunks: list[Chunk] = []
    starts: list[str] = []
    warnings: list[str] = []
    current: _Clause | None = None
    pending: _Clause | None = None  # heading-only clause: kept only if no sub-clauses follow
    top_title = ""  # "28. Overtime" or "Schedule A—…"
    sub_title = ""  # bold SubLevel1 title inside a schedule

    def emit(clause: _Clause) -> None:
        parts = _split(clause.lines)
        for i, part in enumerate(parts, 1):
            ref = f"{clause.ref}#{i}" if len(parts) > 1 else clause.ref
            chunks.append(Chunk(ref, clause.heading, part, clause.is_rate_table))

    def close(next_ref: str | None) -> None:
        nonlocal current, pending
        if pending and not (next_ref and next_ref.startswith(pending.ref + ".")):
            emit(pending)
        pending = None
        if current:
            if len(current.lines) > 1:
                emit(current)
            else:
                pending = current
                if next_ref is None or not next_ref.startswith(current.ref + "."):
                    emit(pending)
                    pending = None
        current = None

    started = False
    for el in _iter_blocks(container):
        if el.name == "tr":
            if current:
                row = _row_text(el)
                if row:
                    current.lines.append(row)
                    current.is_rate_table |= _is_rate_row(row)
            continue

        kind = _classify(el)
        if not started:
            if kind != "level1":
                continue
            started = True
        if " ".join(el.get("class") or []).startswith("Footer"):
            break
        if kind == "skip":
            continue
        text = _p_text(el)
        if not text or _AMENDMENT_NOTE.fullmatch(text):
            continue

        m = _PATTERNS[kind].match(text) if kind in _PATTERNS else None
        if kind in _PATTERNS and not m:
            warnings.append(f"{el.get('class')} without a clause number, kept as body: {text[:60]}")
            kind = "body"

        if kind == "body":
            if current:
                current.lines.append(text)
            continue

        ref = m["num"]
        if kind != "schedule":  # drop a stale Word number ("13.9 13A.1 …" -> "13A.1 …")
            text = text[m.start("num") :]
        bold = "Bold" in " ".join(el.get("class") or [])
        if kind in ("level1", "schedule"):
            top_title, sub_title = text, ""
            heading = text
        elif kind == "level2":
            heading = f"{top_title} > {text}" if bold else top_title
        elif kind == "sub1":
            sub_title = text if bold else ""
            heading = f"{top_title} > {text}" if bold else top_title
        else:  # sub2
            heading = " > ".join(t for t in (top_title, sub_title, text if bold else "") if t)

        close(ref)
        starts.append(ref)
        current = _Clause(ref, heading, [text])

    close(None)
    return ParseResult(chunks, starts, warnings)


def validate(result: ParseResult, limit: int = MAX_CHARS) -> list[str]:
    """Every clause number produces a chunk (or has sub-clause chunks), refs are unique,
    chunks are at most `limit` characters."""
    problems = []
    refs = [c.clause_ref for c in result.chunks]
    dupes = sorted({r for r in refs if refs.count(r) > 1})
    if dupes:
        problems.append(f"duplicate refs: {dupes}")
    base = {r.split("#")[0] for r in refs}
    for start in dict.fromkeys(result.clause_starts):
        if start not in base and not any(b.startswith(start + ".") for b in base):
            problems.append(f"clause {start} produced no chunk")
    for c in result.chunks:
        if len(c.content) > limit:
            problems.append(f"{c.clause_ref} is {len(c.content)} chars (> {limit})")
        if not c.content.strip():
            problems.append(f"{c.clause_ref} is empty")
    return problems


def latest_raw(award_id: str, raw_dir: Path | None = None) -> tuple[Path, dict]:
    raw_dir = raw_dir or get_settings().data_dir / "raw"
    sidecars = sorted(raw_dir.glob(f"{award_id}_*.json"))
    if not sidecars:
        raise FileNotFoundError(f"no saved page for {award_id}; run app.ingestion.fetch first")
    meta = json.loads(sidecars[-1].read_text("utf-8"))
    return raw_dir / meta["html_path"], meta


def main() -> None:
    from app.ingestion.fetch import STARTING_AWARDS

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--award", action="append")
    group.add_argument("--all", action="store_true")
    ap.add_argument("--show", help="print the chunk(s) for this clause ref")
    args = ap.parse_args()

    failed = False
    for award_id in STARTING_AWARDS if args.all else args.award:
        path, meta = latest_raw(award_id)
        result = parse_award_html(path.read_bytes())
        problems = validate(result)
        sizes = [len(c.content) for c in result.chunks]
        print(
            f"{award_id} {meta['effective_date']}: {len(result.chunks)} chunks, "
            f"{len(set(result.clause_starts))} clauses, max {max(sizes)} chars, "
            f"{sum(c.is_rate_table for c in result.chunks)} rate-table chunks, "
            f"{len(result.warnings)} warnings, {len(problems)} problems"
        )
        for w in result.warnings:
            print("  warning:", w)
        for p in problems:
            print("  PROBLEM:", p)
        failed |= bool(problems)
        if args.show:
            for c in result.chunks:
                if c.clause_ref.split("#")[0] == args.show:
                    print(f"\n--- {award_id}:{c.clause_ref}  [{c.heading}]  rate={c.is_rate_table}")
                    print(c.content)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
