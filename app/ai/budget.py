from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session


def try_spend(session: Session, day: date, budget: int) -> bool:
    """Count one outbound request if today's total is still under the budget."""
    session.execute(
        text(
            """
            INSERT INTO ai_budget (day, request_count)
            VALUES (:day, 0)
            ON CONFLICT (day) DO NOTHING
            """
        ),
        {"day": day},
    )
    spent = session.execute(
        text(
            """
            UPDATE ai_budget
            SET request_count = request_count + 1
            WHERE day = :day AND request_count < :budget
            RETURNING request_count
            """
        ),
        {"day": day, "budget": budget},
    ).first()
    session.flush()
    return spent is not None
