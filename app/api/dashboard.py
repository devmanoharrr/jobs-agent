"""Demo pages: scrape boards, add a source, and read every published posting."""

import html
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlencode

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.job import Job
from app.models.source import Source
from app.services.demo_actions import add_board, demo_status, start_scrape, start_seed, start_source_fetch
from app.services.jobi_sync import BASE_URLS, JobiSyncError, sync_table_ready, transfer, waiting_count
from app.services.job_service import (
    get_published_job,
    list_jobs,
    list_sources,
    published_companies,
    recent_failures,
    stats,
)

router = APIRouter()
PAGE_SIZE = 20


@router.get("/", response_class=HTMLResponse)
def dashboard(
    q: str | None = None,
    company: str | None = None,
    notice: str | None = None,
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    totals = stats(db)
    sources = list_sources(db)
    companies = published_companies(db)
    query = _blank(q)
    chosen = _blank(company)
    total, jobs = list_jobs(
        db,
        query=query,
        city=None,
        state=None,
        work_mode=None,
        company=chosen,
        page=page,
        page_size=PAGE_SIZE,
    )
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    current = page
    if page > page_count:
        current = page_count
        total, jobs = list_jobs(
            db,
            query=query,
            city=None,
            state=None,
            work_mode=None,
            company=chosen,
            page=current,
            page_size=PAGE_SIZE,
        )
    failures = recent_failures(db)
    running, scrape_message, ready, fetched = demo_status()
    page_html = _page(
        totals,
        sources,
        jobs,
        failures,
        notice=_blank(notice) or "",
        scraping=running,
        scrape_message=scrape_message,
        query=query or "",
        company=chosen or "",
        companies=companies,
        total_jobs=total,
        page=current,
        ready=ready,
        fetched=fetched,
        jobi_waiting=(waiting_count(db, "local"), waiting_count(db, "prod")),
        jobi_ready=sync_table_ready(db),
    )
    headers = {"Refresh": "8"} if running else None
    return HTMLResponse(page_html, headers=headers)


@router.get("/postings/{job_id}", response_class=HTMLResponse)
def posting(job_id: uuid.UUID, db: Session = Depends(get_db)) -> HTMLResponse:
    job = get_published_job(db, job_id)
    if job is None:
        return HTMLResponse(_layout("Posting", "<p class='empty'>This posting is not published.</p>", False), status_code=404)
    return HTMLResponse(_layout(job.title_original, _detail(job), False))


@router.post("/scrape")
def scrape(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    fields = _form(request)
    raw_id = fields.get("source_id", "")
    source_id: uuid.UUID | None = None
    if raw_id:
        try:
            source_id = uuid.UUID(raw_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid source id") from exc
    if source_id is not None:
        source = db.get(Source, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        ids = [source.id]
    else:
        ids = [source.id for source in list_sources(db) if source.enabled]
    message = start_scrape(ids)
    return RedirectResponse(f"/?notice={quote(message)}", status_code=303)


@router.post("/sources/fetch")
def fetch_sources() -> RedirectResponse:
    message = start_source_fetch()
    return RedirectResponse(f"/?notice={quote(message)}", status_code=303)


@router.post("/sources/seed")
def seed_sources(request: Request) -> RedirectResponse:
    message = start_seed(_fields(request).get("pick", []))
    return RedirectResponse(f"/?notice={quote(message)}", status_code=303)


@router.post("/jobi/post")
def post_to_jobi(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    fields = _form(request)
    publish = fields.get("publish") == "true"
    try:
        message = transfer(db, target=fields.get("target", ""), publish=publish)
    except JobiSyncError as exc:
        message = str(exc)
    return RedirectResponse(f"/?notice={quote(message)}", status_code=303)


@router.post("/sources/add")
def create_source(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    fields = _form(request)
    message = add_board(db, fields.get("board", ""), fields.get("source_type", ""), fields.get("company_name", ""))
    return RedirectResponse(f"/?notice={quote(message)}", status_code=303)


def _fields(request: Request) -> dict[str, list[str]]:
    raw = anyio.from_thread.run(request.body)
    parsed = parse_qs(raw.decode(), keep_blank_values=True)
    return {key: values for key, values in parsed.items()}


def _form(request: Request) -> dict[str, str]:
    return {key: values[-1] for key, values in _fields(request).items() if values}


def _page(
    totals: dict,
    sources: list,
    jobs: list,
    failures: list,
    *,
    notice: str = "",
    scraping: bool = False,
    scrape_message: str = "",
    query: str = "",
    company: str = "",
    companies: list[str] | None = None,
    total_jobs: int = 0,
    page: int = 1,
    ready: list | None = None,
    fetched: bool = False,
    jobi_waiting: tuple[int, int] = (0, 0),
    jobi_ready: bool = True,
) -> str:
    if jobs:
        jobs_block = _jobs_table(jobs)
    elif query or company:
        jobs_block = "<p class='empty'>No postings match.</p>"
    else:
        jobs_block = "<p class='empty'>No published jobs yet.</p>"
    failures_block = _failures_table(failures) if failures else "<p class='empty'>No failed crawls.</p>"
    counts = totals["crawl_runs"]
    relevance = totals["jobs"]["by_india_relevance"]
    page_count = max(1, (total_jobs + PAGE_SIZE - 1) // PAGE_SIZE) if total_jobs else 1
    start = (page - 1) * PAGE_SIZE + 1 if jobs else 0
    end = start + len(jobs) - 1 if jobs else 0
    banner = _banner(notice, scraping, scrape_message)
    body = f"""
{banner}
{_jobi_block(jobi_waiting, jobi_ready)}
<section class="counts">
  <div><strong>{_num(totals["jobs"]["active"])}</strong><span>Active jobs</span></div>
  <div><strong>{_num(total_jobs)}</strong><span>Matching jobs</span></div>
  <div><strong>{_num(totals["sources"]["enabled"])}</strong><span>Sources enabled</span></div>
  <div><strong>{_num(totals["sources"]["disabled"])}</strong><span>Sources disabled</span></div>
  <div><strong>{_num(counts["success"])}</strong><span>Successful crawls</span></div>
  <div><strong>{_num(counts["failed"])}</strong><span>Failed crawls</span></div>
</section>
<p class="meta">Fetched {_num(counts["fetched"])}, inserted {_num(counts["inserted"])}, rejected {_num(counts["rejected"])}. India relevance: {_relevance(relevance)}</p>
<section>
  <div class="section-head">
    <h2>Sources</h2>
    <form method="post" action="/sources/fetch">
      <button type="submit">Fetch sources</button>
    </form>
  </div>
  {_ready_block(ready or [], fetched)}
  <div class="wrap">{_sources_table(sources)}</div>
  <h3>Add one board</h3>
  {_add_form()}
</section>
<section>
  <div class="section-head"><h2>Postings</h2><p>{start}–{end} of {total_jobs}</p></div>
  {_filter_form(query, company, companies or [])}
  <div class="wrap">{jobs_block}</div>
  {_pager(page, page_count, query, company)}
</section>
<section>
  <h2>Failures</h2>
  <div class="wrap">{failures_block}</div>
</section>
"""
    return _layout("India Job Ingestion Agent", body, scraping)


def _layout(title: str, body: str, scraping: bool) -> str:
    refresh = '<meta http-equiv="refresh" content="8">' if scraping else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh}
  <title>{_text(title)}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; font: 16px/1.45 "Iowan Old Style", Palatino, Georgia, serif; background: #f3efe6; color: #1c1915; }}
    header {{ background: #1c1915; color: #f6f1e7; padding: 18px 20px; }}
    header a {{ color: #f0d8a8; }}
    header h1 {{ margin: 0; font-size: 1.45rem; letter-spacing: -0.02em; }}
    header p {{ margin: 4px 0 0; color: #d9d0c3; font-family: system-ui, sans-serif; font-size: 0.92rem; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 20px 16px 56px; }}
    h2 {{ font-size: 1.05rem; margin: 0; }}
    h3 {{ margin: 18px 0 0; font-size: 0.95rem; }}
    a {{ color: #0b4f6c; }}
    button, .button {{ font: 14px/1.2 system-ui, sans-serif; background: #1c1915; color: #f6f1e7; border: 0; border-radius: 999px; padding: 8px 12px; cursor: pointer; }}
    button.ghost {{ background: #fff; color: #1c1915; border: 1px solid #d9d0c3; }}
    input, select {{ font: 15px/1.3 system-ui, sans-serif; padding: 8px 10px; border: 1px solid #d9d0c3; border-radius: 8px; background: #fff; color: #1c1915; }}
    form.row, .filters {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 10px 0 14px; }}
    label {{ display: grid; gap: 4px; font-family: system-ui, sans-serif; font-size: 0.78rem; color: #5c564e; }}
    .banner {{ background: #fff8e8; border: 1px solid #e4d3a4; border-radius: 10px; padding: 10px 12px; margin: 0 0 14px; font-family: system-ui, sans-serif; }}
    .counts {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .counts div {{ background: #fff; border: 1px solid #e4ddd2; border-radius: 10px; padding: 10px 12px; min-width: 120px; }}
    .counts strong {{ display: block; font-size: 1.35rem; }}
    .counts span, .meta, .section-head p, .empty {{ color: #5c564e; font-family: system-ui, sans-serif; font-size: 0.9rem; }}
    .section-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin: 22px 0 8px; }}
    .picker {{ display: grid; gap: 8px; background: #fff; border: 1px solid #e4ddd2; border-radius: 12px; padding: 12px 14px; margin: 0 0 14px; }}
    label.pick {{ display: flex; flex-direction: row; align-items: center; gap: 10px; color: #1c1915; font-size: 0.95rem; }}
    .pager {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 12px 0 0; font-family: system-ui, sans-serif; }}
    .pager a, .pager strong {{ min-width: 2rem; text-align: center; text-decoration: none; border: 1px solid #e4ddd2; background: #fff; border-radius: 8px; padding: 6px 8px; }}
    .pager strong {{ background: #1c1915; color: #f6f1e7; border-color: #1c1915; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e4ddd2; vertical-align: top; }}
    th {{ font-family: system-ui, sans-serif; font-size: 0.75rem; color: #5c564e; font-weight: 600; }}
    .wrap {{ overflow-x: auto; border: 1px solid #e4ddd2; border-radius: 10px; }}
    .off {{ color: #8a3b2a; }}
    .card {{ background: #fff; border: 1px solid #e4ddd2; border-radius: 12px; padding: 16px 18px; }}
    .card h2 {{ font-size: 1.45rem; margin: 0 0 8px; }}
    dl {{ display: grid; grid-template-columns: 140px 1fr; gap: 6px 12px; margin: 0 0 16px; }}
    dt {{ color: #5c564e; font-family: system-ui, sans-serif; font-size: 0.82rem; }}
    dd {{ margin: 0; }}
    .description {{ white-space: pre-wrap; font-family: system-ui, sans-serif; font-size: 0.95rem; }}
    @media (max-width: 860px) {{ .grid, dl {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
<header>
  <h1><a href="/" style="color:inherit;text-decoration:none">India Job Ingestion Agent</a></h1>
  <p>Published jobs are active India and remote-India roles. <a href="/docs">API docs</a></p>
</header>
<main>
{body}
</main>
</body>
</html>
"""


def _jobi_block(waiting: tuple[int, int], ready: bool = True) -> str:
    local_waiting, prod_waiting = waiting
    sentence = _waiting_sentence(local_waiting, prod_waiting)
    if not ready:
        sentence = "Run alembic upgrade head before posting to Jobi."
    banner = f"<p class='banner'>{_text(sentence)}</p>" if sentence else ""
    return f"""
<section>
  <div class="section-head"><h2>Post to Jobi</h2></div>
  {banner}
  <form class="picker" method="post" action="/jobi/post">
    <p class="meta">Companies go first. Jobs stay waiting when Jobi skips the company.</p>
    <label class="pick"><input type="radio" name="target" value="local" checked><span>Local — {_text(BASE_URLS["local"])}. {_num(local_waiting)} new waiting.</span></label>
    <label class="pick"><input type="radio" name="target" value="prod"><span>Prod — {_text(BASE_URLS["prod"])}. {_num(prod_waiting)} new waiting.</span></label>
    <label class="pick"><input type="checkbox" name="publish" value="true"><span>Publish new companies</span></label>
    <button type="submit">Start posting</button>
  </form>
</section>
"""


def _waiting_sentence(local_count: int, prod_count: int) -> str:
    parts = []
    if local_count:
        parts.append(f"{_job_phrase(local_count)} waiting to post to Jobi local")
    if prod_count:
        parts.append(f"{_job_phrase(prod_count)} waiting to post to Jobi prod")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0] + "."
    return f"{parts[0]}, and {parts[1]}."


def _job_phrase(count: int) -> str:
    if count == 1:
        return "1 new job is"
    return f"{count} new jobs are"


def _banner(notice: str, scraping: bool, scrape_message: str) -> str:
    parts = []
    if scraping:
        parts.append("Working. This page refreshes automatically.")
    if scrape_message and scrape_message != notice:
        parts.append(scrape_message)
    if notice:
        parts.append(notice)
    if not parts:
        return ""
    return f"<p class='banner'>{_text(' '.join(parts))}</p>"


def _ready_block(ready: list, fetched: bool) -> str:
    if not ready and not fetched:
        return "<p class='meta'>Fetch sources to see boards that passed the live check and are not saved yet.</p>"
    if not ready:
        return "<p class='empty'>No new boards passed the live check.</p>"
    rows = []
    for item in ready:
        rows.append(
            "<label class='pick'>"
            f"<input type='checkbox' name='pick' value='{_text(item.token)}'>"
            f"<span><strong>{_text(item.company_name)}</strong> "
            f"{_text(item.source_type)} · {_num(item.job_count)} jobs · {_num(item.india_count)} India</span>"
            "</label>"
        )
    return (
        "<form class='picker' method='post' action='/sources/seed'>"
        "<p class='meta'>Choose which of these to seed. Seeding starts a scrape.</p>"
        + "".join(rows)
        + "<button type='submit'>Seed selected and scrape</button></form>"
    )


def _pager(page: int, pages: int, query: str, company: str) -> str:
    if pages <= 1:
        return ""
    parts = []
    if page > 1:
        parts.append(f"<a href='{_href(query, company, page - 1)}'>Previous</a>")
    for number in range(1, pages + 1):
        if number == page:
            parts.append(f"<strong>{number}</strong>")
        else:
            parts.append(f"<a href='{_href(query, company, number)}'>{number}</a>")
    if page < pages:
        parts.append(f"<a href='{_href(query, company, page + 1)}'>Next</a>")
    return f"<nav class='pager'>{''.join(parts)}</nav>"


def _href(query: str, company: str, page: int) -> str:
    params: dict[str, str] = {}
    if query:
        params["q"] = query
    if company:
        params["company"] = company
    if page > 1:
        params["page"] = str(page)
    return "/?" + urlencode(params) if params else "/"


def _add_form() -> str:
    return """
<form class="row" method="post" action="/sources/add">
  <label>Board key or URL<input name="board" required placeholder="cloudsek or https://jobs.lever.co/cred"></label>
  <label>Type
    <select name="source_type">
      <option value="">From the URL</option>
      <option value="greenhouse">Greenhouse</option>
      <option value="lever">Lever</option>
      <option value="ashby">Ashby</option>
      <option value="workable">Workable</option>
    </select>
  </label>
  <label>Company name<input name="company_name" required placeholder="Exact board name"></label>
  <button type="submit">Add source</button>
</form>
"""


def _filter_form(query: str, company: str, companies: list[str]) -> str:
    options = ['<option value="">All companies</option>']
    for name in companies:
        selected = " selected" if name.lower() == company.lower() else ""
        options.append(f'<option value="{_text(name)}"{selected}>{_text(name)}</option>')
    return f"""
<form class="filters" method="get" action="/">
  <label>Search<input name="q" value="{_text(query)}" placeholder="Title, company, or description"></label>
  <label>Company<select name="company">{''.join(options)}</select></label>
  <button class="ghost" type="submit">Filter</button>
</form>
"""


def _sources_table(sources: list) -> str:
    rows = []
    for source in sources:
        state = "enabled" if source.enabled else '<span class="off">disabled</span>'
        rows.append(
            "<tr>"
            f"<td>{_text(source.company_name or source.name)}</td>"
            f"<td>{_text(source.source_type)}</td>"
            f"<td>{state}</td>"
            f"<td>{_when(source.last_success_at)}</td>"
            "<td><form method=\"post\" action=\"/scrape\">"
            f"<input type=\"hidden\" name=\"source_id\" value=\"{_text(source.id)}\">"
            "<button class=\"ghost\" type=\"submit\">Scrape</button></form></td>"
            "</tr>"
        )
    body = "".join(rows) or "<tr><td colspan=\"5\">No sources.</td></tr>"
    return (
        "<table><thead><tr><th>Company</th><th>Type</th><th>State</th>"
        "<th>Last success</th><th></th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _jobs_table(jobs: list) -> str:
    rows = []
    for job in jobs:
        title = _text(job.title_original)
        title = f'<a href="/postings/{job.id}">{title}</a>'
        apply = ""
        if job.canonical_apply_url and job.canonical_apply_url.startswith(("http://", "https://")):
            href = html.escape(job.canonical_apply_url, quote=True)
            apply = f'<a href="{href}">Apply</a>'
        place = ", ".join(part for part in (job.city, job.state) if part)
        rows.append(
            "<tr>"
            f"<td>{title}</td>"
            f"<td>{_text(job.company_name)}</td>"
            f"<td>{_text(place)}</td>"
            f"<td>{_text(job.work_mode)}</td>"
            f"<td>{_when(job.posted_at)}</td>"
            f"<td>{apply}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Title</th><th>Company</th><th>Location</th><th>Work mode</th>"
        f"<th>Posted</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _detail(job: Job) -> str:
    place = ", ".join(part for part in (job.city, job.state, job.country_code) if part)
    skills = ", ".join(str(item) for item in (job.skills or []) if str(item).strip())
    experience = _experience(job.experience_min, job.experience_max)
    apply = ""
    if job.canonical_apply_url and job.canonical_apply_url.startswith(("http://", "https://")):
        href = html.escape(job.canonical_apply_url, quote=True)
        apply = f'<a class="button" href="{href}">Apply</a>'
    description = _text(job.description_text) if job.description_text else "No description stored."
    source = ""
    if job.canonical_source is not None:
        source = _text(job.canonical_source.company_name or job.canonical_source.name)
    rows = [
        ("Company", _text(job.company_name)),
        ("Location", _text(place)),
        ("Work mode", _text(job.work_mode)),
        ("Employment", _text(job.employment_type)),
        ("India", _text(job.india_relevance)),
        ("Status", _text(job.status)),
        ("Posted", _when(job.posted_at)),
        ("Expires", _when(job.expires_at)),
        ("Experience", _text(experience)),
        ("Skills", _text(skills)),
        ("Source", source),
        ("Last seen", _when(job.last_seen_at)),
    ]
    facts = "".join(f"<dt>{label}</dt><dd>{value or '—'}</dd>" for label, value in rows)
    back = "/?" + urlencode({"company": job.company_name}) if job.company_name else "/"
    return f"""
<p><a href="{html.escape(back, quote=True)}">Back to postings</a></p>
<article class="card">
  <h2>{_text(job.title_original)}</h2>
  <p>{apply}</p>
  <dl>{facts}</dl>
  <h2>Description</h2>
  <div class="description">{description}</div>
</article>
"""


def _failures_table(failures: list) -> str:
    rows = []
    for run, source in failures:
        rows.append(
            "<tr>"
            f"<td>{_text(source.company_name or source.name)}</td>"
            f"<td>{_when(run.started_at)}</td>"
            f"<td>{_text(run.error_text)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Source</th><th>When</th><th>Error</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _experience(low: object, high: object) -> str:
    if low is None and high is None:
        return ""
    if low is not None and high is not None:
        return f"{low}–{high} years"
    return f"{low or high} years"


def _relevance(counts: dict) -> str:
    if not counts:
        return "none yet"
    parts = [f"{_text(name)} {_num(count)}" for name, count in sorted(counts.items())]
    return ", ".join(parts)


def _text(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _num(value: object) -> str:
    return html.escape(str(value), quote=True)


def _when(value: datetime | None) -> str:
    if value is None:
        return ""
    shown = value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return html.escape(shown, quote=True)


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
