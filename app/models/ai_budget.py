from datetime import date

from sqlalchemy import Date, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AiBudget(Base):
    __tablename__ = "ai_budget"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
