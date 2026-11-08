"""P6-S3/P6-S4 通用数据导出与异常追溯测试（E-1..E-18）。

夹具模式照 tests/test_p6s1_csv_import.py（tmp_path 隔离库 + operations 会话）。
"""
from __future__ import annotations

import importlib
import logging
from datetime import datetime

from fastapi.testclient import TestClient

GOOD_CSV = (
    "external_sku,account_ref,store_ref,available_qty,as_of\n"
    "S-1,a,s,7,2026-11-01T10:00:00Z\n"
    "S-2,a,s,3,2026-11-01T10:00:00Z\n"
)
# 第 2 行 available_qty=-5 校验失败，其余两行合法（供二分隔离区分好坏行）。
DIRTY_CSV = (
    "external_sku,account_ref,store_ref,available_qty,as_of\n"
    "S-1,a,s,7,2026-11-01T10:00:00Z\n"
    "S-2,a,s,-5,2026-11-01T10:00:00Z\n"
    "S-3,a,s,4,2026-11-01T10:00:00Z\n"
)
BOM = b"\xef\xbb\xbf"


def _client(tmp_path, monkeypatch, *, role: str = "operations", login: str = "operator"):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'export.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="export-test", name="导出测试")
        db.add(workspace)
        db.flush()
        user = db_mod.UserAccount(
            login="operator", password_hash=hash_password("operator-password"), display_name="运营",
        )
        db.add(user)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        if role != "operations":
            other = db_mod.UserAccount(
                login="helper", password_hash=hash_password("helper-password"), display_name="协助",
            )
            db.add(other)
            db.flush()
            db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=other.id, role=role))
        db.commit()
    client = TestClient(main_mod.app)
    login_name = "helper" if role != "operations" else "operator"
    password = "helper-password" if role != "operations" else "operator-password"
    response = client.post(
        "/login", data={"login": login_name, "password": password, "next": "/"}, follow_redirects=False,
    )
    assert response.status_code == 303
    client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
    return client, db_mod


def _upload(client, *, path="/api/external/imports", content=GOOD_CSV.encode(), allow_skipped=False):
    data = {"platform": "taobao", "kind": "inventory"}
    if allow_skipped:
        data["allow_skipped"] = "true"
    return client.post(path, files={"file": ("report.csv", content, "text/csv")}, data=data)


def _workspace_id(db_mod) -> int:
    with db_mod.SessionLocal() as db:
        return db.query(db_mod.Workspace).filter(db_mod.Workspace.tenant_key == "export-test").one().id


def _export(client, run_id: str, view: str, fmt: str = "csv"):
    """导出请求；P6-S4 起端点默认 xlsx，故 CSV 相关断言显式带 format=csv。"""
    suffix = f"&format={fmt}" if fmt else ""
    return client.post(f"/api/external/imports/{run_id}/export?view={view}{suffix}")


def _lines(response) -> list[str]:
    return response.content.decode("utf-8-sig").splitlines()


def test_e1_raw_export_with_headers(tmp_path, monkeypatch):
    client, _db = _client(tmp_path, monkeypatch)
    commit = _upload(client, content=GOOD_CSV.encode())
    assert commit.status_code == 200, commit.text
    run_id = commit.json()["sync_run_id"]
    response = _export(client, run_id, "raw")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    lines = _lines(response)
    assert lines[0].split(",") == [
        "平台", "账号", "店铺", "仓库", "商品编码", "可售库存", "占用库存",
        "在途库存", "快照时间", "入库状态", "来源模式", "接收时间",
    ]
    assert len(lines) == 3  # 表头 + 2 行
    assert "S-1" in lines[1] and "S-2" in lines[2]


