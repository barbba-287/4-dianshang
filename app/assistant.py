"""Deterministic read-only operator assistant over structured business data."""
from __future__ import annotations

from datetime import date
import re
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.dashboard import build_sku_health, build_sync_health_summary
from app.db import Product, ProductSku, Warehouse
from app.replenishment import list_suggestions
from app.skills_ecommerce import SkillContext, analyze_ad_roi, analyze_sales_fluctuation, check_inventory_alert, find_product_opportunities
from app.skills_ecommerce_platform import PlatformEcomDataSource
from app.schemas import ReplenishmentSuggestionResponse

WRITE_TERMS = ("提交采购", "创建采购", "确认补货", "生成补货", "修改库存", "调整库存", "自动下单", "取消订单", "退款", "改价", "入库确认", "重试同步")
INTENT_TERMS = (
    ("ad_roi", ("广告 roi", "广告ROI", "广告投产", "投产比", "广告效果")),
    ("sales_fluctuation", ("销售波动", "销量趋势", "销量变化", "销售趋势")),
    ("product_opportunities", ("商品机会", "选品机会", "机会商品", "潜力商品")),
    ("inventory_alert", ("库存预警", "库存告警", "低库存预警")),
    ("sync_health", ("同步健康", "同步状态", "同步失败", "同步停滞", "同步运行")),
    ("replenishment", ("补货建议", "建议补货", "补多少", "补货情况")),
    ("inventory_health", ("库存健康", "库存风险", "可售天数", "库存情况")),
    ("catalog", ("商品资料", "商品信息", "商品", "sku", "SKU")),
)

LIMITATIONS = ["仅查询当前工作空间的结构化业务数据", "不接入商品知识库/RAG，不执行任何写操作"]

def route_intent(message: str) -> tuple[str, str | None]:
    text = message.strip().casefold()
    if any(term.casefold() in text for term in WRITE_TERMS):
        return "readonly_rejected", "READONLY_ONLY"
    for intent, terms in INTENT_TERMS:
        if any(term.casefold() in text for term in terms):
            return intent, None
    return "unknown", "UNKNOWN_INTENT"

STATUS_LABELS={"healthy":"健康","reorder":"建议补货","urgent":"紧急","data_insufficient":"数据不足","no_sales":"无销量","suggested":"待处理","modified":"已调整","confirmed":"已确认","ignored":"已忽略","submitted":"已提交","running":"运行中","succeeded":"成功","failed":"失败","partial":"部分成功","stalled":"停滞"}
COMPLETENESS_LABELS={"complete":"完整","insufficient":"数据不足","partial":"部分完整"}
REASON_LABELS={"LOW_STOCK":"库存偏低","STOCK_SUFFICIENT":"库存充足","INCOMPLETE_COVERAGE":"销量覆盖不完整","NO_SALES":"暂无销量"}
def label(value, mapping, fallback="其他"):
    if value is None or value=="": return "数据不足"
    return mapping.get(value, f"{fallback}（{value}）")

def enrich_sku_rows(db, rows):
    sku_ids=[row.get("sku_id") for row in rows if row.get("sku_id") is not None]
    skus={x.id:x for x in db.scalars(select(ProductSku).where(ProductSku.id.in_(sku_ids))).all()} if sku_ids else {}
    product_ids=[x.product_id for x in skus.values()]
    products={x.id:x for x in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()} if product_ids else {}
    warehouses={x.id:x for x in db.scalars(select(Warehouse).where(Warehouse.id.in_([row.get("warehouse_id") for row in rows if row.get("warehouse_id")]))).all()}
    out=[]
    for row in rows:
        sku=skus.get(row.get("sku_id")); product=products.get(sku.product_id) if sku else None; warehouse=warehouses.get(row.get("warehouse_id"))
        item=dict(row); item.update({"product_title":product.title if product else None,"category":product.category if product else None,"variant_label":sku.variant_label if sku else None,"warehouse_name":warehouse.name if warehouse else None,"status_label":label(item.get("status"),STATUS_LABELS),"data_completeness_label":label(item.get("data_completeness"),COMPLETENESS_LABELS),"reason_label":label(item.get("reason"),REASON_LABELS,"未知原因")})
        out.append(item)
    return out

