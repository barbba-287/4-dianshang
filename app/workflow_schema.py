"""Validation and canonicalization for visual workflow graph documents."""
from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from typing import Any

SCHEMA_VERSION = "visual-workflow.v1"
ALLOWED_NODE_TYPES = frozenset({
    "source_snapshot", "source_image_check", "foreground_cutout", "source_geometry_check",
    "provider_generate", "deterministic_post_process", "quality_gate", "human_review",
    "revision", "export",
})
REQUIRED_ENTRY = "source_snapshot"
REQUIRED_EXIT = "export"


def canonical_workflow_json(workflow: dict[str, Any]) -> str:
    return json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def workflow_hash(workflow: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_workflow_json(workflow).encode("utf-8")).hexdigest()


def _nodes(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("WORKFLOW_NODES_REQUIRED")
    return nodes


def validate_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(workflow, dict):
        raise ValueError("WORKFLOW_OBJECT_REQUIRED")
    if workflow.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("WORKFLOW_SCHEMA_UNSUPPORTED")
    for key in ("workflow_key", "version", "label"):
        if not isinstance(workflow.get(key), str) or not workflow[key].strip():
            raise ValueError(f"WORKFLOW_{key.upper()}_REQUIRED")
    nodes = _nodes(workflow)
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("WORKFLOW_NODE_INVALID")
        node_id = node.get("node_id")
        node_type = node.get("type")
        if not isinstance(node_id, str) or not node_id.strip() or node_id in node_ids:
            raise ValueError("WORKFLOW_NODE_ID_INVALID")
        if node_type not in ALLOWED_NODE_TYPES:
            raise ValueError(f"WORKFLOW_NODE_TYPE_UNSUPPORTED:{node_type}")
        if not isinstance(node.get("config", {}), dict):
            raise ValueError("WORKFLOW_NODE_CONFIG_INVALID")
        node_ids.add(node_id)
    if REQUIRED_ENTRY not in node_ids or REQUIRED_EXIT not in node_ids:
        raise ValueError("WORKFLOW_ENTRY_EXIT_REQUIRED")
    edges = workflow.get("edges")
    if not isinstance(edges, list):
        raise ValueError("WORKFLOW_EDGES_REQUIRED")
    edge_ids: set[str] = set()
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise ValueError("WORKFLOW_EDGE_INVALID")
        edge_id = edge.get("edge_id") or f"edge-{index}"
        source, target = edge.get("source"), edge.get("target")
        if edge_id in edge_ids or source not in node_ids or target not in node_ids or source == target:
            raise ValueError("WORKFLOW_EDGE_INVALID")
        pair = (source, target)
        if pair in {(s, t) for s in outgoing for t in outgoing[s]}:
            raise ValueError("WORKFLOW_EDGE_DUPLICATE")
        edge_ids.add(edge_id)
        outgoing[source].append(target)
        incoming[target].append(source)
    if incoming[REQUIRED_ENTRY] or outgoing[REQUIRED_EXIT]:
        raise ValueError("WORKFLOW_ENTRY_EXIT_DIRECTION_INVALID")
    if any(not incoming[node_id] and node_id != REQUIRED_ENTRY for node_id in node_ids):
        raise ValueError("WORKFLOW_NODE_DISCONNECTED")
    if any(not outgoing[node_id] and node_id != REQUIRED_EXIT for node_id in node_ids):
        raise ValueError("WORKFLOW_NODE_DISCONNECTED")
    order = topological_nodes({"nodes": nodes, "edges": edges})
    if len(order) != len(node_ids):
        raise ValueError("WORKFLOW_CYCLE")
    guardrails = workflow.get("guardrails", {})
    if not isinstance(guardrails, dict) or guardrails.get("human_review_required") is not True:
        raise ValueError("WORKFLOW_HUMAN_REVIEW_REQUIRED")
    return copy.deepcopy(workflow)


def topological_nodes(workflow: dict[str, Any]) -> list[str]:
    nodes = workflow.get("nodes") or []
    ids = [node.get("node_id") for node in nodes]
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in ids}
    incoming: dict[str, int] = {node_id: 0 for node_id in ids}
    for edge in workflow.get("edges") or []:
        source, target = edge.get("source"), edge.get("target")
        if source in outgoing and target in incoming:
            outgoing[source].append(target)
            incoming[target] += 1
    queue = deque(node_id for node_id in ids if incoming[node_id] == 0)
    result: list[str] = []
    while queue:
        node_id = queue.popleft()
        result.append(node_id)
        for target in outgoing[node_id]:
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    return result


def legacy_plan(workflow: dict[str, Any]) -> dict[str, Any]:
    validated = validate_workflow(workflow)
    nodes = {node["node_id"]: node for node in validated["nodes"]}
    stages = [nodes[node_id]["type"] for node_id in topological_nodes(validated)]
    return {
        "workflow_key": validated["workflow_key"],
        "workflow_version": validated["version"],
        "workflow_hash": workflow_hash(validated),
        "nodes": validated["nodes"],
        "edges": validated["edges"],
        "stages": stages,
        "label": validated["label"],
        "guardrails": validated.get("guardrails", {}),
    }


def customize_workflow(workflow: dict[str, Any], *, version: str, node_config_updates: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    derived = validate_workflow(workflow)
    derived["version"] = version
    derived["parent"] = {"workflow_key": workflow["workflow_key"], "version": workflow["version"]}
    for node_id, updates in (node_config_updates or {}).items():
        node = next((item for item in derived["nodes"] if item["node_id"] == node_id), None)
        if node is None:
            raise ValueError("WORKFLOW_NODE_NOT_FOUND")
        if not isinstance(updates, dict):
            raise ValueError("WORKFLOW_NODE_CONFIG_INVALID")
        node["config"].update(copy.deepcopy(updates))
    return validate_workflow(derived)
