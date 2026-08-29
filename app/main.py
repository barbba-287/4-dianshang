"""电商商品与客服工作台的 FastAPI 应用入口。

用途：提供商品采集、分页筛选、商品详情、价格历史、任务状态和健康检查
接口，并承载第一周演示用的极简网页。
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crawler import CrawlError, collect_fixture
from app.db import CrawlJob, Product, ProductPriceHistory, get_db, init_db
from app.repository import fail_job, import_records, start_job
from app.schemas import CrawlJobResponse, PriceHistoryResponse, ProductPage, ProductResponse


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title=get_settings().app_name, version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    template = Path(__file__).parent / "templates" / "index.html"
    return template.read_text(encoding="utf-8")


@app.post("/api/crawl/fixture", response_model=CrawlJobResponse, status_code=201)
def crawl_fixture(db: Session = Depends(get_db)) -> CrawlJob:
    job = start_job(db, source="fixture", keyword="fixture")
    try:
        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "products.html"
        records = collect_fixture(fixture)
        return import_records(db, records, keyword="fixture", job=job)
    except CrawlError as exc:
        db.rollback()
        job = db.get(CrawlJob, job.id)
        if job is not None:
            fail_job(db, job, error_code=exc.code, message=exc.message)
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@app.get("/api/products", response_model=ProductPage)
def list_products(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
) -> ProductPage:
    filters = []
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
    return ProductPage(items=items, page=page, page_size=page_size, total=total)


@app.get("/api/products/{product_id}", response_model=ProductResponse)
def get_product(product_id: int, db: Session = Depends(get_db)) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail={"code": "PRODUCT_NOT_FOUND", "message": "商品不存在"})
    return product


@app.get("/api/products/{product_id}/price-history", response_model=list[PriceHistoryResponse])
def price_history(product_id: int, db: Session = Depends(get_db)) -> list[ProductPriceHistory]:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail={"code": "PRODUCT_NOT_FOUND", "message": "商品不存在"})
    return db.scalars(
        select(ProductPriceHistory)
        .where(ProductPriceHistory.product_id == product_id)
        .order_by(ProductPriceHistory.observed_at.asc(), ProductPriceHistory.id.asc())
    ).all()


@app.get("/api/crawl/jobs", response_model=list[CrawlJobResponse])
def list_jobs(
    limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)
) -> list[CrawlJob]:
    return db.scalars(select(CrawlJob).order_by(CrawlJob.id.desc()).limit(limit)).all()
