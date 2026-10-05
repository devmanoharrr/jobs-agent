import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class JobiSync(Base):
    """A published job accepted by one Jobi target.

    Local and prod keep separate rows, so a job posted to local still waits for prod.
    """

    __tablename__ = "jobi_syncs"
    __table_args__ = (UniqueConstraint("target", "external_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    external_key: Mapped[str] = mapped_column(Text, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
