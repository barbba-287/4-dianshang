"""Manual customer-service ticket application service."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db import ServiceTicket, TicketAssignment, TicketEvent, TicketLink, ExternalOrder, Product

STATUSES = {"open", "assigned", "in_progress", "waiting", "resolved", "closed"}
PRIORITIES = {"low", "normal", "high", "urgent"}
TRANSITIONS = {"open": {"assigned", "in_progress"}, "assigned": {"in_progress", "waiting", "resolved"}, "in_progress": {"waiting", "resolved"}, "waiting": {"in_progress", "resolved"}, "resolved": {"closed", "open"}, "closed": {"open"}}


def _ticket_no(db, workspace_id: int) -> str:
    count = db.scalar(select(func.count(ServiceTicket.id)).where(ServiceTicket.workspace_id == workspace_id)) or 0
    return f"T-{workspace_id:04d}-{int(count) + 1:06d}"


def _ticket(db, workspace_id: int, ticket_id: int) -> ServiceTicket | None:
    return db.scalar(select(ServiceTicket).where(ServiceTicket.id == ticket_id, ServiceTicket.workspace_id == workspace_id))


def create_ticket(db: Session, *, workspace_id: int, subject: str, actor: str, issue_type: str = "other", description: str = "", sku_ref: str | None = None, priority: str = "normal", customer_ref: str | None = None, external_order_ref: str | None = None, source_mode: str = "manual", simulated: bool = True, idempotency_key: str | None = None) -> ServiceTicket:
    if not subject.strip() or issue_type not in {"presale", "order_payment", "logistics", "return_refund", "product_quality", "complaint", "other"} or priority not in PRIORITIES: raise ValueError("INVALID_TICKET_INPUT")
    if idempotency_key:
        previous = db.scalar(select(TicketEvent).where(TicketEvent.workspace_id == workspace_id, TicketEvent.idempotency_key == idempotency_key, TicketEvent.event_type == "ticket_created"))
        if previous: return _ticket(db, workspace_id, int(previous.body.split(":", 1)[1]))
    ticket = ServiceTicket(workspace_id=workspace_id, ticket_no=_ticket_no(db, workspace_id), subject=subject.strip(), issue_type=issue_type, description=description.strip(), sku_ref=sku_ref, status="open", priority=priority, channel="manual", customer_ref=customer_ref, external_order_ref=external_order_ref, source_mode=source_mode, simulated=simulated, created_by=actor, version=1)
    db.add(ticket); db.flush()
    db.add(TicketEvent(workspace_id=workspace_id, ticket_id=ticket.id, event_type="ticket_created", body=f"ticket:{ticket.id}", author=actor, visibility="internal", source_mode=source_mode, simulated=simulated, idempotency_key=idempotency_key))
    db.commit(); db.refresh(ticket); return ticket


def list_tickets(db: Session, *, workspace_id: int, page: int = 1, page_size: int = 20, status: str | None = None, priority: str | None = None, keyword: str | None = None):
    filters = [ServiceTicket.workspace_id == workspace_id]
    if status: filters.append(ServiceTicket.status == status)
    if priority: filters.append(ServiceTicket.priority == priority)
    if keyword: filters.append(ServiceTicket.subject.ilike(f"%{keyword}%"))
    total = db.scalar(select(func.count(ServiceTicket.id)).where(*filters)) or 0
    rows = db.scalars(select(ServiceTicket).where(*filters).order_by(ServiceTicket.updated_at.desc(), ServiceTicket.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    return rows, int(total)


def ticket_detail(db: Session, *, workspace_id: int, ticket_id: int) -> dict | None:
    ticket = _ticket(db, workspace_id, ticket_id)
    if not ticket: return None
    events = db.scalars(select(TicketEvent).where(TicketEvent.workspace_id == workspace_id, TicketEvent.ticket_id == ticket.id).order_by(TicketEvent.created_at, TicketEvent.id)).all()
    assignments = db.scalars(select(TicketAssignment).where(TicketAssignment.workspace_id == workspace_id, TicketAssignment.ticket_id == ticket.id).order_by(TicketAssignment.created_at)).all()
    links = db.scalars(select(TicketLink).where(TicketLink.workspace_id == workspace_id, TicketLink.ticket_id == ticket.id)).all()
    return {"ticket": ticket, "events": events, "assignments": assignments, "links": links}


def add_event(db: Session, *, workspace_id: int, ticket_id: int, body: str, author: str, event_type: str = "internal_note", idempotency_key: str | None = None) -> TicketEvent:
    ticket = _ticket(db, workspace_id, ticket_id)
    if not ticket or not body.strip(): raise ValueError("TICKET_NOT_FOUND_OR_INVALID")
    if idempotency_key:
        old = db.scalar(select(TicketEvent).where(TicketEvent.workspace_id == workspace_id, TicketEvent.ticket_id == ticket_id, TicketEvent.idempotency_key == idempotency_key))
        if old: return old
    event = TicketEvent(workspace_id=workspace_id, ticket_id=ticket_id, event_type=event_type, body=body.strip(), author=author, visibility="internal", source_mode="manual", simulated=True, idempotency_key=idempotency_key)
    db.add(event); ticket.updated_at = datetime.utcnow(); db.commit(); db.refresh(event); return event


def transition_ticket(db: Session, *, workspace_id: int, ticket_id: int, target: str, actor: str, expected_version: int, idempotency_key: str) -> ServiceTicket:
    ticket = _ticket(db, workspace_id, ticket_id)
    if not ticket: raise ValueError("TICKET_NOT_FOUND")
    if target not in STATUSES or target not in TRANSITIONS.get(ticket.status, set()): raise ValueError("INVALID_TICKET_TRANSITION")
    if ticket.version != expected_version: raise ValueError("TICKET_VERSION_CONFLICT")
    if not idempotency_key: raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    ticket.status = target; ticket.version += 1; ticket.closed_at = datetime.utcnow() if target == "closed" else None
    db.add(TicketEvent(workspace_id=workspace_id, ticket_id=ticket.id, event_type="status_transition", body=target, author=actor, visibility="internal", source_mode="manual", simulated=True, idempotency_key=idempotency_key))
    db.commit(); db.refresh(ticket); return ticket


def assign_ticket(db: Session, *, workspace_id: int, ticket_id: int, assignee: str, actor: str, expected_version: int, idempotency_key: str) -> ServiceTicket:
    ticket = _ticket(db, workspace_id, ticket_id)
    if not ticket: raise ValueError("TICKET_NOT_FOUND")
    if ticket.version != expected_version: raise ValueError("TICKET_VERSION_CONFLICT")
    if not assignee.strip() or not idempotency_key: raise ValueError("INVALID_ASSIGNMENT")
    now = datetime.utcnow()
    current = db.scalars(select(TicketAssignment).where(TicketAssignment.workspace_id == workspace_id, TicketAssignment.ticket_id == ticket.id, TicketAssignment.ended_at.is_(None))).all()
    for row in current: row.ended_at = now
    db.add(TicketAssignment(workspace_id=workspace_id, ticket_id=ticket.id, assignee=assignee.strip(), actor=actor, created_at=now))
    ticket.assigned_to = assignee.strip(); ticket.status = "assigned" if ticket.status == "open" else ticket.status; ticket.version += 1
    db.commit(); db.refresh(ticket); return ticket
