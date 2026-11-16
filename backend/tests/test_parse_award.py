"""Award parser tests.

Fixtures are verbatim excerpts of the real pages (whole elements sliced from data/raw; see the
comment at the top of each fixture). Expected strings below are quoted from those pages.
"""

from pathlib import Path

import pytest

from app.ingestion.parse_award import (
    MAX_CHARS,
    Chunk,
    ParseResult,
    _split,
    latest_raw,
    parse_award_html,
    validate,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def hosp():
    return parse_award_html((FIXTURES / "MA000009_excerpt.html").read_bytes())


@pytest.fixture(scope="module")
def clerks():
    return parse_award_html((FIXTURES / "MA000002_excerpt.html").read_bytes())


def by_ref(result: ParseResult) -> dict[str, Chunk]:
    return {c.clause_ref: c for c in result.chunks}


def all_text(result: ParseResult) -> str:
    return "\n".join(c.content for c in result.chunks)


def test_fixtures_pass_validation(hosp, clerks):
    assert validate(hosp) == []
    assert validate(clerks) == []


def test_starts_at_clause_1_and_skips_header_and_part_heading(clerks):
    assert clerks.chunks[0].clause_ref == "1.1"
    assert clerks.chunks[0].content == "1.1 This is the Clerks—Private Sector Award 2020."
    assert "Part 1—Application and Operation of this award" not in all_text(clerks)


def test_stale_word_number_is_dropped(clerks):
    chunks = by_ref(clerks)
    assert ["13A.1", "13A.2", "13A.3", "13A.4"] == [r for r in chunks if r.startswith("13")]
    assert chunks["13A.1"].content.startswith(
        "13A.1 Clause 13A provides for the exercise of an employee’s right to disconnect "
        "under section 333M of the Act."
    )
    assert "13.9" not in all_text(clerks)
    assert chunks["13A.1"].heading == "13A. Employee right to disconnect"


def test_long_clause_is_split_with_hash_refs(clerks):
    parts = [c for c in clerks.chunks if c.clause_ref.startswith("2#")]
    assert [c.clause_ref for c in parts] == ["2#1", "2#2", "2#3"]
    assert parts[0].content.startswith("2. Definitions\nIn this award:")
    assert all(len(c.content) <= MAX_CHARS for c in parts)
    assert all(c.heading == "2. Definitions" for c in parts)


def test_spans_are_joined_without_spaces(hosp):
    # Word splits "maximum" into "ma" + "x…" spans on the real page.
    assert "a maximum of 12 hours per day or per shift" in all_text(hosp)


def test_table_rows_join_cells_and_flag_rates(hosp):
    junior = by_ref(hosp)["18.4"]
    assert junior.heading == "18. Minimum rates > 18.4 Junior rates"
    assert "\nUnder 17 years of age 50%\n" in junior.content
    assert junior.is_rate_table


def test_table_inside_wrapper_div_is_read(hosp):
    assert "Table 4—Casino gaming minimum rates" in all_text(hosp)


def test_schedule_rate_tables_without_dollar_sign_are_flagged(hosp):
    assert by_ref(hosp)["B.2.1"].is_rate_table


def test_rates_footnote_is_content_not_a_clause(hosp):
    chunks = by_ref(hosp)
    assert "1" not in chunks
    assert any(
        "1 Rates in table are calculated based on the minimum hourly rate, see clauses B.1.1 "
        "and B.1.2." in c.content
        for ref, c in chunks.items()
        if ref.startswith("B.2.")
    )


def test_schedule_clauses_use_letter_refs(hosp):
    chunks = by_ref(hosp)
    assert chunks["B"].heading.startswith("Schedule B—")
    assert {"B.1.1", "B.2.1", "C.1.1", "C.4.3"} <= set(chunks)


def test_unnumbered_sublevel_is_kept_as_body_with_warning(hosp):
    assert "The additional hourly rate component of the penalty rates in" in by_ref(hosp)["C.3"].content
    assert any("without a clause number" in w for w in hosp.warnings)


def test_history_and_footer_are_skipped(hosp, clerks):
    for result in (hosp, clerks):
        text = all_text(result)
        assert "[Varied by PR" not in text
        assert "Schedule X—Additional Measures" not in text  # History lines before the Footer
    assert hosp.chunks[-1].clause_ref == "C.4.3"


def test_no_empty_or_duplicate_chunks(hosp, clerks):
    for result in (hosp, clerks):
        refs = [c.clause_ref for c in result.chunks]
        assert len(refs) == len(set(refs))
        assert all(c.content.strip() for c in result.chunks)


def test_split_keeps_all_text_and_respects_limit():
    lines = ["word " * 50, "x" * 30, ("long sentence; " * 40).strip()]
    parts = _split(lines, limit=200)
    assert all(len(p) <= 200 for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(" ".join(lines).split())


def test_validate_reports_problems():
    result = ParseResult(
        chunks=[Chunk("X.1", "h", "placeholder"), Chunk("X.1", "h", "y" * 2000)],
        clause_starts=["X.1", "X.2"],
    )
    problems = " | ".join(validate(result))
    assert "duplicate refs: ['X.1']" in problems
    assert "clause X.2 produced no chunk" in problems
    assert "2000 chars" in problems


@pytest.mark.parametrize("award_id", ["MA000009", "MA000004", "MA000003", "MA000002"])
def test_saved_awards_pass_validation(award_id):
    """Build-step-5 gate: every saved award parses with no validation problems."""
    try:
        path, _ = latest_raw(award_id)
    except FileNotFoundError:
        pytest.skip(f"{award_id} not fetched")
    result = parse_award_html(path.read_bytes())
    assert validate(result) == []
    assert len(result.chunks) > 150
