"""P6-S1 多平台报表 CSV 导入测试（C-1..C-14）。

夹具模式照 tests/test_api_sync_compensation.py（tmp_path 隔离库 + operations 会话）。
"""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

INVENTORY_CSV = (
    "external_sku,account_ref,store_ref,warehouse_ref,available_qty,reserved_qty,as_of\n"
    "S-1,a,s,w,7,0,2026-11-01T10:00:00Z\n"
    "S-2,a,s,w,3,1,2026-11-01T10:00:00Z\n"
    "S-3,a,s,w,0,0,2026-11-01T10:00:00Z\n"
)
ORDERS_CSV = (
    "external_order_no,account_ref,store_ref,status,created_at,updated_at,"
    "external_sku,ordered_qty,gross_amount\n"
    "O-1,a,s,paid,2026-11-01T10:00:00Z,2026-11-01T10:01:00Z,S-1,2,20.00\n"
    "O-2,a,s,paid,2026-11-01T11:00:00Z,2026-11-01T11:01:00Z,S-2,1,10.00\n"
)


def _client(tmp_path, monkeypatch, *, with_readonly: bool = False):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'csv-import.db'}")
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
        workspace = db_mod.Workspace(tenant_key="csv-import", name="CSV 导入测试")
        db.add(workspace)
        db.flush()
        user = db_mod.UserAccount(
            login="operator",
            password_hash=hash_password("operator-password"),
            display_name="运营",
        )
        db.add(user)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        if with_readonly:
            viewer = db_mod.UserAccount(
                login="viewer",
                password_hash=hash_password("viewer-password"),
                display_name="只读",
            )
            db.add(viewer)
            db.flush()
            db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=viewer.id, role="readonly"))
        db.commit()
    client = TestClient(main_mod.app)
    login = client.post(
        "/login",
        data={"login": "operator", "password": "operator-password", "next": "/"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
    return client, db_mod


def _upload(client, *, path="/api/external/imports", content=INVENTORY_CSV.encode(), platform="taobao", kind="inventory", **extra):
    data = {"platform": platform, "kind": kind}
    data.update(extra)
    return client.post(path, files={"file": ("report.csv", content, "text/csv")}, data=data)


def _count(db_mod, model):
    with db_mod.SessionLocal() as db:
        return db.query(model).count()


def test_c1_inventory_csv_ingest(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    from app.db import ExternalInventorySnapshot
    response = _upload(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inserted"] == 3
    assert body["no_op"] == 0
    assert body["sync_run_id"]
    assert body["sync_status"] == "succeeded"
    assert body["detected_encoding"] == "utf-8"
    assert _count(db_mod, ExternalInventorySnapshot) == 3


def test_c2_same_file_is_noop(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    assert _upload(client).status_code == 200
    second = _upload(client)
    assert second.status_code == 200, second.text
    assert second.json()["no_op"] == 3
    assert second.json()["inserted"] == 0


def test_c3_conflict_never_overwrites(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    assert _upload(client).status_code == 200
    tampered = INVENTORY_CSV.replace("S-1,a,s,w,7,", "S-1,a,s,w,9,")
    response = _upload(client, content=tampered.encode())
    assert response.status_code == 200, response.text
    assert response.json()["conflict"] == 1
    assert response.json()["no_op"] == 2
    from app.db import ExternalInventorySnapshot
    with db_mod.SessionLocal() as db:
        row = db.query(ExternalInventorySnapshot).filter_by(external_sku="S-1").one()
        assert row.available_qty == 7


def test_c4_gbk_decoded(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    gbk_csv = (
        "external_sku,account_ref,store_ref,available_qty,as_of\n"
        "货品-A,a,s,5,2026-11-01T10:00:00Z\n"
    )
    response = _upload(client, content=gbk_csv.encode("gb18030"))
    assert response.status_code == 200, response.text
    assert response.json()["detected_encoding"] == "gb18030"
    assert response.json()["inserted"] == 1


def test_c5_utf8_bom(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    response = _upload(client, content=b"\xef\xbb\xbf" + INVENTORY_CSV.encode())
    assert response.status_code == 200, response.text
    assert response.json()["detected_encoding"] == "utf-8-sig"
    assert response.json()["inserted"] == 3


def test_c6_unrecognized_encoding_422(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    response = _upload(client, content=b"\xff\xfe\x00\x00")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "EXTERNAL_INPUT_ENCODING"


def test_c7_missing_required_column(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    bad = "account_ref,available_qty,as_of\na,7,2026-11-01T10:00:00Z\n"
    response = _upload(client, content=bad.encode())
    assert response.status_code == 422, response.text
    assert "external_sku" in response.json()["detail"]["code"]


def test_c8_oversize_413(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    huge = b"external_sku,account_ref\n" + b"a" * (20 * 1024 * 1024)
    response = _upload(client, content=huge)
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["code"] == "FILE_TOO_LARGE"


def test_c9_forbidden_zero_writes(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch, with_readonly=True)
    viewer = TestClient(client.app)
    login = viewer.post(
        "/login",
        data={"login": "viewer", "password": "viewer-password", "next": "/"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    from app.db import ExternalInventorySnapshot, ExternalSyncRun
    response = _upload(viewer)
    assert response.status_code == 403, response.text
    assert _count(db_mod, ExternalInventorySnapshot) == 0
    assert _count(db_mod, ExternalSyncRun) == 0


def test_c10_bad_kind_no_run(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    from app.db import ExternalSyncRun
    response = _upload(client, kind="events")
    assert response.status_code == 422, response.text
    assert _count(db_mod, ExternalSyncRun) == 0


def test_c11_orders_flat_columns(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    from app.db import ExternalOrder
    response = _upload(client, content=ORDERS_CSV.encode(), kind="orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inserted"] == 2
    assert body["kind"] == "orders"
    with db_mod.SessionLocal() as db:
        rows = db.query(ExternalOrder).all()
        assert len(rows) == 2
        assert rows[0].source_mode == "csv"


def test_c12_form_defaults_fill_missing_columns(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    no_account = (
        "external_sku,available_qty,as_of\n"
        "S-9,5,2026-11-01T10:00:00Z\n"
    )
    response = _upload(client, content=no_account.encode(), account_ref="acc-form", store_ref="st-form")
    assert response.status_code == 200, response.text
    from app.db import ExternalInventorySnapshot
    with db_mod.SessionLocal() as db:
        row = db.query(ExternalInventorySnapshot).one()
        assert row.account_ref == "acc-form"
        assert row.store_ref == "st-form"


def test_c13_preview_writes_nothing(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    from app.db import ExternalInventorySnapshot, ExternalOrder, ExternalSyncRun
    response = _upload(client, path="/api/external/imports/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert body["kind"] == "inventory"
    assert len(body["normalized_rows"]) == 3
    assert body["errors"] == []
    assert _count(db_mod, ExternalInventorySnapshot) == 0
    assert _count(db_mod, ExternalOrder) == 0
    assert _count(db_mod, ExternalSyncRun) == 0


def test_c14_run_queryable_with_csv_mode(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    assert _upload(client).status_code == 200
    runs = client.get("/api/external/sync/runs").json()
    assert any(run["source_mode"] == "csv" for run in runs)
