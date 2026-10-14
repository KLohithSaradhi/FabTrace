from __future__ import annotations

from collections import deque

from fabtrace.products.models import ProductRecipe, ValidationIssue


def _issue(code: str, message: str, path: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, path=path)


def validate_for_publish(recipe: ProductRecipe) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    node_ids = set(recipe.nodes)
    output_ids = [
        node_id
        for node_id, node in recipe.nodes.items()
        if node.kind == "output"
    ]
    if len(output_ids) != 1:
        issues.append(
            _issue(
                "output_count",
                "published products require exactly one final-output node",
                "nodes",
            )
        )
    elif recipe.output_node_id != output_ids[0]:
        issues.append(
            _issue(
                "output_mismatch",
                "output_node_id must identify the final-output node",
                "output_node_id",
            )
        )

    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    referenced_by: dict[str, set[str]] = {
        node_id: set() for node_id in node_ids
    }
    for node_id, node in recipe.nodes.items():
        if not node.unit:
            issues.append(
                _issue(
                    "missing_unit",
                    f"node {node_id!r} requires an output unit",
                    f"nodes.{node_id}.unit",
                )
            )
        if node.kind == "process" and (
            node.duration_days is None or node.duration_days <= 0
        ):
            issues.append(
                _issue(
                    "missing_duration",
                    f"process {node_id!r} requires a positive duration",
                    f"nodes.{node_id}.duration_days",
                )
            )
        if node.kind == "material" and (
            node.lead_time_days is None or node.lead_time_days <= 0
        ):
            issues.append(
                _issue(
                    "missing_lead_time",
                    f"material {node_id!r} requires a positive lead time",
                    f"nodes.{node_id}.lead_time_days",
                )
            )
        if node.kind in {"material", "process"} and not node.provider_candidates:
            issues.append(
                _issue(
                    "missing_provider",
                    f"{node.kind} {node_id!r} requires a provider candidate",
                    f"nodes.{node_id}.provider_candidates",
                )
            )
        for index, requirement in enumerate(node.requires):
            required_id = requirement.node_id
            requirement_path = f"nodes.{node_id}.requires.{index}"
            if required_id not in node_ids:
                issues.append(
                    _issue(
                        "missing_reference",
                        f"required node {required_id!r} does not exist",
                        f"{requirement_path}.node_id",
                    )
                )
                continue
            if required_id == node_id:
                issues.append(
                    _issue(
                        "self_dependency",
                        f"node {node_id!r} cannot require itself",
                        requirement_path,
                    )
                )
                continue
            required_node = recipe.nodes[required_id]
            if required_node.unit and requirement.unit != required_node.unit:
                issues.append(
                    _issue(
                        "unit_mismatch",
                        f"edge unit {requirement.unit!r} does not match "
                        f"{required_id!r} output unit {required_node.unit!r}",
                        f"{requirement_path}.unit",
                    )
                )
            if node_id not in adjacency[required_id]:
                adjacency[required_id].add(node_id)
                indegree[node_id] += 1
                referenced_by[required_id].add(node_id)

    queue = deque(
        sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    )
    visited: list[str] = []
    while queue:
        current = queue.popleft()
        visited.append(current)
        for dependent in sorted(adjacency[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(visited) != len(node_ids):
        issues.append(
            _issue(
                "cycle",
                "the dependency graph must not contain a cycle",
                "nodes",
            )
        )

    if len(output_ids) == 1:
        output_id = output_ids[0]
        if referenced_by[output_id]:
            issues.append(
                _issue(
                    "output_not_terminal",
                    "the final-output node cannot be required by another node",
                    f"nodes.{output_id}",
                )
            )
        reachable: set[str] = set()
        stack = [output_id]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(
                requirement.node_id
                for requirement in recipe.nodes[current].requires
                if requirement.node_id in recipe.nodes
            )
        for orphan_id in sorted(node_ids - reachable):
            issues.append(
                _issue(
                    "orphan_node",
                    f"node {orphan_id!r} does not contribute to the output",
                    f"nodes.{orphan_id}",
                )
            )
    return issues
