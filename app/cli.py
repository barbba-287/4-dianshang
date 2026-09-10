"""电商工作台的 CLI 命令。

用法
----

```bash
# 查看可用命令
python -m app.cli --help

# 初始化数据库表（幂等）
python -m app.cli init-db

# 向 uploads/ 写入示例 PDF + DOCX 并同步解析、索引（可用 --workspace 指定商家）
python -m app.cli seed-docs [--workspace <workspace-id 或 tenant-key>]

# 入队 fixture 采集任务，等待 ThreadPoolExecutor 完成（可用 --workspace 指定商家）
python -m app.cli enqueue-crawl [--workspace <workspace-id 或 tenant-key>]

# RAG 客服问答
python -m app.cli query "green tea"

# 打印运行时配置与规模
python -m app.cli status
```
"""

import io
import sys
from pathlib import Path
import argparse

from app.background import reset_executor, submit_crawl_fixture, wait_for_jobs
from app.config import get_settings
from app.db import CrawlJob, SessionLocal
from app.importers import get_parser
from app.rag_runtime import get_answerer, get_retriever
from app.repository import (
    append_document_chunk,
    mark_version_ready,
    upsert_document,
    upsert_document_version,
)
from app.main import safe_database_url
from app.storage import save_upload
from app.versioning import content_sha256, estimate_token_count


def _option(args: list[str], name: str, default: str | None = None) -> str | None:
    if name not in args:
        return default
    index = args.index(name)
    if index + 1 >= len(args):
        raise ValueError(f"缺少参数: {name}")
    return args[index + 1]


def _resolve_cli_workspace(db, args: list[str]) -> int:
    """解析 CLI 写入目标；多商家场景禁止猜测。"""
    from sqlalchemy import func, select
    from app.db import Workspace

    value = _option(args, "--workspace")
    active = db.scalars(select(Workspace).where(Workspace.status == "active").order_by(Workspace.id)).all()
    if value:
        try:
            workspace = db.scalar(select(Workspace).where(Workspace.status == "active", Workspace.id == int(value))) if value.isdigit() else db.scalar(select(Workspace).where(Workspace.status == "active", Workspace.tenant_key == value))
        except (ValueError, OverflowError):
            raise ValueError("WORKSPACE_NOT_FOUND") from None
        if workspace is None:
            raise ValueError("WORKSPACE_NOT_FOUND")
        return workspace.id
    if len(active) == 1:
        return active[0].id
    if len(active) > 1:
        raise ValueError("WORKSPACE_SELECTION_REQUIRED")
    total_workspaces = db.scalar(select(func.count(Workspace.id))) or 0
    if total_workspaces:
        raise ValueError("WORKSPACE_REQUIRED")
    from app.db import Product, Document, CrawlJob
    has_data = any(db.scalar(select(func.count(model.id))) for model in (Product, Document, CrawlJob))
    if has_data:
        raise ValueError("WORKSPACE_REQUIRED")
    from app.config import get_settings
    settings = get_settings()
    workspace = Workspace(tenant_key=settings.auth_bootstrap_tenant_key, name=settings.auth_bootstrap_workspace_name)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace.id


def _resolve_workspace_or_report(db, args: list[str]) -> int | None:
    try:
        return _resolve_cli_workspace(db, args)
    except (ValueError, OverflowError) as exc:
        print(f"工作空间解析失败: {exc}")
        return None


def _ensure_schema() -> None:
    """按数据库状态执行 Alembic 初始化或升级。"""
    # 复用项目根目录的 state-aware 初始化逻辑，兼容新库、legacy 库和
    # 已迁移库，避免 CLI 与 `python init_db.py` 的行为分叉。
    from init_db import main as init_db_main

    init_db_main()


def cmd_init_db(args: list[str]) -> int:
    """按数据库状态通过 Alembic 初始化或升级到最新版本。"""
    _ensure_schema()
    return 0


def _seed_sample_pdf() -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, invariant=1)
    c.drawString(72, 720, "Premium green tea leaves spring harvest 250g")
    c.showPage()
    c.drawString(72, 720, "Care: brew at 80 degrees for 2 minutes")
    c.showPage()
    c.save()
    return buf.getvalue()


