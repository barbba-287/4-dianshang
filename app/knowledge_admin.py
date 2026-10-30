"""P3-S3 知识文档管理服务层：谱系解析、发布/撤回、列表聚合。

**零新表、零 migration、零 `app/db.py` 改动**（用户裁定，见任务文件 §5 禁区 1）：

- **发布位** = `DocumentVersion.published_at`（既有列，发布非空即已发布；撤回即置回 NULL）。
- **谱系** = `Document.filename`（首传值即谱系键；同文件名新内容在该 Document 下开新版本）。
- **归因** = `CrawlJob.payload_json["actor"]`（既有 Text 列；无 created_by 列属既有事实，
  本片不解锁、不伪造）。
- **状态词典**零扩展：沿用 `parsing / ready / failed`，"已发布" 由 `published_at` 派生。

本模块只做领域语义（不 import fastapi），错误一律抛 `app.errors.DomainError`，
HTTP 映射由端点显式给出（不依赖任何 endswith 式推断）。

模型与 repository 一律**按模块取属性**（`db_mod.Document`），不在 import 期绑定类对象：
测试夹具会 `importlib.reload(app.db)`，import 期绑定的旧类对象会与新 engine 的
metadata 脱节（同表两套映射）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as db_mod
from app import repository as repo_mod
from app.errors import DomainError
from app.identity import ROLE_ADMIN, ROLE_OPERATIONS, Principal
from app.storage import save_upload
from app.versioning import content_sha256


# 发布/撤回的角色闸（Q-S3-2 裁定）：客服可上传（提供子弹），但发布权收紧。
PUBLISH_ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_OPERATIONS)


# ---------- 权限 ----------


def require_publish_rights(principal: Principal) -> None:
    """发布/撤回的角色闸（写死在本 service 的权限参数里，Q-S3-2）。

    - **员工会话**（`auth_type == "session"`）：必须命中 `PUBLISH_ROLES`，
      否则 403 `FORBIDDEN`；客服（`customer_service`）有 `knowledge.write`
      能上传，但**不能**发布/撤回。
    - **服务通道**（`demo` / `api_key`）：这两个通道本身没有角色体系
      （demo 模式本就放开全部端点、api_key 走 scope 判定），按服务通道放行；
      真实部署（`EMPLOYEE_AUTH_ENABLED=true`）走员工会话分支，角色闸严格生效。

    说明：角色闸**不**放在 `require_principal(roles=...)`——该分支对 demo/api_key
    主体一律 403（"演示模式不可访问员工管理功能"），会把服务通道与演示环境打死。
    """
    if principal.has_role(*PUBLISH_ROLES):
        return
    if principal.auth_type in ("demo", "api_key"):
        return
    raise DomainError(
        code="FORBIDDEN",
        message="只有管理员或运营可以发布/撤回知识版本",
        http_status=403,
    )


# ---------- 谱系（D1） ----------


def resolve_lineage(
    db: Session,
    *,
    workspace_id: int | None,
    source_type: str,
    title: str,
    filename: str,
    sha256_hex: str,
) -> tuple["db_mod.Document", bool]:
    """D1 三步解析：sha 命中 → filename 命中 → 新建 Document。

    1. **sha 命中**（`(workspace_id, sha256)`）→ 复用该 Document（内容优先于文件名：
       同内容=同知识，避免向量库双份）；
    2. **filename 命中**（`(workspace_id, filename)`）→ 复用该 Document（版本链谱系）；
    3. 都未命中 → 新建 Document。

    已知边界（任务文件 D1，登记不修）：
    - Document 行的 `sha256` / `filename` 保留**首传值**（仅作寻址键）；
    - 同内容先后以不同文件名上传 → 收敛到首个谱系；
    - 谱系 D 的 v2 内容 C 再以新文件名上传 → C 会开出新 Document（C 同时存在于两个谱系，
      不同 version_id，向量记录 id 不冲突）。

    返回 `(document, created)`。
    """
    existing = db.scalar(
        select(db_mod.Document)
        .where(
            db_mod.Document.workspace_id == workspace_id,
            db_mod.Document.sha256 == sha256_hex,
        )
        .order_by(db_mod.Document.id)
    )
    if existing is not None:
        return existing, False

    lineage = db.scalar(
        select(db_mod.Document)
        .where(
            db_mod.Document.workspace_id == workspace_id,
            db_mod.Document.filename == filename,
        )
        .order_by(db_mod.Document.id)
    )
    if lineage is not None:
        return lineage, False

    document = db_mod.Document(
        workspace_id=workspace_id,
        source_type=source_type,
        title=title,
        filename=filename,
        sha256=sha256_hex,
    )
    db.add(document)
    db.flush()
    return document, True


def get_document(
    db: Session, *, document_id: int, workspace_id: int | None
) -> "db_mod.Document":
    document = db.scalar(
        select(db_mod.Document).where(
            db_mod.Document.id == document_id,
            db_mod.Document.workspace_id == workspace_id,
        )
    )
    if document is None:
        raise DomainError(
            code="DOCUMENT_NOT_FOUND", message="文档不存在", http_status=404
        )
    return document


def get_version(
    db: Session, *, document_id: int, version_id: int, workspace_id: int | None
) -> "db_mod.DocumentVersion":
    """取版本；版本不属于该 document 或跨租户一律 404（不泄露存在性）。"""
    version = db.scalar(
        select(db_mod.DocumentVersion).where(
            db_mod.DocumentVersion.id == version_id,
            db_mod.DocumentVersion.document_id == document_id,
            db_mod.DocumentVersion.workspace_id == workspace_id,
        )
    )
    if version is None:
        raise DomainError(
            code="DOCUMENT_NOT_FOUND", message="文档版本不存在", http_status=404
        )
    return version


def list_versions(db: Session, *, document_id: int, workspace_id: int | None):
    return db.scalars(
        select(db_mod.DocumentVersion)
        .where(
            db_mod.DocumentVersion.document_id == document_id,
            db_mod.DocumentVersion.workspace_id == workspace_id,
        )
        .order_by(db_mod.DocumentVersion.version_no.desc())
    ).all()


def is_superseded(version, *, published_siblings: set[int]) -> bool:
    """D2 派生标记（零 migration，响应层计算，不落库）。

    被降级版本 = `published_at IS NULL` 且同 document 存在 `published_at` 非空的兄弟版本。
    """
    if version.published_at is not None:
        return False
    return bool(published_siblings - {version.id})


def published_sibling_ids(versions) -> set[int]:
    return {version.id for version in versions if version.published_at is not None}


# ---------- 上传（谱系 + 幂等） ----------


def _job_cursor(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def _pseudo_import_job(
    db: Session,
    *,
    workspace_id: int | None,
    source_type: str,
    filename: str,
    document_id: int,
    version_id: int,
    storage_uri: str,
) -> "db_mod.CrawlJob":
    """幂等重传用的伪 job：与现状逐字节一致（只写 cursor，不写 payload_json）。

    幂等路径**没有新的解析工作**，解析事件仍由真实 `document_import` job 承载。
    """
    job = db_mod.CrawlJob(
        workspace_id=workspace_id,
        source=source_type,
        keyword=filename[:255],
        type="document_import",
        status="succeeded",
        cursor=_job_cursor(
            {
                "document_id": document_id,
                "version_id": version_id,
                "storage_uri": storage_uri,
            }
        ),
        run_id=version_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def ingest_document(
    db: Session,
    *,
    principal: Principal,
    source_type: str,
    title: str | None,
    filename: str | None,
    content: bytes,
) -> tuple["db_mod.Document", "db_mod.DocumentVersion", "db_mod.CrawlJob", bool]:
    """上传落库：内容寻址存盘 → 谱系解析（D1）→ 版本落库 → 入队解析。

    返回 `(document, version, job, version_created)`。

    幂等语义（D1 第 1 步）：sha 命中既有版本时**不重复入队**，返回一个 succeeded 的
    伪 job（与现状逐字节一致）。
    """
    workspace_id = principal.workspace_id
    sha = content_sha256(content)
    safe_filename = filename or sha
    safe_title = title or safe_filename or sha

    # 内容寻址存盘：路径只由 source_type + sha256 构造，用户输入不进路径。
    storage_uri = save_upload(
        source_type=source_type, sha256_hex=sha, ext=source_type, content=content
    )

    document, _doc_created = resolve_lineage(
        db,
        workspace_id=workspace_id,
        source_type=source_type,
        title=safe_title,
        filename=safe_filename,
        sha256_hex=sha,
    )
    return _store_version_and_enqueue(
        db,
        principal=principal,
        document=document,
        source_type=source_type,
        sha=sha,
        storage_uri=storage_uri,
        content=content,
    )


def ingest_version_into_document(
    db: Session,
    *,
    principal: Principal,
    document: "db_mod.Document",
    source_type: str,
    title: str | None,
    filename: str | None,
    content: bytes,
) -> tuple["db_mod.Document", "db_mod.DocumentVersion", "db_mod.CrawlJob", bool]:
    """显式往**指定谱系**下加一个版本（端点 #5）。

    - 前置：`filename` 必须与该 Document 的谱系键（`Document.filename`）一致，
      否则 409 `FILENAME_LINEAGE_MISMATCH`；
    - 幂等：同 sha 已存在于该 Document → 不重复入队，返回既有版本（伪 job）。
    """
    workspace_id = principal.workspace_id
    sha = content_sha256(content)
    safe_filename = filename or sha
    if safe_filename != (document.filename or ""):
        raise DomainError(
            code="FILENAME_LINEAGE_MISMATCH",
            message="文件名与该文档谱系不一致，无法作为新版本入库",
            http_status=409,
        )

    storage_uri = save_upload(
        source_type=source_type, sha256_hex=sha, ext=source_type, content=content
    )
    return _store_version_and_enqueue(
        db,
        principal=principal,
        document=document,
        source_type=source_type,
        sha=sha,
        storage_uri=storage_uri,
        content=content,
    )


def _store_version_and_enqueue(
    db: Session,
    *,
    principal: Principal,
    document: "db_mod.Document",
    source_type: str,
    sha: str,
    storage_uri: str,
    content: bytes,
) -> tuple["db_mod.Document", "db_mod.DocumentVersion", "db_mod.CrawlJob", bool]:
    from app.background import submit_document_import

    workspace_id = principal.workspace_id
    version, version_created = repo_mod.upsert_document_version(
        db,
        document=document,
        sha256_hex=sha,
        size_bytes=len(content),
        storage_uri=storage_uri,
        workspace_id=workspace_id,
    )
    db.commit()

    if version_created:
        job = submit_document_import(
            db,
            document_id=document.id,
            version_id=version.id,
            storage_uri=storage_uri,
            filename=document.filename,
            source_type=source_type,
            workspace_id=workspace_id,
            actor=principal.subject,
        )
    else:
        job = _pseudo_import_job(
            db,
            workspace_id=workspace_id,
            source_type=source_type,
            filename=document.filename,
            document_id=document.id,
            version_id=version.id,
            storage_uri=storage_uri,
        )
    return document, version, job, version_created


# ---------- 发布 / 撤回（D3） ----------


def publish_version(
    db: Session,
    *,
    principal: Principal,
    document_id: int,
    version_id: int,
) -> dict:
    """发布一个版本：单事务换发布位 → 同步删旧向量 → 入队索引。

    - 前置：`status == "ready"`（parsing/failed → 409 `INVALID_DOCUMENT_STATE`）；
    - 单发布位：同 document 的兄弟版本 `published_at` 一并清空（自动降级）；
    - **换版发布同步删除旧向量**（D3 红线）：同步 `delete_by_version(旧 version_id)`，
      异步 `document_unindex` 仅作补偿通道（同步删除抛异常时才入队）；
    - 索引入队 `document_index`（D4 闸在执行时重查 DB 的 `published_at`）。
    """
    from app.background import submit_chunk_index, submit_document_unindex
    from app.rag_runtime import get_vector_store

    workspace_id = principal.workspace_id
    document = get_document(db, document_id=document_id, workspace_id=workspace_id)
    version = get_version(
        db, document_id=document.id, version_id=version_id, workspace_id=workspace_id
    )
    if version.status != "ready":
        raise DomainError(
            code="INVALID_DOCUMENT_STATE",
            message=f"版本状态为 {version.status}，无法发布",
            http_status=409,
        )

    siblings = list_versions(db, document_id=document.id, workspace_id=workspace_id)
    superseded = [
        item for item in siblings if item.published_at is not None and item.id != version.id
    ]

    # 单事务：清兄弟发布位 → 置本版本发布位。
    now = datetime.utcnow()
    for item in superseded:
        item.published_at = None
    version.published_at = now
    db.commit()

    # 换版发布同步删旧向量：杜绝"旧知识仍在检索池"的知识污染窗口。
    store = get_vector_store()
    for item in superseded:
        try:
            store.delete_by_version(item.id)
        except Exception:  # noqa: BLE001 - 同步删除失败降级为补偿通道
            submit_document_unindex(
                db,
                document_id=document.id,
                version_id=item.id,
                workspace_id=workspace_id,
                actor=principal.subject,
            )
    db.commit()

    job = submit_chunk_index(
        db,
        document_id=document.id,
        version_id=version.id,
        workspace_id=workspace_id,
        actor=principal.subject,
    )

    db.refresh(version)
    return {
        "document": document,
        "version": version,
        "job_id": job.id,
        "superseded_version_ids": [item.id for item in superseded],
    }


def unpublish_version(
    db: Session,
    *,
    principal: Principal,
    document_id: int,
    version_id: int,
) -> dict:
    """撤回一个已发布版本：置空发布位 → **同步**删除该版本向量。

    - 前置：`published_at` 非空（否则 409 `VERSION_NOT_PUBLISHED`）；
    - 同步删除是主路径（撤回必须立即生效，安全方向宁可同步）；
    - 同步删除抛异常 → 入队 `document_unindex` 补偿；
    - 归因：`actor` 随补偿/留痕 job 落 `CrawlJob.payload_json`。
    """
    from app.background import submit_document_unindex
    from app.rag_runtime import get_vector_store

    workspace_id = principal.workspace_id
    document = get_document(db, document_id=document_id, workspace_id=workspace_id)
    version = get_version(
        db, document_id=document.id, version_id=version_id, workspace_id=workspace_id
    )
    if version.published_at is None:
        raise DomainError(
            code="VERSION_NOT_PUBLISHED",
            message="该版本未发布，无需撤回",
            http_status=409,
        )

    version.published_at = None
    db.commit()

    removed = 0
    try:
        removed = get_vector_store().delete_by_version(version.id)
    except Exception:  # noqa: BLE001 - 同步删除失败降级为补偿通道
        submit_document_unindex(
            db,
            document_id=document.id,
            version_id=version.id,
            workspace_id=workspace_id,
            actor=principal.subject,
        )
        db.commit()

    db.refresh(version)
    return {
        "document": document,
        "version": version,
        "removed_vectors": removed,
    }


# ---------- 列表 / 详情聚合（D7） ----------


def _version_summary(db: Session, version, *, published_ids: set[int]) -> dict:
    return {
        "id": version.id,
        "document_id": version.document_id,
        "version_no": version.version_no,
        "sha256": version.sha256,
        "size_bytes": version.size_bytes,
        "status": version.status,
        "created_at": version.created_at,
        "published_at": version.published_at,
        "superseded": is_superseded(version, published_siblings=published_ids),
        "chunk_count": repo_mod.count_chunks(
            db, version_id=version.id, workspace_id=version.workspace_id
        ),
    }


def list_documents(
    db: Session,
    *,
    workspace_id: int | None,
    filename: str | None = None,
    status: str | None = None,
    published: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """分页列表 + 过滤 + 每行版本摘要。

    过滤语义（显式约定，避免歧义）：
    - `filename`：对 `Document.filename` 做模糊匹配（大小写不敏感）；
    - `status`：该文档**最新版本**的状态；
    - `published`：该文档是否存在已发布版本（true=有，false=无，缺省=全部）。
    """
    documents = db.scalars(
        select(db_mod.Document)
        .where(db_mod.Document.workspace_id == workspace_id)
        .order_by(db_mod.Document.id.desc())
    ).all()

    keyword = (filename or "").strip().lower()
    rows: list[dict] = []
    for document in documents:
        versions = list_versions(
            db, document_id=document.id, workspace_id=workspace_id
        )
        if keyword and keyword not in (document.filename or "").lower():
            continue
        latest = versions[0] if versions else None
        published_ids = published_sibling_ids(versions)
        published_version = next(
            (item for item in versions if item.published_at is not None), None
        )
        if status is not None and (latest is None or latest.status != status):
            continue
        if published is True and published_version is None:
            continue
        if published is False and published_version is not None:
            continue
        superseded_nos = [
            item.version_no
            for item in versions
            if is_superseded(item, published_siblings=published_ids)
        ]
        rows.append(
            {
                "id": document.id,
                "filename": document.filename,
                "title": document.title,
                "source_type": document.source_type,
                "sha256": document.sha256,
                "version_count": len(versions),
                "latest_version_no": latest.version_no if latest else None,
                "latest_version_id": latest.id if latest else None,
                "latest_status": latest.status if latest else None,
                "published_version_no": (
                    published_version.version_no if published_version else None
                ),
                "published_version_id": (
                    published_version.id if published_version else None
                ),
                "superseded_version_nos": superseded_nos,
                "created_at": document.created_at,
            }
        )

    total = len(rows)
    start = max(page - 1, 0) * page_size
    return {
        "items": rows[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def document_detail(db: Session, *, document_id: int, workspace_id: int | None) -> dict:
    """单文档详情：谱系信息 + 版本链（含 superseded 派生标记）。"""
    document = get_document(db, document_id=document_id, workspace_id=workspace_id)
    versions = list_versions(db, document_id=document.id, workspace_id=workspace_id)
    published_ids = published_sibling_ids(versions)
    published_version = next(
        (item for item in versions if item.published_at is not None), None
    )
    return {
        "id": document.id,
        "filename": document.filename,
        "title": document.title,
        "source_type": document.source_type,
        "sha256": document.sha256,
        "version_count": len(versions),
        "published_version_no": (
            published_version.version_no if published_version else None
        ),
        "published_version_id": (
            published_version.id if published_version else None
        ),
        "versions": [
            _version_summary(db, item, published_ids=published_ids) for item in versions
        ],
        "created_at": document.created_at,
    }
