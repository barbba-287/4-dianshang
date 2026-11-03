"""P6-S2 智能中文列头映射测试（D-1..D-11）。

夹具模式照 test_p6s1_csv_import.py（tmp_path 隔离库 + operations 会话）。
"""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

# 中文列头库存报表：全命中（available_qty 用「库存数量」）
ZH_INVENTORY_CSV = (
    "商品编码,店铺账号,库存数量,统计时间\n"
    "S-ZH-1,我的店铺,120,2026-11-01T10:00:00Z\n"
    "S-ZH-2,我的店铺,0,2026-11-01T10:00:00Z\n"
)
# 缺必填列（无「统计时间」→ as_of 缺失）
ZH_INVENTORY_NO_ASOF = (
    "商品编码,店铺账号,库存数量\n"
    "S-ZH-1,我的店铺,120\n"
)
# 全角 + 空格 + 大小写混杂列头
NORMALIZED_CSV = (
    "ＳＫＵ, Account Ref ,ＳＴＡＴＳ时间\n"
    "S-N-1,a,2026-11-01T10:00:00Z\n"
)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'col-map.db'}")
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
        workspace = db_mod.Workspace(tenant_key="col-map", name="列头映射测试")
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


def _upload(client, *, path="/api/external/imports/preview", content=ZH_INVENTORY_CSV.encode(), platform="taobao", kind="inventory", column_mapping=None, **extra):
    data = {"platform": platform, "kind": kind}
    if column_mapping is not None:
        data["column_mapping"] = json.dumps(column_mapping, ensure_ascii=False)
    data.update(extra)
    return client.post(path, files={"file": ("报表.csv", content, "text/csv")}, data=data)


def _count(db_mod, model):
    with db_mod.SessionLocal() as db:
        return db.query(model).count()


def test_d1_mapping_module_isolated():
    """映射字典独立模块；后端文件零别名硬编码（sources.html 的 FIELD_LABELS 是
    展示名常量，评审已裁定放前端，不属于匹配别名，不在 grep 范围）。"""
    import app.column_mappings as cm
    assert cm.FIELD_ALIASES.keys() >= {"inventory", "orders"}
    assert cm.REQUIRED_FIELDS["inventory"] == ("external_sku", "as_of")
    assert "external_sku" in cm.REQUIRED_FIELDS["orders"] or "external_order_no" in cm.REQUIRED_FIELDS["orders"]
    for path in ("app/main.py", "app/imports.py"):
        text = open(path, encoding="utf-8").read()
        for alias in ("商品编码", "实付金额", "买家实付", "规格代码", "商家编码"):
            assert alias not in text, f"{path} 硬编码了别名 {alias}"


def test_d2_alias_conflict_rejected_at_import(monkeypatch):
    """同 kind 内一个归一化别名命中两个 field → 模块加载即报错。"""
    import app.column_mappings as cm
    monkeypatch.setitem(cm.FIELD_ALIASES, "inventory", {
        "external_sku": ("商品编码",),
        "internal_code": ("商品编码",),
    })
    with pytest.raises(RuntimeError, match="别名冲突"):
        cm._build_reverse_index("inventory")


def test_d3_normalize_fullwidth_space_case():
    from app.column_mappings import normalize_header
    assert normalize_header("ＳＫＵ") == normalize_header("sku")
    assert normalize_header("商 品　编 码") == normalize_header("商品编码")
    assert normalize_header("Sku") == "sku"
    assert normalize_header("）ＡＢ（") == ")ab("


