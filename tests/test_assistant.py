from app.assistant import route_intent, query_assistant

def test_route_intent_prioritizes_write_rejection():
    assert route_intent('补货建议并提交采购')[0] == 'readonly_rejected'

def test_route_intent_structured_queries():
    assert route_intent('库存健康')[0] == 'inventory_health'
    assert route_intent('当前补货建议')[0] == 'replenishment'
    assert route_intent('同步状态')[0] == 'sync_health'
    assert route_intent('查看商品和SKU')[0] == 'catalog'

def test_route_intent_unknown():
    assert route_intent('今天天气')[1] == 'UNKNOWN_INTENT'


def test_replenishment_empty_is_friendly(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'assistant.db'}")
    import importlib
    import app.config as cfg
    import app.db as db_mod
    importlib.reload(cfg); importlib.reload(db_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    with db_mod.SessionLocal() as db:
        result = query_assistant(db, workspace_id=1, message="当前有哪些补货建议？")
    assert result["ok"] is True
    assert result["data"]["total"] == 0
    assert result["data"]["items"] == []
    assert "没有" in result["answer"]
