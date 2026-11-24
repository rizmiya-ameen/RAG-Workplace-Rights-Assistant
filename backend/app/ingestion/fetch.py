"""Download source pages from allow-listed domains only and save them with a sha256.

    python -m app.ingestion.fetch --award MA000009
    python -m app.ingestion.fetch --all

    python -m app.ingestion.fetch --nes           # Fair Work Act Part 2-2
    python -m app.ingestion.fetch --worksafe      # WorkSafe Victoria pages

Saves data/raw/<source>_<effective date>.html plus a .json sidecar (url, title,
effective date, sha256, fetched_at) that ingestion uses for the `documents` row.
"""

import argparse
import hashlib
import json
import re
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.config import get_settings

# Context guardrail: only these hosts are ever fetched (exact match, https only), and only
# documents whose URL is on one of them reach the LLM (context guard).
ALLOWED_HOSTS = frozenset(
    {
        "awards.fairwork.gov.au",  # Modern Awards
        "www.legislation.gov.au",  # Fair Work Act 2009: the NES (step 15)
        "www.worksafe.vic.gov.au",  # WorkSafe Victoria guidance (step 15)
    }
)

STARTING_AWARDS = ("MA000009", "MA000004", "MA000003", "MA000002")
AWARD_ID = re.compile(r"MA\d{6}")
MAX_BYTES = 20_000_000
TIMEOUT_S = 60
USER_AGENT = "workplace-rights-assistant/0.1"
NES_CRAWL_DELAY_S = 10  # legislation.gov.au robots.txt
POLITE_DELAY_S = 2  # between WorkSafe pages

_DATE = re.compile(r"up to and including\s+(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})")
_TITLE = re.compile(r"^(?P<title>.*?)\s*\[(?P<id>MA\d{6})\]")


class FetchRefused(ValueError):
    """The URL is not on the allow-list, or the response is unacceptable."""


