"""Publish canonical jobs from raw payloads already stored for enabled sources.

python -m scripts.publish_raw
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.connectors.registry import build_connector
from app.db import SessionLocal
from app.models.source import Source
from app.pipeline.publish import publish_latest_raw


def main() -> None:
    checked_at = datetime.now(timezone.utc)
    session = SessionLocal()
    try:
        sources = list(session.scalars(select(Source).where(Source.enabled.is_(True))).all())
        for source in sources:
            connector = build_connector(source.source_type, company_name=source.company_name)
            counts = publish_latest_raw(session, source, connector, checked_at=checked_at)
            session.commit()
            print(
                f"{source.external_key}: created={counts['created']} "
                f"updated={counts['updated']} rejected={counts['rejected']}"
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
