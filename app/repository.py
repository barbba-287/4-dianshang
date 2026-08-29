"""电商商品与客服工作台的数据写入服务。

用途：将标准化商品记录以业务唯一键幂等写入数据库，维护当前价格、
价格历史和采集任务状态，保证重复采集不会制造重复商品。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import CrawlJob, Product, ProductPriceHistory
from app.schemas import ProductRecord


def upsert_product(db: Session, record: ProductRecord, *, source_run_id: str | None = None) -> Product:
    product = db.scalar(
        select(Product).where(
            Product.source == record.source,
            Product.external_product_id == record.external_product_id,
        )
    )
    observed_at = record.observed_at
    if product is None:
        product = Product(
            source=record.source,
            external_product_id=record.external_product_id,
            title=record.title,
            url=str(record.url),
            category=record.category,
            description=record.description,
            rating=record.rating,
            current_price=record.current_price,
            currency=record.currency,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            updated_at=observed_at,
        )
        db.add(product)
        db.flush()
        db.add(
            ProductPriceHistory(
                product_id=product.id,
                price=record.current_price,
                currency=record.currency,
                observed_at=observed_at,
                source_run_id=source_run_id,
            )
        )
    else:
        price_changed = product.current_price != record.current_price
        product.title = record.title
        product.url = str(record.url)
        product.category = record.category
        product.description = record.description
        product.rating = record.rating
        product.last_seen_at = observed_at
        product.updated_at = observed_at
        if price_changed:
            product.current_price = record.current_price
            db.add(
                ProductPriceHistory(
                    product_id=product.id,
                    price=record.current_price,
                    currency=record.currency,
                    observed_at=observed_at,
                    source_run_id=source_run_id,
                )
            )
    return product


def import_records(
    db: Session,
    records: list[ProductRecord],
    *,
    keyword: str | None = None,
    job: CrawlJob | None = None,
) -> CrawlJob:
    run_id = uuid4().hex
    if job is None:
        job = CrawlJob(
            source=records[0].source if records else "fixture",
            keyword=keyword,
            status="running",
            started_at=datetime.utcnow(),
        )
        db.add(job)
        db.flush()
    try:
        for record in records:
            upsert_product(db, record, source_run_id=run_id)
        job.status = "succeeded"
        job.finished_at = datetime.utcnow()
        job.cursor = str(len(records))
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(CrawlJob, job.id)
        if job:
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.error_code = "IMPORT_FAILED"
            job.error_message = str(exc)
            db.commit()
        raise
    return job


def start_job(db: Session, *, source: str, keyword: str | None = None) -> CrawlJob:
    job = CrawlJob(
        source=source,
        keyword=keyword,
        status="running",
        started_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    return job


def fail_job(db: Session, job: CrawlJob, *, error_code: str, message: str) -> None:
    job.status = "failed"
    job.finished_at = datetime.utcnow()
    job.error_code = error_code
    job.error_message = message
    db.commit()
