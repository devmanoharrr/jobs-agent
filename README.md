# India Job Ingestion Agent

## Step 1 — what is running

- Python 3.12 virtualenv and project dependencies
- PostgreSQL 16 via Docker Compose
- Settings loaded from `.env`
- SQLAlchemy engine and session
- Alembic migration for the section 7 schema (`sources`, `raw_jobs`, `jobs`, `job_sources`, `crawl_runs`, `ai_cache`)
- `GET /health`, which checks that Postgres accepts a query

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
docker compose up -d
alembic upgrade head
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000/` for the demo page, or `http://127.0.0.1:8000/health`. A healthy process returns:

```json
{"status": "ok", "database": "ok"}
```

Run the foundation test after Postgres is up and migrated:

```bash
pytest
```

Do not commit `.env`. The local database password in `.env.example` is only for this Docker demo.
