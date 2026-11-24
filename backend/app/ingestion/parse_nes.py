"""Parse the National Employment Standards (Fair Work Act 2009, Part 2-2) into one chunk per
section, cited as "NES:s62" (long sections split as "s62#1", "s62#2").

    python -m app.ingestion.parse_nes                 # parse + validate the saved text
    python -m app.ingestion.parse_nes --show 117      # print one section

The text is the Act's EPUB document from legislation.gov.au (a Word export, like the awards):
- `ActHead2` paragraphs are Parts; Part 2-2 runs until the next `ActHead1`/`ActHead2`;
- `ActHead3` = Division, `ActHead4` = Subdivision, `ActHead5` = section, whose number is in
  a `CharSectno` span ("62");
- body paragraphs (`subsection`, `paragraph`, `SubsectionHead`, `notetext`, `Definition`, …)
  are read per <p>, tables per row (s117's notice-period table), like the award parser.
Non-breaking hyphens (U+2011) become "-".
"""

import argparse
import re
import sys
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from app.ingestion.parse_award import MAX_CHARS, Chunk, ParseResult, _row_text, _split

_PART = re.compile(r"^Part 2-2\b")


def _text(el) -> str:
    raw = el.get_text() if el.name == "p" else _row_text(el)
    return " ".join(raw.replace("‑", "-").replace("\xa0", " ").split())


@dataclass
class _Section:
    number: str
    heading: str
    lines: list[str] = field(default_factory=list)


def parse_nes_html(html: bytes | str) -> ParseResult:
    soup = BeautifulSoup(html, "html.parser")
    chunks: list[Chunk] = []
    starts: list[str] = []
    warnings: list[str] = []
    inside = False
    division = subdivision = ""
    current: _Section | None = None

    def emit() -> None:
        if current is None:
            return
        parts = _split(current.lines)
        for i, part in enumerate(parts, 1):
            ref = f"s{current.number}#{i}" if len(parts) > 1 else f"s{current.number}"
            chunks.append(Chunk(ref, current.heading, part))

    for el in soup.find_all(["p", "tr"]):
        if el.name == "p" and el.find_parent("table") is not None:
            continue  # read with its row
        cls = (el.get("class") or [""])[0] if el.name == "p" else ""
        text = _text(el)
        if cls in ("ActHead1", "ActHead2"):
            if inside:
                break  # end of Part 2-2
            inside = cls == "ActHead2" and bool(_PART.match(text))
            continue
        if not inside or not text:
            continue
        if cls == "ActHead3":
            division, subdivision = text, ""
        elif cls == "ActHead4":
            subdivision = text
        elif cls == "ActHead5":
            emit()
            sectno = el.find("span", class_="CharSectno")
            number = sectno.get_text().strip() if sectno else ""
            if not re.fullmatch(r"\d+[A-Z]*", number):
                warnings.append(f"section heading without a number: {text[:60]}")
                continue
            title = " ".join(p for p in (division, subdivision) if p)
            path = " > ".join(p for p in (division, subdivision, text) if p)
            current = _Section(number, path or title, [text])
            starts.append(f"s{number}")
        elif current is not None:
            current.lines.append(text)
    emit()
    if not starts:
        raise ValueError("no Part 2-2 sections found: not the Fair Work Act text?")
    return ParseResult(chunks=chunks, clause_starts=starts, warnings=warnings)


def validate_nes(result: ParseResult, limit: int = MAX_CHARS) -> list[str]:
    problems = []
    refs = [c.clause_ref for c in result.chunks]
    dupes = sorted({r for r in refs if refs.count(r) > 1})
    if dupes:
        problems.append(f"duplicate refs: {dupes}")
    base = {r.split("#")[0] for r in refs}
    problems += [f"section {s} produced no chunk" for s in result.clause_starts if s not in base]
    for c in result.chunks:
        if len(c.content) > limit:
            problems.append(f"{c.clause_ref} is {len(c.content)} chars (> {limit})")
    for must in ("s62", "s87", "s96", "s114", "s117", "s119"):  # hours, leave, notice, redundancy
        if must not in base:
            problems.append(f"expected section {must} missing")
    return problems


def main() -> None:
    from app.ingestion.parse_award import latest_raw

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", help="section number, e.g. 117")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    path, meta = latest_raw("NES")
    result = parse_nes_html(path.read_bytes())
    problems = validate_nes(result)
    print(
        f"NES {meta['effective_date']}: {len(result.chunks)} chunks, "
        f"{len(result.clause_starts)} sections "
        f"({result.clause_starts[0]}-{result.clause_starts[-1]}), "
        f"max {max(len(c.content) for c in result.chunks)} chars, "
        f"{len(result.warnings)} warnings, {len(problems)} problems"
    )
    for w in result.warnings:
        print("  warning:", w)
    for p in problems:
        print("  PROBLEM:", p)
    if args.show:
        for c in result.chunks:
            if c.clause_ref.split("#")[0] == f"s{args.show}":
                print(f"\n--- NES:{c.clause_ref}  [{c.heading}]\n{c.content}")


if __name__ == "__main__":
    main()
