"""NES parser on a verbatim excerpt of the Fair Work Act text (legislation.gov.au, compilation
2026-07-07): Part 2-2 heading, Divisions 1-3 (s59-s64), s117 with its notice table, and the
Part 2-3 heading that ends the NES."""

from pathlib import Path

import pytest

from app.ingestion.parse_nes import parse_nes_html, validate_nes

FIXTURE = Path(__file__).parent / "fixtures" / "NES_excerpt.html"


@pytest.fixture(scope="module")
def result():
    return parse_nes_html(FIXTURE.read_bytes())


def by_ref(result):
    return {c.clause_ref: c for c in result.chunks}


def test_sections_in_part_2_2_only(result):
    assert result.clause_starts == ["s59", "s60", "s61", "s62", "s63", "s64", "s117"]
    assert result.warnings == []


def test_section_text_and_heading_path(result):
    s62 = by_ref(result)["s62#1"]  # long section: split in two
    assert s62.heading == "Division 3—Maximum weekly hours > 62 Maximum weekly hours"
    assert s62.content.startswith("62 Maximum weekly hours\n")
    assert "(a) for a full-time employee—38 hours; or" in s62.content  # U+2011 -> "-"
    assert "‑" not in s62.content and "\xa0" not in s62.content


def test_tables_are_read_per_row(result):
    s117 = "\n".join(c.content for c in result.chunks if c.clause_ref.startswith("s117"))
    assert "\n1 Not more than 1 year 1 week\n" in s117
    assert "\n4 More than 5 years 4 weeks\n" in s117


def test_long_sections_are_split(result):
    parts = [c.clause_ref for c in result.chunks if c.clause_ref.startswith("s117")]
    assert parts == ["s117#1", "s117#2"]
    assert all(len(c.content) <= 1800 for c in result.chunks)


def test_validation_names_missing_core_sections(result):
    # The excerpt lacks s87, s96, ... so validation reports them; the full text has none.
    problems = validate_nes(result)
    assert "expected section s87 missing" in problems
    assert not any("duplicate" in p or "chars" in p for p in problems)


def test_other_documents_are_refused():
    with pytest.raises(ValueError, match="no Part 2-2"):
        parse_nes_html("<p class='ActHead2'>Part 1-1—Introduction</p><p>text</p>")