def _seed_sample_docx() -> bytes:
    from docx import Document as DocxDocument

    doc = DocxDocument()
    doc.add_heading("Return Policy", level=1)
    doc.add_paragraph("7 days no-reason return allowed.")
    doc.add_paragraph("Items must be in original packaging.")
    doc.add_heading("Shipping", level=1)
    doc.add_paragraph("Free shipping on orders over 99 yuan.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def cmd_seed_docs(args: list[str]) -> int:
    """向 uploads/ 写入示例 PDF + DOCX 并入队文档导入 / 索引任务。

    若 schema 未初始化，会自动调用 init-db 创建（便于一次性演示）。
    """
    # 若 schema 未初始化，会自动调用 Alembic 初始化（便于一次性演示）。
    from sqlalchemy import inspect

    from app.db import engine as db_engine

    insp = inspect(db_engine)
    if not {"products", "documents"}.issubset(set(insp.get_table_names())):
        print("schema 未初始化，自动调用 init-db ...")
        _ensure_schema()

    samples = [
        ("seed-tea.pdf", _seed_sample_pdf(), "application/pdf"),
        ("seed-rules.docx", _seed_sample_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ]
    db = SessionLocal()
    try:
        workspace_id = _resolve_cli_workspace(db, args)
        for filename, content, _mime in samples:
            source_type = "pdf" if filename.endswith(".pdf") else "docx"
            sha = content_sha256(content)
            document, _ = upsert_document(
                db,
                source_type=source_type,
                title=filename,
                filename=filename,
                sha256_hex=sha,
                workspace_id=workspace_id,
            )
            storage_uri = save_upload(
                source_type=source_type,
                sha256_hex=sha,
                ext=filename.rsplit(".", 1)[-1],
                content=content,
            )
            version, version_created = upsert_document_version(
                db,
                document=document,
                sha256_hex=sha,
                size_bytes=len(content),
                storage_uri=storage_uri,
                workspace_id=workspace_id,
            )
            db.commit()
            if not version_created and version.status == "ready":
                print(
                    f"reused {filename} -> document #{document.id} "
                    f"version #{version.id} (already ready)"
                )
                continue
            print(f"seeded {filename} -> document #{document.id} version #{version.id}")

            # 直接同步解析与索引（不走后台 executor，便于演示脚本）
            parser = get_parser(source_type)
            chunks = list(parser.parse(content, filename=filename))
            import hashlib

            for chunk_no, draft in enumerate(chunks, start=1):
                text_hash = hashlib.sha256(draft.text.encode("utf-8")).hexdigest()
                append_document_chunk(
                    db,
                    version_id=version.id,
                    chunk_no=chunk_no,
                    text=draft.text,
                    content_hash=text_hash,
                    token_count=estimate_token_count(draft.text),
                    page_no=draft.page_no,
                    paragraph_no=draft.paragraph_no,
                    workspace_id=workspace_id,
                )
            mark_version_ready(db, version_id=version.id, workspace_id=workspace_id)

            # 同步索引到向量库
            import json

            from app.background import _execute_document_index
            from app.db import CrawlJob

            job = CrawlJob(
                id=0,
                source=source_type,
                keyword=filename[:255],
                workspace_id=workspace_id,
                type="document_index",
                status="running",
                cursor=json.dumps(
                    {
                        "document_id": document.id,
                        "version_id": version.id,
                        "storage_uri": storage_uri,
                    },
                    ensure_ascii=False,
                ),
            )
            _execute_document_index(db, job)
            db.commit()
            print(f"  indexed {len(chunks)} chunks -> vector store")
    finally:
        db.close()
    reset_executor()
    return 0


def cmd_enqueue_crawl(args: list[str]) -> int:
    _ensure_schema()
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "products.html"
    db = SessionLocal()
    try:
        workspace_id = _resolve_cli_workspace(db, args)
        job = submit_crawl_fixture(db, fixture_path=fixture, workspace_id=workspace_id)
        print(f"已入队: job_id={job.id}, status={job.status}")
        wait_for_jobs(timeout=10)
        db.expire_all()
        final = db.get(CrawlJob, job.id)
        if final is None:
            print(f"任务查询失败: job_id={job.id}")
            return 1
        print(f"任务完成: status={final.status}, attempt={final.attempt}")
        return 0 if final.status == "succeeded" else 1
    finally:
        db.close()
    return 0


def cmd_query(args: list[str]) -> int:
    if not args:
        print("usage: python -m app.cli query <text>")
        return 2
    question = " ".join(args)
    answerer = get_answerer()
    result = answerer.answer(question)
    if result.no_answer:
        print(f"[no_answer] reason={result.reason}")
        return 0
    print(f"answer: {result.answer}")
    for hit in result.hits[:3]:
        print(
            f"  - chunk {hit.chunk_id} doc {hit.document_id} v{hit.document_version_id} "
            f"score={hit.score:.3f}: {hit.snippet[:80]}"
        )
    return 0


def cmd_external_list_connectors(args: list[str]) -> int:
    from app.connectors import connector_capabilities

    print(__import__("json").dumps(connector_capabilities(), ensure_ascii=False, indent=2))
    return 0


def cmd_external_preview(args: list[str]) -> int:
    from app.connectors import load_records

    content = Path(args[args.index("--file") + 1]).read_bytes()
    records = load_records(content, platform=args[args.index("--platform") + 1], source_mode=args[args.index("--mode") + 1] if "--mode" in args else "json")
    print(__import__("json").dumps([record.as_dict() for record in records], ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def cmd_external_import(args: list[str]) -> int:
    from app.connectors import load_records
    from app.external_sync import ingest_inventory

    _ensure_schema()
    platform = args[args.index("--platform") + 1]
    path = Path(args[args.index("--file") + 1])
    mode = args[args.index("--mode") + 1] if "--mode" in args else "json"
    db = SessionLocal()
    try:
        workspace_id = _resolve_cli_workspace(db, args)
        records = load_records(path.read_bytes(), platform=platform, source_mode=mode)
        result = ingest_inventory(db, records, workspace_id=workspace_id)
        print(__import__("json").dumps({"platform": platform, "inserted": result.inserted, "no_op": result.no_op, "conflict": result.conflict, "total": result.total, "snapshot_ids": result.snapshot_ids or [], "simulated": True, "live_enabled": False}, ensure_ascii=False, sort_keys=True))
    finally:
        db.close()
    return 0


def cmd_init_admin(args: list[str]) -> int:
    """创建首个管理员账户；仅显式调用，不自动创建生产账户。"""
    import getpass
    from sqlalchemy import select
    from app.db import UserAccount, Workspace, WorkspaceMembership
    from app.employee_auth import hash_password

    def option(name: str, default: str = "") -> str:
        if name in args:
            index = args.index(name)
            if index + 1 >= len(args):
                raise ValueError(f"缺少参数: {name}")
            return args[index + 1]
        return default

    try:
        login = (option("--login") or input("管理员登录名: ").strip()).strip()
        display_name = option("--display-name", login) or input("显示名称: ").strip()
        password = option("--password") or getpass.getpass("管理员密码: ")
        tenant_key = option("--tenant-key", get_settings().auth_bootstrap_tenant_key).strip()
        workspace_name = option("--workspace-name", get_settings().auth_bootstrap_workspace_name).strip()
    except (EOFError, ValueError) as exc:
        print(f"初始化失败: {exc}")
        return 2
    if len(login) < 1 or len(password) < 8 or not tenant_key:
        print("初始化失败: 登录名、工作空间不能为空，密码至少 8 位")
        return 2
    _ensure_schema()
    db = SessionLocal()
    try:
        if db.scalar(select(UserAccount).where(UserAccount.login == login)) is not None:
            print("初始化失败: 账户已存在")
            return 1
        workspace = db.scalar(select(Workspace).where(Workspace.tenant_key == tenant_key))
        if workspace is None:
            workspace = Workspace(tenant_key=tenant_key, name=workspace_name or tenant_key)
            db.add(workspace)
            db.flush()
        user = UserAccount(login=login, display_name=display_name or login, password_hash=hash_password(password))
        db.add(user)
        db.flush()
        db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        db.commit()
        print(f"管理员创建成功: {login}，工作空间: {workspace.name}")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"初始化失败: {exc}")
        return 1
    finally:
        db.close()


def cmd_seed_demo(args: list[str]) -> int:
    """在隔离的 demo workspace 创建虚构补货演示数据。"""
    from datetime import date
    import json as _json

    from app.config import get_settings
    if not get_settings().demo_mode_enabled:
        print("seed-demo 失败: DEMO_MODE_ENABLED=true 才允许执行")
        return 2
    workspace_key = _option(args, "--workspace")
    if not workspace_key or not workspace_key.startswith("demo-"):
        print("seed-demo 失败: --workspace 必须使用 demo- 前缀")
        return 2
    database_url = get_settings().database_url
    if not database_url.startswith("sqlite:"):
        print("seed-demo 失败: 仅允许 SQLite 演示数据库")
        return 2
    _ensure_schema()
    db = SessionLocal()
    try:
        from app.demo_seed import seed_demo_workspace
        as_of_text = _option(args, "--as-of-date", "2026-09-07")
        as_of = date.fromisoformat(as_of_text)
        result = seed_demo_workspace(db, tenant_key=workspace_key, workspace_name=_option(args, "--workspace-name", "[虚构] 电商演示商家"), as_of_date=as_of)
        output = result.as_dict()
        print(_json.dumps(output, ensure_ascii=False, indent=2, default=str) if "--json" in args else f"seed-demo 完成: workspace={result.tenant_key}, products={len(result.product_ids)}, suggestions={len(result.suggestion_ids)}, drafts={len(result.purchase_request_ids)}")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"seed-demo 失败: {exc}")
        return 1
    finally:
        db.close()


def cmd_status(args: list[str]) -> int:
    """打印运行时配置与数据库 / 向量库规模。"""
    from sqlalchemy import func, inspect, select
    from app.db import Document, DocumentChunk, Product, engine as db_engine

    insp = inspect(db_engine)
    if not {"products", "documents"}.issubset(set(insp.get_table_names())):
        print("schema 未初始化，请先执行 python -m app.cli init-db")
        return 1
    settings = get_settings()
    db = SessionLocal()
    try:
        doc_count = db.scalar(select(func.count(Document.id))) or 0
        chunk_count = db.scalar(select(func.count(DocumentChunk.id))) or 0
        product_count = db.scalar(select(func.count(Product.id))) or 0
        retriever = get_retriever()
        vector_count = len(retriever.store.records)
        print(f"app_name           = {settings.app_name}")
        print(f"database_url       = {safe_database_url(settings.database_url)}")
        print(f"worker_concurrency = {settings.worker_concurrency}")
        print(f"upload_max_bytes   = {settings.upload_max_bytes}")
        print(f"embedder_dim       = {settings.embedder_dim}")
        print(f"rag_top_k          = {settings.rag_top_k}")
        print(f"rag_min_score      = {settings.rag_min_score}")
        print(f"vector_store_path  = {settings.vector_store_path}")
        print("----")
        print(f"products           = {product_count}")
        print(f"documents          = {doc_count}")
        print(f"chunks             = {chunk_count}")
        print(f"vector_records     = {vector_count}")
    finally:
        db.close()
    return 0


COMMANDS = {
    "init-db": cmd_init_db,
    "init-admin": cmd_init_admin,
    "seed-docs": cmd_seed_docs,
    "enqueue-crawl": cmd_enqueue_crawl,
    "query": cmd_query,
    "seed-demo": cmd_seed_demo,
    "status": cmd_status,
    "external-list-connectors": cmd_external_list_connectors,
    "external-preview": cmd_external_preview,
    "external-import": cmd_external_import,
}


COMMAND_HELP = {
    "init-db": "初始化数据库表（幂等，重复执行安全）",
    "init-admin": "创建首个管理员账户（显式执行）",
    "seed-docs": "向 uploads/ 写入示例 PDF + DOCX 并同步解析、索引（可用 --workspace 指定商家）",
    "enqueue-crawl": "把 fixtures/products.html 入队到 ThreadPoolExecutor，等待完成（可用 --workspace 指定商家）",
    "query": "用法: python -m app.cli query <text...>   从向量库检索并返回 answer + 引用",
    "seed-demo": "创建隔离的虚构商品、库存、销量、补货、告警和采购草稿演示数据",
    "status": "打印当前配置与数据库 / 向量库规模",
    "external-list-connectors": "列出离线平台连接器能力",
    "external-preview": "预览并标准化外部库存文件（不落库）",
    "external-import": "导入外部库存快照（不修改内部库存，可用 --workspace 指定商家）",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("电商工作台 CLI")
        print("=" * 40)
        print("用法: python -m app.cli <command> [args]")
        print()
        print("可用命令：")
        for name in sorted(COMMANDS):
            print(f"  {name:<14} {COMMAND_HELP.get(name, '')}")
        print()
        print("示例:")
        print("  python -m app.cli init-db")
        print("  python -m app.cli seed-docs")
        print('  python -m app.cli query "green tea"')
        print("  python -m app.cli status")
        return 0
    cmd, rest = argv[0], argv[1:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"未知命令: {cmd}")
        print(f"可用命令: {', '.join(sorted(COMMANDS))}")
        print("运行 `python -m app.cli --help` 查看详情。")
        return 2
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())