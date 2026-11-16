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
