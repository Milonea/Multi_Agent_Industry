"""Cross-repository dependency graph with fail-closed diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEPENDENCY_STATES = frozenset({"blocked", "ready", "verified", "stale", "unknown"})


class DependencyGraphError(ValueError):
    """Raised for structurally invalid graph data."""


def _required(item: Mapping[str, Any], fields: tuple[str, ...], scope: str) -> None:
    missing = [field for field in fields if field not in item]
    if missing:
        raise DependencyGraphError(f"{scope} is missing required fields: {', '.join(missing)}")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DependencyGraphError(f"{field} must be a non-empty string")
    return value


def _find_cycles(edges: list[Mapping[str, Any]]) -> tuple[tuple[str, ...], ...]:
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(str(edge["producer"]), []).append(str(edge["consumer"]))
    cycles: list[tuple[str, ...]] = []

    def visit(node: str, stack: list[str], active: set[str], visited: set[str]) -> None:
        if node in active:
            start = stack.index(node)
            cycles.append(tuple(stack[start:] + [node]))
            return
        if node in visited:
            return
        active.add(node)
        stack.append(node)
        for child in adjacency.get(node, []):
            visit(child, stack, active, visited)
        stack.pop()
        active.remove(node)
        visited.add(node)

    visited: set[str] = set()
    for node in sorted(adjacency):
        visit(node, [], set(), visited)
    return tuple(sorted(set(cycles)))


def validate_graph(data: Mapping[str, Any]) -> list[str]:
    """Return diagnostics; cycle and stale conditions are never treated as ready."""

    if not isinstance(data, Mapping):
        raise DependencyGraphError("dependency graph must be an object")
    _required(data, ("schema_version", "nodes", "edges"), "graph")
    if data["schema_version"] != "0.1":
        raise DependencyGraphError(f"unsupported graph version: {data['schema_version']!r}")
    nodes = data["nodes"]
    edges = data["edges"]
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise DependencyGraphError("nodes and edges must be arrays")
    node_ids: set[str] = set()
    diagnostics: list[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise DependencyGraphError(f"nodes[{index}] must be an object")
        _required(node, ("id", "repository", "capability", "state", "contract_version"), f"nodes[{index}]")
        node_id = _text(node["id"], f"nodes[{index}].id")
        if node_id in node_ids:
            raise DependencyGraphError(f"nodes[{index}].id must be unique and non-empty")
        node_ids.add(node_id)
        for field in ("repository", "capability"):
            _text(node[field], f"nodes[{index}].{field}")
        if not isinstance(node["state"], str) or node["state"] not in DEPENDENCY_STATES:
            raise DependencyGraphError(f"nodes[{index}].state is invalid")
        _text(node["contract_version"], f"nodes[{index}].contract_version")
    edge_ids: set[str] = set()
    for index, edge in enumerate(edges):
        if not isinstance(edge, Mapping):
            raise DependencyGraphError(f"edges[{index}] must be an object")
        _required(edge, ("id", "producer", "consumer", "requirement", "contract_version", "state"), f"edges[{index}]")
        edge_id = _text(edge["id"], f"edges[{index}].id")
        if edge_id in edge_ids:
            raise DependencyGraphError(f"edges[{index}].id must be unique and non-empty")
        edge_ids.add(edge_id)
        producer_id = _text(edge["producer"], f"edges[{index}].producer")
        consumer_id = _text(edge["consumer"], f"edges[{index}].consumer")
        _text(edge["requirement"], f"edges[{index}].requirement")
        if producer_id not in node_ids:
            diagnostics.append(f"missing producer: {producer_id}")
        if consumer_id not in node_ids:
            diagnostics.append(f"missing consumer: {consumer_id}")
        if not isinstance(edge["state"], str) or edge["state"] not in DEPENDENCY_STATES:
            raise DependencyGraphError(f"edges[{index}].state is invalid")
        if not isinstance(edge["contract_version"], str) or not edge["contract_version"].strip():
            diagnostics.append(f"unknown contract version: edge:{edge_id}")
    for cycle in _find_cycles([edge for edge in edges if isinstance(edge, Mapping)]):
        diagnostics.append("dependency cycle: " + " -> ".join(cycle))
    node_by_id = {node["id"]: node for node in nodes}
    for edge in edges:
        if edge.get("producer") in node_by_id and edge.get("consumer") in node_by_id:
            producer = node_by_id[edge["producer"]]
            if producer["contract_version"] != edge["contract_version"]:
                diagnostics.append(f"contract version mismatch: edge:{edge['id']}")
            if producer["state"] in {"stale", "unknown"} or edge["state"] in {"stale", "unknown"}:
                diagnostics.append(f"non-fresh dependency: edge:{edge['id']}")
    return sorted(set(diagnostics))


@dataclass(frozen=True)
class DependencyGraph:
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    diagnostics: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DependencyGraph":
        diagnostics = validate_graph(data)
        return cls(
            nodes=tuple(dict(node) for node in data["nodes"]),
            edges=tuple(dict(edge) for edge in data["edges"]),
            diagnostics=tuple(diagnostics),
        )

    @property
    def ready(self) -> bool:
        return not self.diagnostics

    def ready_consumers(self) -> tuple[str, ...]:
        if not self.ready:
            return tuple()
        node_by_id = {node["id"]: node for node in self.nodes}
        ready: list[str] = []
        for node in self.nodes:
            if node["state"] not in {"ready", "verified"}:
                continue
            incoming = [edge for edge in self.edges if edge["consumer"] == node["id"]]
            if all(
                edge["state"] in {"ready", "verified"}
                and node_by_id.get(edge["producer"], {}).get("state") == "verified"
                for edge in incoming
            ):
                ready.append(node["id"])
        return tuple(sorted(ready))

    def downstream_unlock_count(self, producer_id: str) -> int:
        return len({edge["consumer"] for edge in self.edges if edge["producer"] == producer_id})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "nodes": [dict(node) for node in self.nodes],
            "edges": [dict(edge) for edge in self.edges],
        }
