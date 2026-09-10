"""Deterministic, synthetic e-commerce demo dataset seeding."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import refresh_low_stock_alerts, upsert_inventory_policy
from app.db import (
    DailySkuSale,
    ExternalAccount,
    InventoryBalance,
    Product,
    ProductSku,
    PurchaseRequest,
    Warehouse,
    Workspace,
)
from app.employee_auth import ROLE_ADMIN
from app.replenishment import create_purchase_request_draft, decide_suggestion, generate_suggestion
from app.repository import confirm_inbound, create_inbound, receive_inbound, upsert_product
from app.schemas import ProductRecord

DEMO_DATASET_ID = "demo-v31-v32"
DEMO_SOURCE = "fixture"
DEMO_PLATFORM = "mock"


@dataclass
class DemoSeedReport:
    dataset_id: str
    workspace_id: int
    tenant_key: str
    product_ids: list[int]
    sku_ids: list[int]
    warehouse_ids: list[int]
    suggestion_ids: list[int]
    purchase_request_ids: list[int]
    alert_ids: list[int]
    inbound_ids: list[int]
    simulated: bool = True
    live_enabled: bool = False

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _find_or_create_product(db: Session, workspace_id: int, *, external_id: str, title: str) -> Product:
    existing = db.scalar(select(Product).where(Product.workspace_id == workspace_id, Product.source == DEMO_SOURCE, Product.external_product_id == external_id))
    if existing is not None:
        return existing
    return upsert_product(
        db,
        ProductRecord(
            source=DEMO_SOURCE,
            external_product_id=external_id,
            title=title,
            url=f"https://fixture.local/{external_id}",
            category="[虚构] 演示商品",
            description="[虚构数据] 仅用于本地演示、测试和录屏。",
            current_price=Decimal("39.90"),
            observed_at=datetime(2026, 9, 7, 12, 0, 0),
        ),
        workspace_id=workspace_id,
    )


def _find_or_create_sku(db: Session, workspace_id: int, *, product_id: int, code: str, variant: str) -> ProductSku:
    sku = db.scalar(select(ProductSku).where(ProductSku.workspace_id == workspace_id, ProductSku.sku_code == code))
    if sku is not None:
        return sku
    sku = ProductSku(workspace_id=workspace_id, product_id=product_id, sku_code=code, variant_label=variant, unit="件")
    db.add(sku)
    db.commit()
    db.refresh(sku)
    return sku


def _find_or_create_warehouse(db: Session, workspace_id: int, *, code: str, name: str, warehouse_type: str) -> Warehouse:
    warehouse = db.scalar(select(Warehouse).where(Warehouse.workspace_id == workspace_id, Warehouse.code == code))
    if warehouse is not None:
        return warehouse
    warehouse = Warehouse(workspace_id=workspace_id, code=code, name=name, warehouse_type=warehouse_type, integration_mode="manual")
    db.add(warehouse)
    db.commit()
    db.refresh(warehouse)
    return warehouse


def _ensure_confirmed_inventory(db: Session, *, workspace_id: int, warehouse_id: int, sku_id: int, qty: int, reference: str) -> int:
    from app.db import InboundOrder
    existing = db.scalar(select(InboundOrder).where(InboundOrder.workspace_id == workspace_id, InboundOrder.reference_no == reference))
    if existing is not None:
        return existing.id
    order = create_inbound(db, warehouse_id=warehouse_id, reference_no=reference, lines=[{"sku_id": sku_id, "expected_qty": qty}], created_by="demo-seed", workspace_id=workspace_id)
    receive_inbound(db, inbound_id=order.id, lines=[{"sku_id": sku_id, "received_qty": qty, "damaged_qty": 0}], idempotency_key=f"{reference}:receive", payload_hash=f"demo:{reference}:receive", workspace_id=workspace_id)
    confirm_inbound(db, inbound_id=order.id, confirmed_by="demo-seed", idempotency_key=f"{reference}:confirm", workspace_id=workspace_id)
    return order.id


def _ensure_sales(db: Session, *, workspace_id: int, account_id: int, sku_id: int, external_sku: str, days: int, daily_qty: int, as_of: date) -> None:
    for offset in range(days):
        sales_date = as_of - timedelta(days=days - 1 - offset)
        existing = db.scalar(select(DailySkuSale).where(DailySkuSale.workspace_id == workspace_id, DailySkuSale.external_account_id == account_id, DailySkuSale.external_sku == external_sku, DailySkuSale.sales_date == sales_date))
        if existing is not None:
            continue
        db.add(DailySkuSale(
            workspace_id=workspace_id, external_account_id=account_id, platform=DEMO_PLATFORM,
            account_ref="demo-account", store_ref="demo-store", external_sku=external_sku,
            internal_sku_id=sku_id, sales_date=sales_date, gross_qty=daily_qty,
            cancelled_qty=0, refunded_qty=0, net_qty=daily_qty,
            gross_amount=Decimal(daily_qty * 39), refund_amount=Decimal("0"),
            net_amount=Decimal(daily_qty * 39), order_count=daily_qty,
            data_completeness="complete",
        ))
    db.commit()


def seed_demo_workspace(db: Session, *, tenant_key: str, workspace_name: str, as_of_date: date = date(2026, 9, 7)) -> DemoSeedReport:
    if not tenant_key.startswith("demo-"):
        raise ValueError("DEMO_WORKSPACE_INVALID")
    workspace = db.scalar(select(Workspace).where(Workspace.tenant_key == tenant_key))
    if workspace is None:
        workspace = Workspace(tenant_key=tenant_key, name=workspace_name)
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
    elif workspace.status != "active":
        raise ValueError("WORKSPACE_NOT_FOUND")
    workspace_id = workspace.id

    product_specs = [
        ("demo-tea", "[虚构] 高山绿茶", "DEMO-TEA-GREEN-250", "绿茶 250g"),
        ("demo-mug", "[虚构] 便携茶具", "DEMO-MUG-BASIC", "基础套装"),
        ("demo-lamp", "[虚构] 智能台灯", "DEMO-LAMP-DESK", "桌面款"),
    ]
    products: list[Product] = []
    skus: list[ProductSku] = []
    for external_id, title, code, variant in product_specs:
        product = _find_or_create_product(db, workspace_id, external_id=external_id, title=title)
        products.append(product)
        skus.append(_find_or_create_sku(db, workspace_id, product_id=product.id, code=code, variant=variant))

    warehouse = _find_or_create_warehouse(db, workspace_id, code="DEMO-OWN-01", name="[虚构] 自有演示仓", warehouse_type="own")
    warehouse_3pl = _find_or_create_warehouse(db, workspace_id, code="DEMO-3PL-01", name="[虚构] 第三方演示仓", warehouse_type="third_party")
    inventory_targets = [4, 30, 8]
    inbound_ids = []
    for sku, qty in zip(skus, inventory_targets):
        inbound_ids.append(_ensure_confirmed_inventory(db, workspace_id=workspace_id, warehouse_id=warehouse.id, sku_id=sku.id, qty=qty, reference=f"DEMO-IN-{sku.sku_code}"))
    _ensure_confirmed_inventory(db, workspace_id=workspace_id, warehouse_id=warehouse_3pl.id, sku_id=skus[0].id, qty=2, reference="DEMO-IN-3PL-GREEN")

    account = db.scalar(select(ExternalAccount).where(ExternalAccount.workspace_id == workspace_id, ExternalAccount.platform == DEMO_PLATFORM, ExternalAccount.account_ref == "demo-account", ExternalAccount.store_ref == "demo-store"))
    if account is None:
        account = ExternalAccount(workspace_id=workspace_id, platform=DEMO_PLATFORM, account_ref="demo-account", store_ref="demo-store", source_mode="mock", simulated=True)
        db.add(account)
        db.commit()
        db.refresh(account)
    _ensure_sales(db, workspace_id=workspace_id, account_id=account.id, sku_id=skus[0].id, external_sku=skus[0].sku_code, days=30, daily_qty=3, as_of=as_of_date)
    _ensure_sales(db, workspace_id=workspace_id, account_id=account.id, sku_id=skus[1].id, external_sku=skus[1].sku_code, days=30, daily_qty=1, as_of=as_of_date)
    _ensure_sales(db, workspace_id=workspace_id, account_id=account.id, sku_id=skus[2].id, external_sku=skus[2].sku_code, days=5, daily_qty=2, as_of=as_of_date)

    policies = [(skus[0].id, 5, 8), (skus[1].id, 5, 8), (skus[2].id, 2, 5)]
    for sku_id, safety, reorder in policies:
        upsert_inventory_policy(db, workspace_id=workspace_id, warehouse_id=warehouse.id, sku_id=sku_id, safety_stock_qty=safety, reorder_point_qty=reorder)
    alerts = refresh_low_stock_alerts(db, workspace_id=workspace_id)

    suggestion_ids = []
    purchase_request_ids = []
    low = db.scalar(select(__import__("app.db", fromlist=["ProductSku"]).ProductSku).where(ProductSku.id == skus[0].id))
    suggestion = db.scalar(select(__import__("app.db", fromlist=["ReplenishmentSuggestion"]).ReplenishmentSuggestion).where(__import__("app.db", fromlist=["ReplenishmentSuggestion"]).ReplenishmentSuggestion.workspace_id == workspace_id, __import__("app.db", fromlist=["ReplenishmentSuggestion"]).ReplenishmentSuggestion.sku_id == skus[0].id, __import__("app.db", fromlist=["ReplenishmentSuggestion"]).ReplenishmentSuggestion.active_slot == "open"))
    if suggestion is None:
        try:
            suggestion = generate_suggestion(db, workspace_id=workspace_id, warehouse_id=warehouse.id, sku_id=skus[0].id, actor="demo-seed", coverage_days=14, as_of=as_of_date)
        except ValueError as exc:
            suggestion = None
    if suggestion is not None:
        if suggestion.status == "suggested":
            suggestion = decide_suggestion(db, workspace_id=workspace_id, suggestion_id=suggestion.id, action="confirm", actor="demo-seed", idempotency_key="demo-decision-green", expected_version=suggestion.version)
        suggestion_ids.append(suggestion.id)
        existing_request = db.scalar(select(PurchaseRequest).join(__import__("app.db", fromlist=["PurchaseRequestLine"]).PurchaseRequestLine, __import__("app.db", fromlist=["PurchaseRequestLine"]).PurchaseRequestLine.purchase_request_id == PurchaseRequest.id).where(PurchaseRequest.workspace_id == workspace_id, __import__("app.db", fromlist=["PurchaseRequestLine"]).PurchaseRequestLine.suggestion_id == suggestion.id))
        if existing_request is None:
            draft = create_purchase_request_draft(db, workspace_id=workspace_id, suggestion_ids=[suggestion.id], actor="demo-seed", idempotency_key="demo-draft-green", note="[虚构数据] 仅用于演示", supplier_ref="[虚构] demo-supplier")
            existing_request = draft
        purchase_request_ids.append(existing_request.id)
    return DemoSeedReport(DEMO_DATASET_ID, workspace_id, tenant_key, [p.id for p in products], [s.id for s in skus], [warehouse.id, warehouse_3pl.id], suggestion_ids, purchase_request_ids, [a.id for a in alerts], inbound_ids)
