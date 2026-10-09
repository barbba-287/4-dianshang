"""客服工单领域服务测试。"""
from datetime import datetime
import importlib
import pytest

@pytest.fixture
def db_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tickets.db'}")
    import app.config as cfg
    import app.db as db
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db)
    db.Base.metadata.create_all(db.engine)
    yield db
    cfg.get_settings.cache_clear()

def test_ticket_lifecycle_events_assignment_and_workspace_scope(db_mod):
    from app.tickets import add_event, assign_ticket, create_ticket, list_tickets, ticket_detail, transition_ticket
    with db_mod.SessionLocal() as db:
        ws = db_mod.Workspace(tenant_key="ticket-a", name="工单 A")
        other = db_mod.Workspace(tenant_key="ticket-b", name="工单 B")
        db.add_all([ws, other]); db.commit()
        ticket = create_ticket(db, workspace_id=ws.id, subject="商品咨询", actor="agent", priority="high", idempotency_key="create-1")
        replay = create_ticket(db, workspace_id=ws.id, subject="商品咨询", actor="agent", priority="high", idempotency_key="create-1")
        assert replay.id == ticket.id
        assign_ticket(db, workspace_id=ws.id, ticket_id=ticket.id, assignee="customer-service", actor="agent", expected_version=1, idempotency_key="assign-1")
        add_event(db, workspace_id=ws.id, ticket_id=ticket.id, body="内部备注", author="agent", idempotency_key="event-1")
        transition_ticket(db, workspace_id=ws.id, ticket_id=ticket.id, target="in_progress", actor="agent", expected_version=2, idempotency_key="transition-1")
        detail = ticket_detail(db, workspace_id=ws.id, ticket_id=ticket.id)
        hidden = ticket_detail(db, workspace_id=other.id, ticket_id=ticket.id)
        rows, total = list_tickets(db, workspace_id=ws.id)
    assert total == 1
    assert rows[0].status == "in_progress"
    assert len(detail["events"]) >= 3
    assert hidden is None

def test_ticket_rejects_stale_version_and_invalid_transition(db_mod):
    from app.tickets import create_ticket, transition_ticket
    with db_mod.SessionLocal() as db:
        ws = db_mod.Workspace(tenant_key="ticket-version", name="版本")
        db.add(ws); db.commit()
        ticket = create_ticket(db, workspace_id=ws.id, subject="问题", actor="agent", idempotency_key="create-v")
        with pytest.raises(ValueError, match="TICKET_VERSION_CONFLICT"):
            transition_ticket(db, workspace_id=ws.id, ticket_id=ticket.id, target="in_progress", actor="agent", expected_version=99, idempotency_key="bad-v")
        with pytest.raises(ValueError, match="INVALID_TICKET_TRANSITION"):
            transition_ticket(db, workspace_id=ws.id, ticket_id=ticket.id, target="closed", actor="agent", expected_version=1, idempotency_key="bad-state")
