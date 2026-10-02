"""APScheduler jobs for enabled sources, capped by the crawl concurrency setting.

Each enabled source gets its own interval job, first timed from next_crawl_at.
The job id is the source id. The thread pool size is the concurrency limit, so
due sources run independently without a hard-coded company list.
"""

import uuid
from datetime import datetime, timezone

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models.source import Source
from app.scheduler.tasks import Fetch, fetch_source, interval_minutes, run_source_crawl, source_job_id


class DueSourceScheduler:
    def __init__(
        self,
        fetch: Fetch | None = None,
        *,
        concurrency: int | None = None,
        session_factory=SessionLocal,
    ) -> None:
        self._fetch = fetch or fetch_source
        self._sessions = session_factory
        self.concurrency = settings.crawl_concurrency if concurrency is None else concurrency
        self.scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(self.concurrency)},
            job_defaults={"coalesce": True, "max_instances": 1},
            timezone="UTC",
        )

    def load(self, now: datetime | None = None) -> list[str]:
        """Register one job per enabled source, timed from next_crawl_at."""
        now = now or datetime.now(timezone.utc)
        session: Session = self._sessions()
        try:
            sources = session.scalars(select(Source).where(Source.enabled.is_(True))).all()
            planned = [
                (
                    source.id,
                    source.next_crawl_at or now,
                    interval_minutes(source.source_type, source.crawl_interval_minutes),
                )
                for source in sources
            ]
        finally:
            session.close()

        wanted = {source_job_id(source_id) for source_id, _run_at, _minutes in planned}
        for job in list(self.scheduler.get_jobs()):
            if job.id not in wanted:
                self.scheduler.remove_job(job.id)

        for source_id, run_at, minutes in planned:
            self.scheduler.add_job(
                self._execute,
                trigger="interval",
                minutes=minutes,
                next_run_time=run_at,
                args=[str(source_id)],
                id=source_job_id(source_id),
                replace_existing=True,
                misfire_grace_time=60 * 60,
                max_instances=1,
            )
        return [source_job_id(source_id) for source_id, _run_at, _minutes in planned]

    def _execute(self, source_id: str) -> None:
        session: Session = self._sessions()
        try:
            source = session.get(Source, uuid.UUID(source_id))
            if source is None or not source.enabled:
                return
            run_source_crawl(session, source, self._fetch, checked_at=datetime.now(timezone.utc))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
