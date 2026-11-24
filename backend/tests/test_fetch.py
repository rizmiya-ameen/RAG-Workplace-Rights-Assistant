"""Fetch: domain allow-list (context guardrail), award metadata, saving with sha256.

No network: downloads are faked with a verbatim excerpt of the real MA000009 page.
"""

import hashlib
import json
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from app.ingestion.fetch import (
    FetchRefused,
    _AllowListRedirects,
    award_url,
    check_allowed,
    download,
    fetch_award,
    parse_award_meta,
)

FIXTURE = Path(__file__).parent / "fixtures" / "MA000009_meta_excerpt.html"


def test_award_url_is_allowed():
    check_allowed(award_url("MA000009"))


@pytest.mark.parametrize(
    "url",
    [
        "http://awards.fairwork.gov.au/MA000009.html",  # not https
        "https://example.com/MA000009.html",  # other host
        "https://awards.fairwork.gov.au.evil.example/MA000009.html",  # lookalike suffix
        "https://evil.awards.fairwork.gov.au/MA000009.html",  # subdomain
        "https://awards.fairwork.gov.au@evil.example/MA000009.html",  # userinfo trick
        "https://awards.fairwork.gov.au:8443/MA000009.html",  # other port
        "file:///etc/passwd",
        "not a url",
    ],
)
def test_non_allow_listed_urls_are_refused(url):
    with pytest.raises(FetchRefused):
        check_allowed(url)


def test_download_refuses_before_any_network_call():
    with pytest.raises(FetchRefused):
        download("https://example.com/")


def test_redirect_to_other_host_is_refused():
    req = urllib.request.Request(award_url("MA000009"))
    with pytest.raises(FetchRefused):
        _AllowListRedirects().redirect_request(
            req, None, 302, "Found", {}, "https://evil.example/MA000009.html"
        )


@pytest.mark.parametrize("bad", ["MA00009", "ma000009", "MA000009/../x", "../etc", ""])
def test_award_url_rejects_bad_ids(bad):
    with pytest.raises(ValueError):
        award_url(bad)


def test_parse_award_meta_reads_title_and_date():
    title, effective = parse_award_meta(FIXTURE.read_bytes(), "MA000009")
    assert title == "Hospitality Industry (General) Award 2020"
    assert effective == date(2026, 7, 1)


def test_parse_award_meta_rejects_wrong_page():
    with pytest.raises(ValueError, match="doesn't match"):
        parse_award_meta(FIXTURE.read_bytes(), "MA000004")


def test_parse_award_meta_rejects_page_without_date():
    html = b"<html><head><title>Hospitality Industry (General) Award 2020 [MA000009]</title>"
    with pytest.raises(ValueError, match="up to and including"):
        parse_award_meta(html, "MA000009")


def test_fetch_award_saves_html_and_sidecar(tmp_path):
    body = FIXTURE.read_bytes()
    seen = []

    def fake_download(url):
        seen.append(url)
        return body

    doc = fetch_award("MA000009", raw_dir=tmp_path, downloader=fake_download)

    assert seen == ["https://awards.fairwork.gov.au/MA000009.html"]
    html_path = tmp_path / "MA000009_2026-07-01.html"
    assert html_path.read_bytes() == body
    meta = json.loads((tmp_path / "MA000009_2026-07-01.json").read_text("utf-8"))
    assert meta["sha256"] == hashlib.sha256(body).hexdigest() == doc.sha256
    assert meta["effective_date"] == "2026-07-01"
    assert meta["source_id"] == "MA000009"
    assert meta["doc_type"] == "award"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.legislation.gov.au/C2009A00028/latest/text",
        "https://www.worksafe.vic.gov.au/bullying",
    ],
)
def test_nes_and_worksafe_hosts_are_allowed(url):
    check_allowed(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://legislation.gov.au/C2009A00028/latest/text",  # exact host only (no www)
        "https://worksafe.vic.gov.au/bullying",
        "https://www.worksafe.vic.gov.au.evil.example/bullying",
        "http://www.legislation.gov.au/C2009A00028/latest/text",
    ],
)
def test_lookalike_nes_and_worksafe_urls_are_refused(url):
    with pytest.raises(FetchRefused):
        check_allowed(url)


def test_worksafe_slug_is_validated():
    from app.ingestion.fetch import worksafe_url

    assert worksafe_url("bullying") == "https://www.worksafe.vic.gov.au/bullying"
    for bad in ("../admin", "bullying?x=1", "a/b", "Bullying", ""):
        with pytest.raises(ValueError):
            worksafe_url(bad)


def test_fetch_nes_reads_the_compilation_link_and_saves(tmp_path):
    from app.ingestion.fetch import NES_PAGE_URL, fetch_nes

    doc_url = (
        "https://www.legislation.gov.au/C2009A00028/2026-07-07/2026-07-07/text/original/epub/"
        "OEBPS/document_1/document_1.html"
    )
    page = f'<a href="{doc_url}#_Toc1" target="epubFrame">61 The NES</a>'.encode()
    body = (Path(__file__).parent / "fixtures" / "NES_excerpt.html").read_bytes()
    seen, slept = [], []

    def fake_download(url):
        seen.append(url)
        return page if url == NES_PAGE_URL else body

    doc = fetch_nes(raw_dir=tmp_path, downloader=fake_download, sleep=slept.append)
    assert seen == [NES_PAGE_URL, doc_url] and slept == [10]
    assert doc.source_id == "NES" and doc.doc_type == "nes"
    assert doc.effective_date == "2026-07-07" and doc.url == NES_PAGE_URL
    assert (tmp_path / "NES_2026-07-07.html").read_bytes() == body
    assert json.loads((tmp_path / "NES_2026-07-07.json").read_text("utf-8"))["html_path"] == (
        "NES_2026-07-07.html"
    )


def test_fetch_worksafe_saves_under_a_windows_safe_name(tmp_path):
    from app.ingestion.fetch import fetch_worksafe
    from app.ingestion.parse_award import latest_raw

    body = (Path(__file__).parent / "fixtures" / "WSV_injured_excerpt.html").read_bytes()
    slug = "what-do-when-youve-been-injured-work"
    doc = fetch_worksafe(slug, raw_dir=tmp_path, downloader=lambda url: body)
    assert doc.source_id == f"WSV:{slug}" and doc.effective_date == "2024-11-27"
    assert doc.title == "WorkSafe Victoria: What to do when you’ve been injured at work"
    path, meta = latest_raw(f"WSV:{slug}", raw_dir=tmp_path)
    assert path.name == f"WSV-{slug}_2024-11-27.html" and meta["doc_type"] == "worksafe"
