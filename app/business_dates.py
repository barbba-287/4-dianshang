"""Business-date helpers for sales-based metrics."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import DailySkuSale


def resolve_sales_as_of(
    db: Session,
    *,
    workspace_id: int,
    explicit_as_of: date | None = None,
) -> date:
    """Resolve one comparable sales cutoff for a workspace.

    Explicit replay dates win. Otherwise use the latest persisted sales fact;
    an empty workspace falls back to today's UTC date so empty responses keep
    their existing shape and semantics.
    """
    if explicit_as_of is not None:
        return explicit_as_of
    latest = db.scalar(
        select(func.max(DailySkuSale.sales_date)).where(
            DailySkuSale.workspace_id == workspace_id,
            DailySkuSale.data_completeness == "complete",
        )
    )
    if latest is not None:
        return latest
    # If a workspace has only partial upstream facts, retain a usable cutoff
    # rather than pretending those rows provide complete internal coverage.
    latest_any = db.scalar(
        select(func.max(DailySkuSale.sales_date)).where(
            DailySkuSale.workspace_id == workspace_id,
        )
    )
    return latest_any or datetime.utcnow().date()
