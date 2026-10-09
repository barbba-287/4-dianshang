"""Platform-backed data source for the read-only e-commerce Skills."""
from __future__ import annotations

from sqlalchemy import select

from app.db import DailySkuSale, InventoryBalance, InventoryPolicy, Product, ProductSku, SessionLocal


class PlatformEcomDataSource:
    """Read-only adapter backed by this project's current workspace database."""

    def sales(self, *, workspace_id: int, sku_id: int, days: int) -> list[dict]:
        db = SessionLocal()
        try:
            rows = db.scalars(
                select(DailySkuSale)
                .where(
                    DailySkuSale.workspace_id == workspace_id,
                    DailySkuSale.internal_sku_id == sku_id,
                )
                .order_by(DailySkuSale.sales_date.desc())
                .limit(days)
            ).all()
            return [
                {
                    "sales_date": row.sales_date.isoformat(),
                    "net_qty": row.net_qty,
                    "data_completeness": row.data_completeness,
                }
                for row in reversed(rows)
            ]
        finally:
            db.close()

    def ad(self, *, workspace_id: int, campaign_id: str) -> dict | None:
        # Advertising facts are not modeled in the current project yet.
        return None

    def inventory(self, *, workspace_id: int, warehouse_id: int) -> list[dict]:
        db = SessionLocal()
        try:
            rows = db.execute(
                select(InventoryBalance, ProductSku, InventoryPolicy)
                .join(ProductSku, ProductSku.id == InventoryBalance.sku_id)
                .outerjoin(
                    InventoryPolicy,
                    (InventoryPolicy.sku_id == InventoryBalance.sku_id)
                    & (InventoryPolicy.warehouse_id == InventoryBalance.warehouse_id)
                    & (InventoryPolicy.workspace_id == workspace_id),
                )
                .where(
                    InventoryBalance.workspace_id == workspace_id,
                    InventoryBalance.warehouse_id == warehouse_id,
                )
            ).all()
            result = []
            for balance, sku, policy in rows:
                result.append(
                    {
                        "sku_id": sku.id,
                        "sku_code": sku.sku_code,
                        "on_hand_qty": balance.on_hand_qty,
                        "reorder_point_qty": policy.reorder_point_qty if policy else 0,
                        "days_of_inventory": None,
                    }
                )
            # Shopify locations are external observations and must not be
            # silently merged into the internal inventory ledger.
            return result
        finally:
            db.close()

    def products(self, *, workspace_id: int, category: str | None) -> list[dict]:
        db = SessionLocal()
        try:
            query = select(Product).where(Product.workspace_id == workspace_id)
            if category:
                query = query.where(Product.category == category)
            return [
                {
                    "product_id": row.id,
                    "title": row.title,
                    "growth_rate": None,
                    "days_of_inventory": None,
                }
                for row in db.scalars(query).all()
            ]
        finally:
            db.close()
