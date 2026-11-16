"""Download source pages from allow-listed domains only and save them with a sha256.

    python -m app.ingestion.fetch --award MA000009
    python -m app.ingestion.fetch --all

Saves data/raw/<award>_<effective date>.html plus a .json sidecar (url, title,
effective date, sha256, fetched_at) that ingestion uses for the `documents` row.
"""

import argparse
import hashlib
import json
import re
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.config import get_settings

# Context guardrail: only these hosts are ever fetched (exact match, https only).
# NES and WorkSafe hosts get added in build step 15.
ALLOWED_HOSTS = frozenset({"awards.fairwork.gov.au"})

STARTING_AWARDS = ("MA000009", "MA000004", "MA000003", "MA000002")
AWARD_ID = re.compile(r"MA\d{6}")
MAX_BYTES = 20_000_000
TIMEOUT_S = 60
USER_AGENT = "workplace-rights-assistant/0.1"

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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--award", action="append", help="award id, e.g. MA000009 (repeatable)")
    group.add_argument("--all", action="store_true", help="the four starting awards")
    args = ap.parse_args()

    for award_id in STARTING_AWARDS if args.all else args.award:
        doc = fetch_award(award_id)
        print(f"{doc.source_id}  {doc.effective_date}  {doc.sha256[:12]}  {doc.title}")


if __name__ == "__main__":
    main()
