"""WorkSafe parser and metadata on a verbatim excerpt of "What to do when you've been injured at
work" (<main> through the "Reviewed on" line, reviewed 27/11/2024)."""

from datetime import date
from pathlib import Path

import pytest

from app.ingestion.fetch import parse_worksafe_meta
from app.ingestion.parse_worksafe import parse_worksafe_html, section_slug, validate_worksafe

FIXTURE = Path(__file__).parent / "fixtures" / "WSV_injured_excerpt.html"


@pytest.fixture(scope="module")
def result():
    return parse_worksafe_html(FIXTURE.read_bytes())


def test_one_chunk_per_h2_section(result):
    assert result.clause_starts == [
        "overview",
        "first-steps",
        "get-support-early",
        "what-you-can-claim-for",
        "what-you-need-to-make-a-claim",
        "claim-assessment-and-outcome",
    ]
    assert validate_worksafe(result) == []


def test_section_content_and_heading(result):
    first = next(c for c in result.chunks if c.clause_ref == "first-steps")
    assert first.heading == "What to do when you’ve been injured at work > First steps"
    assert first.content.startswith("First steps\n")
    assert "You have 30 days from the injury date to report it." in first.content


def test_link_cards_and_breadcrumbs_are_left_out(result):
    text = "\n".join(c.content for c in result.chunks)
    assert "Mental injury support" not in text  # a card linking to another page
    assert "Home" not in text.split("\n")  # breadcrumb
    assert "Provisional payments: Fact sheet for workers" not in text


def test_list_items_once_each(result):
    text = "\n".join(c.content for c in result.chunks)
    assert text.count("a certificate of capacity from your GP") == 1


def test_section_slug():
    assert section_slug("What you can claim for") == "what-you-can-claim-for"
    assert section_slug("What to do when an issue can't be resolved") == (
        "what-to-do-when-an-issue-can-t-be-resolved"
    )
    assert section_slug("!!!") == "section"


def test_meta_reads_title_and_reviewed_date():
    title, reviewed = parse_worksafe_meta(FIXTURE.read_bytes())
    assert title == "What to do when you’ve been injured at work"
    assert reviewed == date(2024, 11, 27)


def test_meta_and_parser_refuse_other_pages():
    with pytest.raises(ValueError, match="Reviewed on"):
        parse_worksafe_meta(b"<main><h1>Title</h1><p>text</p></main>")
    with pytest.raises(ValueError, match="no <main>"):
        parse_worksafe_html(b"<div><h2>x</h2><p>y</p></div>")
