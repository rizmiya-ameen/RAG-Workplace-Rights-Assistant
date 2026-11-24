"""Parse a WorkSafe Victoria guidance page into one chunk per <h2> section, cited as
"WSV:<page slug>:<section slug>" (e.g. "WSV:what-do-when-youve-been-injured-work:first-steps";
long sections split as "...:first-steps#1").

    python -m app.ingestion.parse_worksafe                       # parse + validate all pages
    python -m app.ingestion.parse_worksafe --show bullying       # print one page's chunks

The page body is <main>. Text before the first <h2> (the intro) is the "overview" section.
Left out: breadcrumbs, the site-wide notice strip, link-card grids (titles of other pages),
video grids, navigation, buttons and bare "Find out more"-style link text. Accordion panels are content: their titles become
lines. Paragraphs, list items and table rows are read once each (a <p> inside an <li> is
part of the item).

The section slug, not "#", separates page and section: "#n" already marks split parts.
"""

import argparse
import re
import sys

from bs4 import BeautifulSoup, Tag

from app.ingestion.parse_award import MAX_CHARS, Chunk, ParseResult, _row_text, _split

_SKIP_CLASS = re.compile(
    r"breadcrumb|notice-strip|card-grid|video-grid|nav-item|navbar|btn|wcl-cta|share|feedback",
    re.IGNORECASE,
)
_BLOCKS = ("h2", "h3", "h4", "p", "li", "tr")
# Link text left over from teaser blocks: not content.
_CALL_TO_ACTION = re.compile(
    r"^(?:find out more|read more|learn more|subscribe now|more information|download)\.?$",
    re.IGNORECASE,
)


def section_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "section"


def _skipped(el: Tag) -> bool:
    for node in (el, *el.parents):
        if not isinstance(node, Tag) or node.name == "main":
            return False
        if _SKIP_CLASS.search(" ".join(node.get("class") or [])) or node.name in ("nav", "form"):
            return True
    return False


def _inside_block(el: Tag) -> bool:
    """True if a parent already yields this text (p, a list item or a table row). Accordion
    items are containers, not text blocks."""
    for parent in el.parents:
        if parent.name in ("p", "tr"):
            return True
        if parent.name == "li" and "accordion" not in (parent.get("class") or []):
            return True
    return False


def _text(el: Tag) -> str:
    raw = _row_text(el) if el.name == "tr" else el.get_text(" ")
    return " ".join(raw.replace("\xa0", " ").split())


def parse_worksafe_html(html: bytes | str, page_title: str = "") -> ParseResult:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main")
    if main is None:
        raise ValueError("no <main> element: not a WorkSafe page?")
    h1 = main.find("h1") or soup.find("h1")
    title = page_title or (" ".join(h1.get_text(" ").split()) if h1 else "")

    sections: list[tuple[str, str, list[str]]] = [("overview", "Overview", [])]
    for el in main.find_all(_BLOCKS):
        if _skipped(el) or _inside_block(el):
            continue
        if el.name == "li" and "accordion" in (el.get("class") or []):
            continue  # container: its title (h4) and panel text come as their own blocks
        text = _text(el)
        if not text or text == title or _CALL_TO_ACTION.match(text):
            continue
        if el.name == "h2":
            sections.append((section_slug(text), text, []))
        else:
            sections[-1][2].append(text)

    chunks: list[Chunk] = []
    starts: list[str] = []
    seen: dict[str, int] = {}
    for slug, heading, lines in sections:
        if not lines:
            continue
        seen[slug] = seen.get(slug, 0) + 1
        ref = slug if seen[slug] == 1 else f"{slug}-{seen[slug]}"
        starts.append(ref)
        full_heading = f"{title} > {heading}" if title else heading
        parts = _split([heading, *lines] if heading != "Overview" else lines)
        for i, part in enumerate(parts, 1):
            chunks.append(Chunk(f"{ref}#{i}" if len(parts) > 1 else ref, full_heading, part))
    if not chunks:
        raise ValueError("no content found on the WorkSafe page")
    return ParseResult(chunks=chunks, clause_starts=starts, warnings=[])


def validate_worksafe(result: ParseResult, limit: int = MAX_CHARS) -> list[str]:
    problems = []
    refs = [c.clause_ref for c in result.chunks]
    if len(refs) != len(set(refs)):
        problems.append("duplicate refs")
    for c in result.chunks:
        if len(c.content) > limit:
            problems.append(f"{c.clause_ref} is {len(c.content)} chars (> {limit})")
    if sum(len(c.content) for c in result.chunks) < 300:
        problems.append("less than 300 characters of content")
    return problems


def main() -> None:
    from app.ingestion.fetch import WORKSAFE_PAGES
    from app.ingestion.parse_award import latest_raw

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", help="page slug, e.g. bullying")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    for slug in WORKSAFE_PAGES:
        path, meta = latest_raw(f"WSV:{slug}")
        result = parse_worksafe_html(path.read_bytes())
        problems = validate_worksafe(result)
        size = sum(len(c.content) for c in result.chunks)
        print(
            f"WSV:{slug} {meta['effective_date']}: {len(result.chunks)} chunks, {size} chars, "
            f"{len(problems)} problems {problems or ''}"
        )
        if args.show == slug:
            for c in result.chunks:
                print(f"\n--- WSV:{slug}:{c.clause_ref}  [{c.heading}]\n{c.content}")


if __name__ == "__main__":
    main()