def check_allowed(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchRefused(f"not https: {url}")
    if parts.username or parts.password:
        raise FetchRefused(f"credentials in URL: {url}")
    try:
        port = parts.port
    except ValueError as exc:
        raise FetchRefused(f"bad port: {url}") from exc
    if port not in (None, 443):
        raise FetchRefused(f"non-standard port: {url}")
    if (parts.hostname or "") not in ALLOWED_HOSTS:
        raise FetchRefused(f"host not allow-listed: {parts.hostname!r}")


class _AllowListRedirects(urllib.request.HTTPRedirectHandler):
    """Check every redirect target before following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_AllowListRedirects)


def download(url: str) -> bytes:
    check_allowed(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _opener.open(req, timeout=TIMEOUT_S) as resp:
        check_allowed(resp.geturl())
        body = resp.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise FetchRefused(f"response larger than {MAX_BYTES} bytes: {url}")
    return body


def award_url(award_id: str) -> str:
    if not AWARD_ID.fullmatch(award_id):
        raise ValueError(f"not an award id: {award_id!r}")
    return f"https://awards.fairwork.gov.au/{award_id}.html"


def parse_award_meta(html: bytes, award_id: str) -> tuple[str, date]:
    """Return (title, effective date) from an award page; fail loudly if unexpected."""
    soup = BeautifulSoup(html, "html.parser")

    page_title = soup.title.get_text(strip=True) if soup.title else ""
    m = _TITLE.match(page_title)
    if not m or m["id"] != award_id:
        raise ValueError(f"page title doesn't match {award_id}: {page_title!r}")

    node = soup.find(string=re.compile("up to and including"))
    if node is None:
        raise ValueError(f"no 'up to and including' date on {award_id} page")
    para = node.find_parent("p") or node.parent
    text = " ".join(para.get_text().split())  # spans joined with "", then whitespace normalised
    d = _DATE.search(text)
    if not d:
        raise ValueError(f"can't read the amendments date: {text[:200]!r}")
    return m["title"], datetime.strptime(d[1], "%d %B %Y").date()


@dataclass
class FetchedDoc:
    source_id: str
    doc_type: str
    title: str
    url: str
    effective_date: str
    sha256: str
    fetched_at: str
    html_path: str


def fetch_award(award_id: str, raw_dir: Path | None = None, downloader=download) -> FetchedDoc:
    url = award_url(award_id)
    body = downloader(url)
    title, effective = parse_award_meta(body, award_id)

    raw_dir = raw_dir or get_settings().data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{award_id}_{effective.isoformat()}"
    html_path = raw_dir / f"{stem}.html"
    html_path.write_bytes(body)

    doc = FetchedDoc(
        source_id=award_id,
        doc_type="award",
        title=title,
        url=url,
        effective_date=effective.isoformat(),
        sha256=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        html_path=html_path.name,
    )
    (raw_dir / f"{stem}.json").write_text(json.dumps(asdict(doc), indent=2) + "\n", "utf-8")
    return doc


def file_stem(source_id: str) -> str:
    """Raw-file name for a source id: "WSV:bullying" -> "WSV-bullying" (no ":" on Windows)."""
    return source_id.replace(":", "-")


def _save(doc: FetchedDoc, body: bytes, raw_dir: Path) -> FetchedDoc:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{file_stem(doc.source_id)}_{doc.effective_date}"
    (raw_dir / f"{stem}.html").write_bytes(body)
    doc.html_path = f"{stem}.html"
    (raw_dir / f"{stem}.json").write_text(json.dumps(asdict(doc), indent=2) + "\n", "utf-8")
    return doc


# --- NES: Fair Work Act 2009, Part 2-2 ----------------------------------------------------
# The Act's page is a shell; the text is an EPUB document it loads in a frame. Its URL holds
# the compilation date: /C2009A00028/<date>/<date>/text/original/epub/OEBPS/document_1/...
NES_SOURCE_ID = "NES"
NES_TITLE = "Fair Work Act 2009, Part 2-2: The National Employment Standards"
NES_PAGE_URL = "https://www.legislation.gov.au/C2009A00028/latest/text"
_NES_DOC = re.compile(
    r"https://www\.legislation\.gov\.au/C2009A00028/(\d{4}-\d{2}-\d{2})/\1/text/original/epub/"
    r"OEBPS/document_1/document_1\.html"
)


def nes_document_url(page_html: bytes) -> tuple[str, date]:
    """The current compilation's text URL and date, from the Act's page."""
    m = _NES_DOC.search(page_html.decode("utf-8", "replace"))
    if not m:
        raise ValueError("no Fair Work Act text link on the legislation.gov.au page")
    return m.group(0), date.fromisoformat(m.group(1))


def fetch_nes(raw_dir: Path | None = None, downloader=download, sleep=time.sleep) -> FetchedDoc:
    url, effective = nes_document_url(downloader(NES_PAGE_URL))
    sleep(NES_CRAWL_DELAY_S)  # robots.txt: Crawl-delay: 10
    body = downloader(url)
    if b"The National Employment Standards" not in body:
        raise ValueError(f"no NES Part in {url}")
    doc = FetchedDoc(
        source_id=NES_SOURCE_ID,
        doc_type="nes",
        title=NES_TITLE,
        url=NES_PAGE_URL,  # the readable page; `body` is its text document
        effective_date=effective.isoformat(),
        sha256=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        html_path="",
    )
    return _save(doc, body, raw_dir or get_settings().data_dir / "raw")


# --- WorkSafe Victoria guidance pages ---------------------------------------------------
# A small, worker-facing set of content pages (not topic hubs, which are mostly links to
# other pages); add slugs here. Each page is one document "WSV:<slug>".
WORKSAFE_PAGES = (
    "what-do-when-youve-been-injured-work",
    "notifiable-incidents-under-occupational-health-and-safety-act-2004",
    "hospitality-safety-basics",
    "retail-safety-basics",
    "injury-hotspots-fast-food",
    "young-workers-information-employees",
    "report-psychosocial-hazard",  # bullying and other psychosocial hazards
    "work-related-sexual-harassment-know-your-rights",
    "what-aggression-or-violence",
    "prevention-and-protection-heat-illness",
    "resolving-occupational-health-and-safety-issues",
    "health-and-safety-representatives",
)
_WSV_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_REVIEWED = re.compile(r"Reviewed on (\d{1,2})/(\d{1,2})/(\d{4})")


def worksafe_url(slug: str) -> str:
    if not _WSV_SLUG.fullmatch(slug):
        raise ValueError(f"not a WorkSafe page slug: {slug!r}")
    return f"https://www.worksafe.vic.gov.au/{slug}"


def parse_worksafe_meta(html: bytes) -> tuple[str, date]:
    """(title, reviewed date) from a WorkSafe page; fail loudly if either is missing."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1 is None:
        raise ValueError("no <h1> on the WorkSafe page")
    m = _REVIEWED.search(soup.get_text(" "))
    if not m:
        raise ValueError("no 'Reviewed on' date on the WorkSafe page")
    day, month, year = (int(x) for x in m.groups())
    return " ".join(h1.get_text(" ").split()), date(year, month, day)


def fetch_worksafe(slug: str, raw_dir: Path | None = None, downloader=download) -> FetchedDoc:
    url = worksafe_url(slug)
    body = downloader(url)
    title, reviewed = parse_worksafe_meta(body)
    doc = FetchedDoc(
        source_id=f"WSV:{slug}",
        doc_type="worksafe",
        title=f"WorkSafe Victoria: {title}",
        url=url,
        effective_date=reviewed.isoformat(),
        sha256=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        html_path="",
    )
    return _save(doc, body, raw_dir or get_settings().data_dir / "raw")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--award", action="append", help="award id, e.g. MA000009 (repeatable)")
    group.add_argument("--all", action="store_true", help="the four starting awards")
    group.add_argument("--nes", action="store_true", help="Fair Work Act Part 2-2 (NES)")
    group.add_argument("--worksafe", action="store_true", help="the WORKSAFE_PAGES set")
    args = ap.parse_args()

    def show(doc: FetchedDoc) -> None:
        print(f"{doc.source_id}  {doc.effective_date}  {doc.sha256[:12]}  {doc.title}", flush=True)

    if args.nes:
        show(fetch_nes())  # two requests; legislation.gov.au asks for 10 s between them
        return
    if args.worksafe:
        for i, slug in enumerate(WORKSAFE_PAGES):
            if i:
                time.sleep(POLITE_DELAY_S)
            show(fetch_worksafe(slug))
        return
    for award_id in STARTING_AWARDS if args.all else args.award:
        show(fetch_award(award_id))


if __name__ == "__main__":
    main()
