import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, Text, Uuid, desc, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.source import Source


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("jobs_india_idx", "india_relevance", "status"),
        Index("jobs_company_idx", "company_normalized"),
        Index("jobs_posted_idx", desc("posted_at")),
        Index(
            "jobs_title_trgm_idx",
            "title_original",
            postgresql_using="gin",
            postgresql_ops={"title_original": "gin_trgm_ops"},
        ),
        Index(
            "jobs_company_name_trgm_idx",
            "company_name",
            postgresql_using="gin",
            postgresql_ops={"company_name": "gin_trgm_ops"},
        ),
        Index(
            "jobs_description_trgm_idx",
            "description_text",
            postgresql_using="gin",
            postgresql_ops={"description_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title_original: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    company_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    description_text: Mapped[str | None] = mapped_column(Text)
    description_html: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    country_code: Mapped[str | None] = mapped_column(Text)
    work_mode: Mapped[str | None] = mapped_column(Text)
    india_relevance: Mapped[str] = mapped_column(Text, nullable=False)
    employment_type: Mapped[str | None] = mapped_column(Text)
    experience_min: Mapped[Decimal | None] = mapped_column(Numeric)
    experience_max: Mapped[Decimal | None] = mapped_column(Numeric)
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric)
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric)
    salary_currency: Mapped[str | None] = mapped_column(Text)
    salary_period: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    role_category: Mapped[str | None] = mapped_column(Text)
    experience_label: Mapped[str | None] = mapped_column(Text)
    responsibilities: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    requirements: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    nice_to_have: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    benefits: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    structured_payload: Mapped[dict | None] = mapped_column(JSONB)
    pipeline_trace: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    structure_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default=text("'pending'")
    )
    structure_hash: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default=text("'active'"))
    canonical_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("sources.id"))
    canonical_apply_url: Mapped[str | None] = mapped_column(Text)
    exact_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    fuzzy_fingerprint: Mapped[str | None] = mapped_column(Text)
    ai_enriched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    enrichment_version: Mapped[str | None] = mapped_column(Text)

    canonical_source: Mapped[Source | None] = relationship(foreign_keys=[canonical_source_id])
