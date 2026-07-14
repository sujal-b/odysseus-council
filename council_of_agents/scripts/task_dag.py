from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from council_of_agents.scripts.ledger_models import TaskResult, WorkPacket


def _bounded_summary(text: str, limit: int = 1200) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    marker = "\n...[dependency output compacted]...\n"
    content_limit = max(0, limit - len(marker))
    head = int(content_limit * 0.75)
    tail = content_limit - head
    return f"{text[:head]}{marker}{text[-tail:]}"


@dataclass
class TaskNode:
    id: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "PENDING"
    output: str = ""
    reason: str = ""
    retry_count: int = 0
    max_retries: int = 2
    error_history: list = field(default_factory=list)
    # Machine-checkable done-condition for this task (emitted by the
    # Strategist). Retained so the completeness auditor can grade the
    # delivered artifact against each task's acceptance criterion.
    acceptance: str = ""
    acceptance_ids: list[str] = field(default_factory=list)
    read_scope: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    read_scope_declared: bool = False
    write_scope_declared: bool = False
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    base_hashes: dict[str, str] = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    result: TaskResult | None = None


class TaskDAG:
    def __init__(self) -> None:
        self._nodes: dict[str, TaskNode] = {}
        self._adj: dict[str, list[str]] = {}
        self._deps: dict[str, set[str]] = {}

    def add_task(self, node: TaskNode) -> None:
        nid = node.id
        if nid in self._nodes:
            raise ValueError(f"Duplicate task ID: {nid}")
        if nid in node.depends_on:
            raise ValueError(f"Self-dependency: {nid}")
        for dep in node.depends_on:
            if dep not in self._nodes:
                raise ValueError(f"Unknown dependency '{dep}' for task '{nid}'")

        self._nodes[nid] = node
        self._adj.setdefault(nid, [])
        self._deps[nid] = set(node.depends_on)
        for dep in node.depends_on:
            self._adj.setdefault(dep, []).append(nid)

        cycle = self._detect_cycle_from(nid)
        if cycle:
            self._remove_node(nid)
            raise ValueError(f"Cycle detected: {' -> '.join(cycle)}")

    def get_ready_tasks(self) -> list[TaskNode]:
        return [
            n for n in self._nodes.values()
            if n.status == "PENDING" and all(
                self._nodes[d].status == "DONE" for d in self._deps[n.id]
            )
        ]

    def mark_done(
        self, task_id: str, output: str = "", result: TaskResult | None = None
    ) -> None:
        if task_id not in self._nodes:
            raise ValueError(f"Unknown task: {task_id}")
        self._nodes[task_id].status = "DONE"
        self._nodes[task_id].output = output
        self._nodes[task_id].result = result or TaskResult(
            task_id=task_id,
            summary=_bounded_summary(output),
        )

    def build_work_packet(self, task_id: str) -> WorkPacket:
        node = self._nodes.get(task_id)
        if node is None:
            raise ValueError(f"Unknown task: {task_id}")
        dependency_results = {}
        for dep_id in node.depends_on:
            dep = self._nodes.get(dep_id)
            if dep and dep.result:
                dependency_results[dep_id] = dep.result
        acceptance_ids = list(node.acceptance_ids)
        if not acceptance_ids and node.acceptance:
            acceptance_ids = [node.id]
        return WorkPacket(
            task_id=node.id,
            objective=node.description,
            acceptance_ids=acceptance_ids,
            depends_on=list(node.depends_on),
            artifact_refs=list(node.artifact_refs),
            evidence_refs=list(node.evidence_refs),
            dependency_results=dependency_results,
            read_scope=list(node.read_scope),
            write_scope=list(node.write_scope),
            base_hashes=dict(node.base_hashes),
            verification=node.verification or None,
            attempt=node.retry_count + 1,
        )

    @staticmethod
    def _normalized_scope(values: list[str]) -> list[PurePosixPath]:
        normalized = []
        for value in values:
            text = str(value).strip().replace("\\", "/").strip("/").casefold()
            if text:
                normalized.append(PurePosixPath(text))
        return normalized

    @staticmethod
    def _scopes_valid(values: list[str]) -> bool:
        for value in values:
            text = str(value).strip().replace("\\", "/")
            path = PurePosixPath(text)
            if (
                not text
                or path.is_absolute()
                or ".." in path.parts
                or (path.parts and ":" in path.parts[0])
            ):
                return False
        return True

    @classmethod
    def tasks_conflict(cls, left: TaskNode, right: TaskNode) -> bool:
        if not all((
            cls._scopes_valid(left.read_scope),
            cls._scopes_valid(left.write_scope),
            cls._scopes_valid(right.read_scope),
            cls._scopes_valid(right.write_scope),
        )):
            return True
        # Two explicitly read-only tasks are always safe together.
        if (
            left.write_scope_declared and not left.write_scope
            and right.write_scope_declared and not right.write_scope
        ):
            return False
        # Unknown scopes are serialized whenever either task may write.
        if not left.write_scope_declared or not right.write_scope_declared:
            return True
        if left.write_scope and not right.read_scope_declared:
            return True
        if right.write_scope and not left.read_scope_declared:
            return True

        left_writes = cls._normalized_scope(left.write_scope)
        right_writes = cls._normalized_scope(right.write_scope)
        left_reads = cls._normalized_scope(left.read_scope)
        right_reads = cls._normalized_scope(right.read_scope)

        def overlaps(a, b):
            return a == b or a in b.parents or b in a.parents

        return any(
            overlaps(a, b)
            for a in left_writes
            for b in (right_writes + right_reads)
        ) or any(
            overlaps(a, b)
            for a in right_writes
            for b in left_reads
        )

    @classmethod
    def safe_execution_wave(cls, ready: list[TaskNode]) -> list[TaskNode]:
        """Greedily select a deterministic maximal conflict-free wave."""
        wave = []
        for task in sorted(ready, key=lambda node: node.id):
            if all(not cls.tasks_conflict(task, selected) for selected in wave):
                wave.append(task)
        return wave

    def mark_failed(self, task_id: str, reason: str = "") -> None:
        if task_id not in self._nodes:
            raise ValueError(f"Unknown task: {task_id}")
        self._nodes[task_id].status = "FAILED"
        if reason:
            self._nodes[task_id].reason = reason

    def mark_retryable(self, task_id: str, error: str = "") -> bool:
        node = self._nodes.get(task_id)
        if not node or node.status != "FAILED":
            return False
        if node.retry_count >= node.max_retries:
            return False
        node.retry_count += 1
        node.error_history.append(node.reason)
        node.status = "PENDING"
        node.reason = ""
        return True

    def propagate_failures(self):
        """Mark PENDING nodes BLOCKED if any dependency is permanently failed or blocked.
        Uses iterative transitive closure to handle chains (T1→T2→T3)."""
        changed = True
        while changed:
            changed = False
            for node in self._nodes.values():
                if node.status != "PENDING":
                    continue
                for dep_id in node.depends_on:
                    dep = self._nodes.get(dep_id)
                    if dep and (
                        (dep.status == "FAILED" and dep.retry_count >= dep.max_retries)
                        or dep.status == "BLOCKED"
                    ):
                        node.status = "BLOCKED"
                        node.reason = (
                            f"Dependency {dep_id} failed permanently"
                            if dep.status == "FAILED"
                            else f"Dependency {dep_id} is blocked"
                        )
                        changed = True
                        break





    def all_complete(self) -> bool:
        return all(n.status in ("DONE", "FAILED", "BLOCKED") for n in self._nodes.values())

    def to_dict(self) -> dict:
        nodes = [
            {"id": n.id, "description": n.description, "depends_on": n.depends_on,
             "status": n.status, "output": n.output, "reason": n.reason,
             "retry_count": n.retry_count, "max_retries": n.max_retries, "error_history": n.error_history,
             "acceptance": n.acceptance, "acceptance_ids": n.acceptance_ids,
             "read_scope": n.read_scope, "write_scope": n.write_scope,
             "read_scope_declared": n.read_scope_declared,
             "write_scope_declared": n.write_scope_declared,
             "artifact_refs": n.artifact_refs, "evidence_refs": n.evidence_refs,
             "base_hashes": n.base_hashes,
             "verification": n.verification,
             "result": n.result.model_dump(mode="json") if n.result else None}
            for n in self._nodes.values()
        ]
        edges = [
            {"from": dep, "to": nid}
            for nid, deps in self._deps.items()
            for dep in deps
        ]
        return {"nodes": nodes, "edges": edges}

    @classmethod
    def from_task_list(cls, tasks: list[dict]) -> TaskDAG:
        dag = cls()
        for t in tasks:
            dag.add_task(TaskNode(
                id=t["id"],
                description=t.get("description", ""),
                depends_on=t.get("depends_on", []),
                retry_count=t.get("retry_count", 0),
                max_retries=t.get("max_retries", 2),
                error_history=t.get("error_history", []),
                acceptance=t.get("acceptance", ""),
                acceptance_ids=t.get("acceptance_ids", []),
                read_scope=t.get("read_scope", []),
                write_scope=t.get("write_scope", []),
                read_scope_declared=t.get("read_scope_declared", "read_scope" in t),
                write_scope_declared=t.get("write_scope_declared", "write_scope" in t),
                artifact_refs=t.get("artifact_refs", []),
                evidence_refs=t.get("evidence_refs", []),
                base_hashes=t.get("base_hashes", {}),
                verification=t.get("verification", {}),
                result=TaskResult.model_validate(t["result"]) if t.get("result") else None,
            ))
        return dag

    def _detect_cycle_from(self, start: str) -> list[str] | None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in self._nodes}
        parent: dict[str, str | None] = {start: None}

        def dfs(nid: str) -> list[str] | None:
            color[nid] = GRAY
            for nbr in self._adj.get(nid, []):
                if nbr not in color:
                    continue
                if color[nbr] == GRAY:
                    path = [nbr, nid]
                    cur = nid
                    while parent.get(cur) and parent[cur] != nbr:
                        cur = parent[cur]
                        path.append(cur)
                    path.append(nbr)
                    path.reverse()
                    return path
                if color[nbr] == WHITE:
                    parent[nbr] = nid
                    result = dfs(nbr)
                    if result:
                        return result
            color[nid] = BLACK
            return None

        return dfs(start)

    def _remove_node(self, nid: str) -> None:
        self._nodes.pop(nid, None)
        self._adj.pop(nid, None)
        self._deps.pop(nid, None)
        for deps in self._deps.values():
            deps.discard(nid)
        for adj_list in self._adj.values():
            while nid in adj_list:
                adj_list.remove(nid)