def query_assistant(db: Session, *, workspace_id: int, message: str) -> dict:
    intent, error = route_intent(message)
    if error:
        answer = "这是写入或未支持的操作；当前助手只支持查询运营数据。" if intent == "readonly_rejected" else "我目前支持查询商品、库存健康、补货建议和同步状态。"
        return {"ok": False, "intent": intent, "answer": answer, "data": None, "error_code": error, "limitations": LIMITATIONS}
    if intent == "sync_health":
        data = build_sync_health_summary(db, workspace_id=workspace_id)
        answer = "已返回当前工作空间的同步健康状态。"
    elif intent == "inventory_health":
        rows = build_sku_health(db, workspace_id=workspace_id, coverage_days=14)[:100]
        health_rows = enrich_sku_rows(db, rows)
        for item in health_rows:
            sales = item.get("sales", {}).get("14", {})
            item["data_completeness"] = sales.get("data_completeness")
            item["data_completeness_label"] = label(item["data_completeness"], COMPLETENESS_LABELS)
            item["reason_label"] = label(item.get("reason"), REASON_LABELS, "未知原因")
        data = {"items": health_rows, "total": len(health_rows), "coverage_days": 14}
        answer = f"已返回 {len(rows)} 个 SKU 的库存健康数据。"
    elif intent in {"ad_roi", "sales_fluctuation", "inventory_alert", "product_opportunities"}:
        source = PlatformEcomDataSource()
        context = SkillContext(workspace_id=workspace_id, actor="assistant", request_id="assistant-query", source_mode="platform")
        if intent == "ad_roi":
            data = analyze_ad_roi(campaign_id="campaign-001", context=context, source=source)
            answer = "当前项目暂未建广告事实表，因此广告 ROI 返回 unknown；接入 Webman 广告数据后可复用同一 Skill。"
        elif intent == "sales_fluctuation":
            sku = db.scalar(select(ProductSku).where(ProductSku.workspace_id == workspace_id, ProductSku.is_active.is_(True)).order_by(ProductSku.id))
            if sku is None:
                data = {"metric": "sales_fluctuation", "status": "insufficient", "reason": "SKU_NOT_FOUND", "source": "platform", "data_completeness": "insufficient"}
                answer = "当前工作空间没有可分析的启用 SKU。"
            else:
                data = analyze_sales_fluctuation(sku_id=sku.id, days=7, context=context, source=source)
                data["status"] = "known" if data.get("data_completeness") == "complete" else "insufficient"
                answer = f"已按当前工作空间的 SKU {sku.sku_code} 返回销售波动分析；数据不足时不会补零。"
        elif intent == "inventory_alert":
            warehouse = db.scalar(select(Warehouse).where(Warehouse.workspace_id == workspace_id, Warehouse.is_active.is_(True)).order_by(Warehouse.id))
            if warehouse is None:
                data = {"metric": "inventory_alert", "status": "insufficient", "reason": "WAREHOUSE_NOT_FOUND", "source": "platform", "data_completeness": "insufficient", "alerts": []}
                answer = "当前工作空间没有可分析的启用仓库。"
            else:
                data = check_inventory_alert(warehouse_id=warehouse.id, context=context, source=source)
                answer = f"已按当前工作空间仓库 {warehouse.code} 返回库存预警；外部 Shopify 库存观察不会直接覆盖内部库存台账。"
        else:
            data = find_product_opportunities(category=None, context=context, source=source)
            answer = "已返回当前平台商品机会候选；当前增长/库存证据不足时不会编造机会结论。"
    elif intent == "replenishment":
        rows, total = list_suggestions(db, workspace_id=workspace_id, limit=100, offset=0)
        items = enrich_sku_rows(db, [ReplenishmentSuggestionResponse.model_validate(row).model_dump(mode="json") for row in rows])
        quality = "empty" if not total else "insufficient" if any(item.get("data_completeness") == "insufficient" for item in items) else "complete"
        data = {"items": items, "page": 1, "page_size": 100, "total": int(total), "meta": {"data_quality": quality}}
        answer = "当前没有可展示的补货建议。补货建议需要先有库存、策略和完整销量数据；这不代表所有 SKU 都无需补货。" if not total else f"当前有 {total} 条已生成的补货建议，下面列出前 {len(items)} 条。"
    else:
        products = db.scalars(select(Product).where(Product.workspace_id == workspace_id).order_by(Product.id.desc()).limit(50)).all()
        skus = db.scalars(select(ProductSku).where(ProductSku.workspace_id == workspace_id, ProductSku.is_active.is_(True)).order_by(ProductSku.id).limit(100)).all()
        data = {"items": [{"product_id": s.product_id, "product_title": next((p.title for p in products if p.id == s.product_id), None), "category": next((p.category for p in products if p.id == s.product_id), None), "current_price": float(next((p.current_price for p in products if p.id == s.product_id), 0)), "sku_id": s.id, "sku_code": s.sku_code, "variant_label": s.variant_label} for s in skus]}
        answer = f"已返回 {len(products)} 个商品和 {len(skus)} 个启用 SKU。"
    return {"ok": True, "intent": intent, "answer": answer, "data": data, "error_code": None, "limitations": LIMITATIONS}
