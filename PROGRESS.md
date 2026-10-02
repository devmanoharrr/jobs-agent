# Progress

Built from the India Job Ingestion Agent demo blueprint (verified 1 October 2026). Steps 1–15 are in place. The automated suite is 104 passed. Seventeen public boards are enabled, and the latest publish listed 276 active India jobs.

The API process does not start the scheduler. A crawl, and `python -m scripts.publish_raw`, write canonical jobs from stored raw payloads. `not_india` postings are rejected and are not listed.

## Done

### 1. Foundation

Python 3.12 virtualenv, Docker Postgres 16, settings, SQLAlchemy, and the Alembic schema from blueprint section 7. `GET /health` checks that Postgres accepts a query.

### 2. Contracts

Pydantic `RawJobEnvelope` and `CanonicalJobCandidate`. SQLAlchemy `Source` and `Job`. Extra fields are rejected.

### 3–6. Connectors

Each connector returns a raw envelope, persists it without overwriting an older payload, and normalizes it. Company display names for Lever, Ashby, and Workable come from the source record and are not written into the raw payload.

| Source | Type | Board | What the live check showed |
| --- | --- | --- | --- |
| Groww | Greenhouse | `groww` | India jobs, company name in the payload |
| CloudSEK | Greenhouse | `cloudsek` | Mostly India jobs |
| Prodigal | Greenhouse | `prodigal` | Mostly India jobs |
| Druva | Greenhouse | `druva` | Mixed board; India jobs published |
| CRED | Lever | `cred` | India jobs |
| Meesho | Lever | `meesho` | India jobs |
| Nium | Lever | `nium` | Mostly India jobs |
| Temporal | Ashby | `temporal` | A few Bengaluru jobs among a larger board |
| Atlan | Ashby | `atlan` | Mostly India jobs |
| Gainsight | Ashby | `gainsight` | A few India jobs among a larger board |
| Level AI | Ashby | `level-ai` | Mostly India jobs |
| Epignosis | Workable | `epignosis` | Public jobs, located in Greece |
| Apna | Workable | `apna` | India jobs |
| Blue Machines AI | Workable | `blue-machines-ai` | Mostly India jobs |
| Leucine | Workable | `leucine` | India jobs |
| Virallens | Workable | `virallens` | India jobs |
| AI Accountant | Workable | `ai-accountant` | India jobs |

A seed stays disabled on 404, malformed JSON, an unexpected Greenhouse or Workable company name, or a Lever or Ashby URL that is not on that site.

### 7. India filter

Deterministic location dictionary. Publishable values are `india` and `remote_india`. Global remote with no India signal stays `unknown_remote`. The word India inside a description does not by itself make a job India-relevant.

### 8. Deduplication

Exact fingerprint first (company, title, location, requisition), then same source posting, normalized URL, and company plus requisition id. Fuzzy match runs only inside one company: title ratio at least 92 and description token-set ratio at least 88. The same title at two companies does not merge. An employer ATS record outranks an aggregator copy.

### 9. Freshness

`job_sources` has `missing_count` and `active`. One successful miss leaves the job active. A second successful miss in a row deactivates that source association. The job becomes `expired` only when no association is still active. A failed, partial, or incomplete crawl does not change miss counts. Raw rows and source associations are kept.

### 10. Scheduler

APScheduler gives each enabled source its own interval job. The job id is the source id, not the company name. Greenhouse, Lever, Ashby, and Workable use the source interval, which defaults to 6 hours. Adzuna and generic sources use at least 12 hours. The thread pool size is `CRAWL_CONCURRENCY` (5 in `.env`).

A crawl writes a `crawl_runs` row, stores new raw payloads, and on success updates freshness. A failed crawl records the error, moves `next_crawl_at` forward, and does not expire jobs. HTTP 403 or 404 disables that source.

### 11. Search API

`GET /jobs` filters with `query`, `city`, `state`, and `work_mode`, and pages in a stable order: newest `posted_at`, then id. Only active `india` and `remote_india` jobs are listed. `GET /jobs/{id}` returns one published job. `GET /stats` counts jobs, sources, and crawl totals. `GET /sources` and `GET /sources/{id}/runs` are read-only. Search uses `ILIKE` with `pg_trgm` indexes. `POST /sources/{id}/crawl` is not wired.

### 12. AI fallback

OpenRouter is an optional client (`https://openrouter.ai/api/v1`, model `openrouter/free`). An empty `OPENROUTER_API_KEY` skips enrichment and does not call the provider. Identical job text reuses `ai_cache` and does not spend another request. The daily cap defaults to 40 (`ai_budget`). The enrichment response is a closed Pydantic contract: extra fields such as salary are rejected, and the job is left unchanged. Valid facts may fill title, skills, experience, and work mode. India relevance changes only when the deterministic value is still `unknown` or `unknown_remote`. Known ATS payloads are not sent to the model for parsing. Enrichment runs only after a job is published, and only when India relevance is still `unknown` or `unknown_remote`. A model call stops after 30 seconds; a timeout or invalid response leaves that job unchanged.

### 13. Controlled discovery

`python -m scripts.discover_source https://company.example/careers` fetches that URL only. Greenhouse, Lever, Ashby, and Workable are detected from the URL or from links in that page, including the board token when it is unambiguous. A schema.org JobPosting JSON-LD block is reported as `generic-jsonld` when no ATS matches. Anything else, or two conflicting ATS matches, is `needs-review`. The command prints `enabled: false` and does not insert a source. HTTP 403 is reported and not evaded.

### 14. JSON-LD

`generic-jsonld` reads JobPosting blocks from a verified source URL, or from the locations in a sitemap that source points at. `robots.txt` is fetched first and kept for 24 hours. A 404 robots file allows the origin. A 401 or 403 robots file disallows it. If the file cannot be reached and there is no cached copy, the page is not fetched. A disallow does not request the page, so the crawl fails instead of looking empty. Mapped fields are title, description, dates, employment type, hiring organization, job location, applicant location, and the posting URL. `directApply` does not invent an apply link. Salary stays in the raw payload and is not a canonical field. The connector does not call the model.

### 15. Demo dashboard

`GET /` lists published postings 20 at a time, with search, a company filter, and page links. Each title opens `GET /postings/{id}` with the stored description and the other canonical fields. Titles are escaped. Unpublished jobs stay off the page. Fetch sources rechecks a catalog of public boards and lists the ones that pass and are not saved yet. Choosing some of those seeds them and starts a scrape. A failed check is saved disabled.

## Still open

The scheduler does not start with the API. The demo page can scrape enabled boards in the background, or you can run `python -m scripts.publish_raw` for payloads already stored. Enrichment runs only for jobs whose India relevance is still unknown, and only when `OPENROUTER_API_KEY` is set.

## Still true for later steps

- Do not parse a known ATS with an LLM.
- Do not publish a field invented by AI.
- Do not delete raw history when a job expires.
- Do not expire jobs after a failed or incomplete crawl.
- Do not bypass auth, CAPTCHA, or robots rules.
- Connectors return raw envelopes and do not write canonical jobs themselves.