def test_d4_suggestion_full_hit(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    response = _upload(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mapping_suggestion"]["external_sku"] == "商品编码"
    assert body["mapping_suggestion"]["as_of"] == "统计时间"
    assert body["missing_required_fields"] == []
    assert body["unmapped_columns"] == []
    assert body["csv_headers"] == ["商品编码", "店铺账号", "库存数量", "统计时间"]
    assert body["total"] == 2


def test_d5_preview_missing_200_commit_422(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    preview = _upload(client, content=ZH_INVENTORY_NO_ASOF.encode())
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["missing_required_fields"] == ["as_of"]
    assert body["normalized_rows"] == []
    assert body["mapping_suggestion"]["external_sku"] == "商品编码"
    from app.db import ExternalInventorySnapshot
    commit = _upload(client, path="/api/external/imports", content=ZH_INVENTORY_NO_ASOF.encode())
    assert commit.status_code == 422
    assert commit.json()["detail"]["code"] == "EXTERNAL_MISSING_FIELDS"
    assert _count(db_mod, ExternalInventorySnapshot) == 0


def test_d6_user_mapping_overrides(tmp_path, monkeypatch):
    """column_mapping 为完整替换语义（所见即所得）：未提及列不自动补充。"""
    client, db_mod = _client(tmp_path, monkeypatch)
    csv_text = "货号,统计时间,库存数量\nS-OV-1,2026-11-01T10:00:00Z,5\n".encode()
    preview = _upload(client, content=csv_text, column_mapping={"货号": "external_sku"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["mapping_suggestion"] == {"external_sku": "货号"}
    assert preview.json()["missing_required_fields"] == ["as_of"]
    full = {"货号": "external_sku", "统计时间": "as_of"}
    commit = _upload(client, path="/api/external/imports", content=csv_text, column_mapping=full, account_ref="acc-ov")
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted"] == 1


def test_d7_zh_end_to_end(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    from app.db import ExternalInventorySnapshot
    commit = _upload(client, path="/api/external/imports")
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted"] == 2
    with db_mod.SessionLocal() as db:
        rows = db.query(ExternalInventorySnapshot).all()
        assert len(rows) == 2
        assert rows[0].external_sku == "S-ZH-1"
        assert rows[0].available_qty == 120
        assert rows[0].source_mode == "csv"


def test_d8_ambiguous_column_takes_first(tmp_path, monkeypatch):
    from app.column_mappings import suggest_mapping
    mapping, unmapped = suggest_mapping(["总价", "金额", "商品编码"], kind="orders")
    assert mapping["gross_amount"] == "总价"
    assert "金额" in unmapped
    assert mapping["external_sku"] == "商品编码"


def test_d9_form_fallback_not_missing(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    csv_text = "商品编码,统计时间,库存数量\nS-FB-1,2026-11-01T10:00:00Z,7\n".encode()
    preview = _upload(client, content=csv_text, account_ref="acc-form")
    assert preview.status_code == 200, preview.text
    assert preview.json()["missing_required_fields"] == []
    commit = _upload(client, path="/api/external/imports", content=csv_text, account_ref="acc-form")
    assert commit.status_code == 200, commit.text
    from app.db import ExternalInventorySnapshot
    with db_mod.SessionLocal() as db:
        row = db.query(ExternalInventorySnapshot).one()
        assert row.account_ref == "acc-form"


def test_d10_mapping_preserves_idempotency(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    from app.db import ExternalInventorySnapshot
    mapping = {"商品编码": "external_sku", "店铺账号": "account_ref", "统计时间": "as_of", "库存数量": "available_qty"}
    first = _upload(client, path="/api/external/imports", column_mapping=mapping)
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 2
    second = _upload(client, path="/api/external/imports", column_mapping=mapping)
    assert second.status_code == 200, second.text
    assert second.json()["no_op"] == 2
    assert second.json()["inserted"] == 0
    assert _count(db_mod, ExternalInventorySnapshot) == 2


def test_d10b_mapping_conflict_422(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    response = _upload(client, column_mapping={"总价": "gross_amount", "金额": "gross_amount"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "MAPPING_CONFLICT"


def test_d10b2_mapping_invalid_json(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/external/imports/preview",
        files={"file": ("报表.csv", ZH_INVENTORY_CSV.encode(), "text/csv")},
        data={"platform": "taobao", "kind": "inventory", "column_mapping": "{bad json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MAPPING_INVALID"


def test_d11_sources_page_mapping_ui_contract(tmp_path, monkeypatch):
    client, db_mod = _client(tmp_path, monkeypatch)
    html = client.get("/sources").text
    for marker in ('id="import-mapping"', "mapping_suggestion", "missing_required_fields", "FIELD_LABELS", "column_mapping"):
        assert marker in html, f"缺少 {marker}"
