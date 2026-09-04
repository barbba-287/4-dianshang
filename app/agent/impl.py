"""Agent 只读工具实现。

S5 阶段仅暴露以下 3 个工具，全部 read-only：
- search_products：按关键词与分类分页查询；
- get_product_detail：按 id 查商品；
- get_price_history：按 id 查价格历史。

任何写入、订单、改价、删除操作均未实现。
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.registry import AgentTool, ToolContext
from app.db import Product, ProductPriceHistory, SessionLocal
from app.schemas import ProductRecord


def _serialize_product(product: Product) -> dict:
    return {
        "id": product.id,
        "source": product.source,
        "external_product_id": product.external_product_id,
        "title": product.title,
        "category": product.category,
        "description": product.description,
        "rating": float(product.rating) if product.rating is not None else None,
        "current_price": float(product.current_price),
        "currency": product.currency,
        "url": product.url,
        "first_seen_at": product.first_seen_at.isoformat() if product.first_seen_at else None,
        "last_seen_at": product.last_seen_at.isoformat() if product.last_seen_at else None,
    }


def search_products_handler(payload: dict, context: ToolContext) -> dict:
    """分页 + 关键词 + 分类筛选；limit <= 100。"""
    keyword = (payload.get("keyword") or "").strip() or None
    category = (payload.get("category") or "").strip() or None
    page = max(1, int(payload.get("page", 1)))
    page_size = min(100, max(1, int(payload.get("page_size", 20))))

    db: Session = SessionLocal()
    try:
        filters = [Product.workspace_id == context.workspace_id]
        if keyword:
            filters.append(Product.title.contains(keyword))
        if category:
            filters.append(Product.category == category)
        total = db.scalar(select(func.count(Product.id)).where(*filters)) or 0
        items = db.scalars(
            select(Product)
            .where(*filters)
            .order_by(Product.last_seen_at.desc(), Product.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [_serialize_product(p) for p in items],
        }
    finally:
        db.close()


def get_product_detail_handler(payload: dict, context: ToolContext) -> dict:
    product_id = int(payload["product_id"])
    db: Session = SessionLocal()
    try:
        product = db.scalar(select(Product).where(Product.id == product_id, Product.workspace_id == context.workspace_id))
        if product is None:
            return {"error": "PRODUCT_NOT_FOUND", "product_id": product_id}
        return _serialize_product(product)
    finally:
        db.close()


def get_price_history_handler(payload: dict, context: ToolContext) -> dict:
    product_id = int(payload["product_id"])
    db: Session = SessionLocal()
    try:
        product = db.scalar(select(Product).where(Product.id == product_id, Product.workspace_id == context.workspace_id))
        if product is None:
            return {"error": "PRODUCT_NOT_FOUND", "product_id": product_id}
        rows = db.scalars(
            select(ProductPriceHistory)
            .where(
                ProductPriceHistory.product_id == product_id,
                ProductPriceHistory.workspace_id == context.workspace_id,
            )
            .order_by(
                ProductPriceHistory.observed_at.asc(),
                ProductPriceHistory.id.asc(),
            )
        ).all()
        return {
            "product_id": product_id,
            "items": [
                {
                    "id": r.id,
                    "price": float(r.price),
                    "currency": r.currency,
                    "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                    "source_run_id": r.source_run_id,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


SEARCH_PRODUCTS_TOOL = AgentTool(
    name="search_products",
    description="按关键词与分类分页查询商品；只读，不可写入。",
    input_schema={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "商品名关键词，可选"},
            "category": {"type": "string", "description": "分类，可选"},
            "page": {"type": "integer", "description": "页码（>=1）"},
            "page_size": {"type": "integer", "description": "每页大小（1..100）"},
        },
        "required": [],
    },
    handler=search_products_handler,
    is_readonly=True,
    timeout_seconds=3.0,
    max_calls_per_minute=60,
    output_schema={
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
            "items": {"type": "array"},
        },
    },
)


GET_PRODUCT_DETAIL_TOOL = AgentTool(
    name="get_product_detail",
    description="按 id 查询商品详情；只读。",
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "商品 id"},
        },
        "required": ["product_id"],
    },
    handler=get_product_detail_handler,
    is_readonly=True,
    timeout_seconds=3.0,
    max_calls_per_minute=60,
)


GET_PRICE_HISTORY_TOOL = AgentTool(
    name="get_price_history",
    description="按商品 id 查询价格历史；只读。",
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "商品 id"},
        },
        "required": ["product_id"],
    },
    handler=get_price_history_handler,
    is_readonly=True,
    timeout_seconds=3.0,
    max_calls_per_minute=60,
)


def register_default_tools(registry) -> None:
    """把默认工具挂到指定 registry；幂等，已存在则跳过。"""
    for tool in (SEARCH_PRODUCTS_TOOL, GET_PRODUCT_DETAIL_TOOL, GET_PRICE_HISTORY_TOOL):
        if registry.get(tool.name) is None:
            registry.register(tool)


# 模块导入时自动把默认工具注册到 default_registry
from app.agent.registry import default_registry as _default_registry

register_default_tools(_default_registry)