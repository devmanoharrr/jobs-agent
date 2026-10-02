import pytest
import respx

from app.config import settings
from app.discovery.ats_detector import detect
from app.discovery.fetch import DiscoveryError, fetch_career_page
from scripts.discover_source import main

GREENHOUSE_URL = "https://boards.greenhouse.io/acme"
LEVER_HTML = '<html><a href="https://jobs.lever.co/cred/1111-2222">Role</a></html>'
ASHBY_HTML = '<html><iframe src="https://jobs.ashbyhq.com/temporal"></iframe></html>'
WORKABLE_HTML = '<html><a href="https://apply.workable.com/epignosis/j/ABC">Role</a></html>'
JSONLD_HTML = """
<html><script type="application/ld+json">
{"@graph": [{"@type": ["JobPosting"], "title": "Engineer"}]}
</script></html>
"""
PLAIN_HTML = "<html><body><h1>Careers</h1><p>Email us.</p></body></html>"


def test_url_detects_greenhouse_without_enabling_a_source() -> None:
    result = detect(GREENHOUSE_URL, PLAIN_HTML)
    assert result.status == "detected"
    assert result.source_type == "greenhouse"
    assert result.external_key == "acme"


def test_html_links_detect_lever_ashby_and_workable() -> None:
    lever = detect("https://company.example/careers", LEVER_HTML)
    ashby = detect("https://company.example/careers", ASHBY_HTML)
    workable = detect("https://company.example/careers", WORKABLE_HTML)
    assert (lever.source_type, lever.external_key) == ("lever", "cred")
    assert (ashby.source_type, ashby.external_key) == ("ashby", "temporal")
    assert (workable.source_type, workable.external_key) == ("workable", "epignosis")
    assert lever.status == ashby.status == workable.status == "detected"


def test_boards_api_token_comes_from_the_boards_segment() -> None:
    result = detect("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "")
    assert result.source_type == "greenhouse"
    assert result.external_key == "acme"


def test_eu_greenhouse_board_token_is_the_first_path_segment() -> None:
    result = detect("https://job-boards.eu.greenhouse.io/groww", "")
    assert result.source_type == "greenhouse"
    assert result.external_key == "groww"


def test_job_posting_jsonld_is_a_candidate_when_no_ats_matches() -> None:
    result = detect("https://company.example/careers", JSONLD_HTML)
    assert result.status == "detected"
    assert result.source_type == "generic-jsonld"
    assert result.external_key is None


def test_plain_page_needs_review() -> None:
    result = detect("https://company.example/careers", PLAIN_HTML)
    assert result.status == "needs-review"
    assert result.source_type is None


def test_two_ats_mentions_are_not_resolved_automatically() -> None:
    html = LEVER_HTML + '<a href="https://boards.greenhouse.io/otherco">Greenhouse</a>'
    result = detect("https://company.example/careers", html)
    assert result.status == "needs-review"
    assert result.source_type is None
    assert "greenhouse" in result.evidence
    assert "lever" in result.evidence


def test_two_board_tokens_for_one_ats_need_review() -> None:
    html = (
        '<a href="https://jobs.lever.co/alpha">A</a>'
        '<a href="https://jobs.lever.co/beta">B</a>'
    )
    result = detect("https://company.example/careers", html)
    assert result.status == "needs-review"
    assert result.external_key is None


def test_ats_match_wins_over_jsonld() -> None:
    result = detect(GREENHOUSE_URL, JSONLD_HTML)
    assert result.source_type == "greenhouse"


@pytest.mark.asyncio
async def test_fetch_reads_only_the_given_url() -> None:
    with respx.mock:
        route = respx.get("https://company.example/careers").respond(200, text=PLAIN_HTML)
        html = await fetch_career_page("https://company.example/careers")
    assert route.call_count == 1
    assert route.calls[0].request.headers["user-agent"] == settings.user_agent
    assert html == PLAIN_HTML


@pytest.mark.asyncio
async def test_forbidden_career_page_is_not_evaded() -> None:
    with respx.mock:
        respx.get("https://company.example/careers").respond(403)
        with pytest.raises(DiscoveryError, match="403"):
            await fetch_career_page("https://company.example/careers")


def test_command_prints_detection_and_does_not_enable(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    async def fake_fetch(url: str) -> str:
        assert url == "https://company.example/careers"
        return LEVER_HTML

    monkeypatch.setattr("scripts.discover_source.fetch_career_page", fake_fetch)
    assert main(["https://company.example/careers"]) == 0
    output = capsys.readouterr().out
    assert "status: detected" in output
    assert "source_type: lever" in output
    assert "external_key: cred" in output
    assert "enabled: false" in output