def test_e2_rejected_export_appends_reason_column(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    commit = _upload(client, content=DIRTY_CSV.encode(), allow_skipped=True)
    assert commit.status_code == 200, commit.text
    assert commit.json()["rejected_count"] == 1
    run_id = commit.json()["sync_run_id"]
    response = _export(client, run_id, "rejected")
    assert response.status_code == 200, response.text
    lines = _lines(response)
    # 商家原表列序原样保留 + 末尾追加「系统拦截原因」
    assert lines[0].split(",") == [
        "external_sku", "account_ref", "store_ref", "available_qty", "as_of", "系统拦截原因",
    ]
    assert len(lines) == 2
    cells = lines[1].split(",")
    assert cells[0] == "S-2" and cells[3] == "-5"
    assert "第 2 行" in cells[-1]
    assert db_mod  # 夹具句柄保留（后续断言用同一个 db_mod）


def test_e3_bom_prefix_both_views(tmp_path, monkeypatch):
    client, _db = _client(tmp_path, monkeypatch)
    commit = _upload(client, content=DIRTY_CSV.encode(), allow_skipped=True)
    run_id = commit.json()["sync_run_id"]
    for view in ("raw", "rejected"):
        response = _export(client, run_id, view)
        assert response.status_code == 200, response.text
        assert response.content.startswith(BOM), f"{view} 缺少 UTF-8 BOM"


def test_e4_permission_and_workspace_isolation(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    commit = _upload(client, content=GOOD_CSV.encode())
    run_id = commit.json()["sync_run_id"]

    # 跨 workspace：run 存在但不属于当前 workspace → 404（不泄露存在性）
    with db_mod.SessionLocal() as db:
        other = db_mod.Workspace(tenant_key="other", name="另一个空间")
        db.add(other)
        db.flush()
        db.add(db_mod.ExternalSyncRun(
            workspace_id=other.id, run_id="run-other-ws", platform="taobao",
            sync_type="inventory", source_mode="csv", status="succeeded", started_at=datetime.utcnow(),
        ))
        db.commit()
    cross = _export(client, "run-other-ws", "raw")
    assert cross.status_code == 404, cross.text
    assert cross.json()["detail"]["code"] == "SYNC_RUN_NOT_FOUND"
    assert _export(client, "run-does-not-exist", "raw").status_code == 404

    # 权限边界：warehouse 角色无 catalog.read → 403
    from app.db import UserAccount, WorkspaceMembership
    with db_mod.SessionLocal() as db:
        user = db.query(UserAccount).filter(UserAccount.login == "operator").one()
        membership = db.query(WorkspaceMembership).filter(WorkspaceMembership.user_id == user.id).one()
        membership.role = "warehouse"
        db.commit()
    denied = _export(client, run_id, "raw")
    assert denied.status_code == 403, denied.text


def test_e5_non_csv_run_rejected(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    workspace_id = _workspace_id(db_mod)
    with db_mod.SessionLocal() as db:
        db.add(db_mod.ExternalSyncRun(
            workspace_id=workspace_id, run_id="run-live", platform="shopify",
            sync_type="inventory", source_mode="shopify_live", status="succeeded",
            started_at=datetime.utcnow(),
        ))
        db.commit()
    response = _export(client, "run-live", "raw")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "EXPORT_NOT_CSV_IMPORT"
    bad_view = _export(client, "run-live", "everything")
    assert bad_view.status_code == 422
    assert bad_view.json()["detail"]["code"] == "EXPORT_VIEW_INVALID"


def test_e6_actor_attribution_logged(tmp_path, monkeypatch):
    """actor 归因落结构化日志（Q3 裁定：不建审计表）。

    自带 handler 挂 app.main logger，不依赖 caplog——全量回归时其他模块
    可能改动 root 日志配置，caplog 会随之失灵（本次 323s 全量中实证）。
    """
    import app.main as main_mod

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture(level=logging.INFO)
    previous_level = main_mod.logger.level
    main_mod.logger.addHandler(handler)
    main_mod.logger.setLevel(logging.INFO)
    try:
        client, _db = _client(tmp_path, monkeypatch)
        run_id = _upload(client, content=GOOD_CSV.encode()).json()["sync_run_id"]
        assert _export(client, run_id, "raw").status_code == 200
    finally:
        main_mod.logger.removeHandler(handler)
        main_mod.logger.setLevel(previous_level)

    text = "\n".join(records)
    assert "报表导出" in text
    assert "actor=operator" in text
    assert f"run_id={run_id}" in text
    assert "view=raw" in text


def test_e7_strict_mode_blocks_without_skipping(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    response = _upload(client, content=DIRTY_CSV.encode())
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "EXTERNAL_ROWS_REJECTED"
    assert "第 2 行" in detail["message"]
    from app.db import ExternalImportRejection, ExternalSyncRun
    with db_mod.SessionLocal() as db:
        assert db.query(ExternalSyncRun).count() == 0
        assert db.query(ExternalImportRejection).count() == 0


def test_e8_allow_skipped_persists_rejections(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    commit = _upload(client, content=DIRTY_CSV.encode(), allow_skipped=True)
    assert commit.status_code == 200, commit.text
    body = commit.json()
    assert body["inserted"] == 2
    assert body["rejected_count"] == 1
    from app.db import ExternalImportRejection
    with db_mod.SessionLocal() as db:
        rows = db.query(ExternalImportRejection).all()
        assert len(rows) == 1
        assert rows[0].row_number == 2
        assert rows[0].kind == "inventory"
        assert '"external_sku": "S-2"' in rows[0].raw_data
    runs = client.get("/api/external/sync/runs").json()
    target = [run for run in runs if run["run_id"] == body["sync_run_id"]]
    assert target and target[0]["rejected_count"] == 1


def test_e9_reupload_stays_per_batch(tmp_path, monkeypatch):
    """幂等不受影响：数据行 no_op；异常明细按批次隔离，各自只含本批脏行。"""
    client, db_mod = _client(tmp_path, monkeypatch)
    first = _upload(client, content=DIRTY_CSV.encode(), allow_skipped=True).json()
    second = _upload(client, content=DIRTY_CSV.encode(), allow_skipped=True).json()
    assert first["sync_run_id"] != second["sync_run_id"]
    assert second["inserted"] == 0 and second["no_op"] == 2
    assert second["rejected_count"] == 1
    from app.db import ExternalImportRejection
    with db_mod.SessionLocal() as db:
        assert db.query(ExternalImportRejection).count() == 2
        assert db.query(ExternalImportRejection).filter(
            ExternalImportRejection.run_id == second["sync_run_id"]
        ).count() == 1
    for run_id in (first["sync_run_id"], second["sync_run_id"]):
        assert len(_lines(_export(client, run_id, "rejected"))) == 2


def test_e10_preview_reports_rejected_rows(tmp_path, monkeypatch):
    client, _db = _client(tmp_path, monkeypatch)
    preview = _upload(client, path="/api/external/imports/preview", content=DIRTY_CSV.encode())
    assert preview.status_code == 200, preview.text
    body = preview.json()
    # P6-S4：total 改为「数据行总数（含坏行）」——脏行多时旧口径会显示「共 2 行」而文件有 3 行。
    assert body["total"] == 3
    assert body["rejected_total"] == 1
    assert body["rejected_rows"][0]["row_number"] == 2
    assert body["rejected_rows"][0]["raw_data"]["external_sku"] == "S-2"
    assert "available_qty" in body["rejected_rows"][0]["reason_message"]


def test_e11_no_heavy_dependencies():
    """P6-S4 口径：openpyxl 由用户明确批准复用（Excel 排版需求）；**pandas 仍禁止**。

    P6-S3 原红线是「导出链路零重型依赖」，P6-S4 用户裁定改为复用项目既有 openpyxl
    （app/exports.py 同款，requirements 早已存在），故断言随之调整并登记。
    """
    exports_code = open("app/import_exports.py", encoding="utf-8").read()
    for path in ("app/imports.py", "app/import_exports.py"):
        code_lines = [line.strip() for line in open(path, encoding="utf-8") if line.strip()]
        for line in code_lines:
            assert not line.startswith(("import pandas", "from pandas")), f"{path} 引入了 pandas: {line}"
    assert "from openpyxl import Workbook" in exports_code, "应复用 openpyxl 生成 xlsx"
    assert "import csv" in exports_code, "csv 通道需保留（format=csv）"
    requirements = open("requirements.txt", encoding="utf-8").read().lower()
    assert "pandas" not in requirements
    assert "openpyxl" in requirements


def test_e12_migration_and_model_registered():
    import app.db as db_mod
    table = db_mod.Base.metadata.tables["external_import_rejections"]
    assert set(table.columns.keys()) == {
        "id", "workspace_id", "run_id", "kind", "row_number",
        "raw_data", "error_code", "error_reason", "created_at",
    }
    text = open("migrations/versions/0024_external_import_rejections.py", encoding="utf-8").read()
    assert 'revision: str = "0024_external_import_rejections"' in text
    assert 'down_revision: Union[str, Sequence[str], None] = "0023_purchase_approval"' in text
    assert "uq_external_import_rejection_row" in text


def test_e13_frontend_contract(tmp_path, monkeypatch):
    page = open("app/templates/sources.html", encoding="utf-8").read()
    assert "导出结果" in page and "导出异常" in page
    assert 'id="import-allow-skipped"' in page
    assert "将跳过错误行，错误数据可在批次历史中导出修改" in page
    assert "allow_skipped" in page
    assert "createObjectURL" in page and "export?view=" in page
    # P6-S4：xlsx 是二进制，前端不再读文本判断空；改用批次行数据里的 rejected_count
    assert "data-bad=" in page and "badCount" in page


# ---------------------------------------------------------------------------
# P6-S4 追加：xlsx 导出美化 + 静默丢行护栏
# ---------------------------------------------------------------------------


def _load_xlsx(response):
    import io as _io
    from openpyxl import load_workbook
    return load_workbook(_io.BytesIO(response.content))


def test_e14_xlsx_default_export_is_beautified(tmp_path, monkeypatch):
    """默认导出 .xlsx：表头加粗、全表居中、列宽自适应、拦截原因列加宽、首行冻结。"""
    from app.import_exports import MAX_WIDTH, MIN_WIDTH, WIDE_WIDTH
    client, _db = _client(tmp_path, monkeypatch)
    commit = _upload(client, content=DIRTY_CSV.encode(), allow_skipped=True)
    run_id = commit.json()["sync_run_id"]

    for view in ("raw", "rejected"):
        response = _export(client, run_id, view, fmt="")  # 不传 format → 默认 xlsx
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert response.headers["content-disposition"].endswith('.xlsx"')

        sheet = _load_xlsx(response).active
        headers = [cell.value for cell in sheet[1]]
        assert all(cell.font.bold for cell in sheet[1]), f"{view} 表头未加粗"
        assert all(cell.alignment.horizontal == "center" for cell in sheet[1]), f"{view} 表头未居中"
        for row in sheet.iter_rows(min_row=2):
            assert all(cell.alignment.horizontal == "center" for cell in row), f"{view} 数据未居中"
        assert sheet.freeze_panes == "A2"
        for index, header in enumerate(headers, start=1):
            width = sheet.column_dimensions[sheet.cell(1, index).column_letter].width
            if header in ("系统拦截原因", "备注"):
                assert width == WIDE_WIDTH, f"{view} 宽列 {header} 宽度应为 {WIDE_WIDTH}，实为 {width}"
            else:
                assert MIN_WIDTH <= width <= MAX_WIDTH, f"{view} 列 {header} 宽度越界: {width}"
    # 阻塞原因列内容仍在最后一列
    rejected_sheet = _load_xlsx(_export(client, run_id, "rejected", fmt="")).active
    header_values = [cell.value for cell in rejected_sheet[1]]
    assert header_values[-1] == "系统拦截原因"
    assert "第 2 行" in rejected_sheet.cell(2, len(header_values)).value


def test_e15_silent_drop_is_captured(tmp_path, monkeypatch):
    """护栏：下游「不报错但少给记录」也必须落成异常行，不允许静默吞行。"""
    import app.imports as imports_mod
    from app.connectors import load_records

    real = imports_mod._load
    dropped = {"sku": "S-2"}

    def silently_dropping(kind, text, *, platform, defaults):
        records = real(kind, text, platform=platform, defaults=defaults)
        return [record for record in records if getattr(record, "external_sku", None) != dropped["sku"]]

    monkeypatch.setattr(imports_mod, "_load", silently_dropping)
    outcome = imports_mod.parse_upload(
        GOOD_CSV.encode(), platform="taobao", kind="inventory", defaults={},
    )
    assert [record.external_sku for record in outcome.records] == ["S-1"]
    assert outcome.rejected_total == 1
    assert outcome.rejected[0].row_number == 2
    assert imports_mod.SILENT_DROP_MESSAGE in outcome.rejected[0].reason_message
    # 不变量：有效记录 + 异常行 == 数据行总数（100% 可对账）
    assert len(outcome.records) + outcome.rejected_total == outcome.rows_total == 2
    assert load_records  # 保留真实 load_* 引用，防止 import 被优化掉


def test_e16_header_only_file_keeps_headers(tmp_path, monkeypatch):
    """只有表头的文件不再丢列头（旧实现取首行键，无数据行时列头为空）。"""
    client, _db = _client(tmp_path, monkeypatch)
    header_only = b"external_sku,account_ref,store_ref,available_qty,as_of\n"
    preview = _upload(client, path="/api/external/imports/preview", content=header_only)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["csv_headers"] == ["external_sku", "account_ref", "store_ref", "available_qty", "as_of"]
    assert body["missing_required_fields"] == [], "列头齐全时不应报必填未识别"
    assert body["total"] == 0 and body["rejected_total"] == 0


def test_e17_extra_columns_do_not_leak_none_header(tmp_path, monkeypatch):
    """首行多出的列不再以 None 混进列头（会污染映射区与导出列）。"""
    client, _db = _client(tmp_path, monkeypatch)
    content = (
        "external_sku,account_ref,store_ref,available_qty,as_of\n"
        "S-1,a,s,7,2026-11-01T10:00:00Z,多余列\n"
        "S-2,a,s,3,2026-11-01T10:00:00Z\n"
    ).encode()
    preview = _upload(client, path="/api/external/imports/preview", content=content)
    assert preview.status_code == 200, preview.text
    headers = preview.json()["csv_headers"]
    assert None not in headers
    assert headers == ["external_sku", "account_ref", "store_ref", "available_qty", "as_of"]


def test_e18_persistence_not_truncated(tmp_path, monkeypatch):
    """预览响应可截断明细，**落库必须全量**（否则大文件脏行只记前 N 条 = 仍算丢行）。"""
    import app.imports as imports_mod
    monkeypatch.setattr(imports_mod, "MAX_REJECTED_ROWS", 2)
    client, db_mod = _client(tmp_path, monkeypatch)
    rows = "".join(f"S-{i},a,s,-5,2026-11-01T10:00:00Z\n" for i in range(1, 6))  # 5 行全坏
    dirty = ("external_sku,account_ref,store_ref,available_qty,as_of\n" + rows).encode()

    preview = _upload(client, path="/api/external/imports/preview", content=dirty)
    assert preview.status_code == 200, preview.text
    assert preview.json()["rejected_total"] == 5
    assert len(preview.json()["rejected_rows"]) == 2  # 响应截断

    # 全坏文件：勾选跳过 → 无有效行 → 422 空文件（拒绝整单，不静默成功）
    empty = _upload(client, content=dirty, allow_skipped=True)
    assert empty.status_code == 422, empty.text

    # 5 坏 + 1 好：可入库，且 5 条异常全部落库
    mixed = dirty + b"S-9,a,s,3,2026-11-01T10:00:00Z\n"
    commit = _upload(client, content=mixed, allow_skipped=True)
    assert commit.status_code == 200, commit.text
    assert commit.json()["rejected_count"] == 5
    with db_mod.SessionLocal() as db:
        from app.db import ExternalImportRejection
        assert db.query(ExternalImportRejection).count() == 5


def test_e19_batch_export_sheet_title_unchanged(tmp_path, monkeypatch):
    """回归保护：批次导出的工作表名默认判据**不得漂移**。

    P9-S2 给 build_download 加了可选 sheet_title（None = 沿用原判据），
    利润导出显式传「利润明细」；批次导出（raw / rejected）**不传参数**，
    仍应是「批次结果」/「异常明细」。

    编号说明：任务书指定本用例为 E-15，但本文件已有 E-15（静默丢行护栏），
    同文件同名函数会互相覆盖既有用例，故顺延为 E-19（已在 delivery 登记偏差）。
    """
    client, _db = _client(tmp_path, monkeypatch)
    commit = _upload(client, content=DIRTY_CSV.encode(), allow_skipped=True)
    assert commit.status_code == 200, commit.text
    run_id = commit.json()["sync_run_id"]

    raw_workbook = _load_xlsx(_export(client, run_id, "raw", fmt=""))
    assert raw_workbook.sheetnames == ["批次结果"], raw_workbook.sheetnames

    rejected_workbook = _load_xlsx(_export(client, run_id, "rejected", fmt=""))
    assert rejected_workbook.sheetnames == ["异常明细"], rejected_workbook.sheetnames
