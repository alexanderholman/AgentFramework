"""Independent bounded Python reference methods and separate acceptance oracles."""

from __future__ import annotations

from collections import Counter, deque
from itertools import pairwise

from af.domain.algorithms.contracts import (
    AlgorithmError,
    AlgorithmInput,
    GraphInput,
    PathInput,
    SearchInput,
    SequenceInput,
    StringInput,
)


def insertion_sort(values: tuple[int, ...]) -> list[int]:
    result = list(values)
    for index in range(1, len(result)):
        value = result[index]
        cursor = index
        while cursor > 0 and result[cursor - 1] > value:
            result[cursor] = result[cursor - 1]
            cursor -= 1
        result[cursor] = value
    return result


def merge_sort(values: tuple[int, ...]) -> list[int]:
    result = list(values)
    width = 1
    while width < len(result):
        merged: list[int] = []
        for offset in range(0, len(result), 2 * width):
            left, right = (
                result[offset : offset + width],
                result[offset + width : offset + 2 * width],
            )
            i = j = 0
            while i < len(left) and j < len(right):
                if left[i] <= right[j]:
                    merged.append(left[i])
                    i += 1
                else:
                    merged.append(right[j])
                    j += 1
            merged.extend(left[i:])
            merged.extend(right[j:])
        result = merged
        width *= 2
    return result


def binary_first(values: tuple[int, ...], target: int) -> int | None:
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if values[middle] < target:
            low = middle + 1
        else:
            high = middle
    return low if low < len(values) and values[low] == target else None


def _breadth_first(graph: GraphInput) -> tuple[list[int], dict[int, int | None]]:
    adjacency: list[list[int]] = [[] for _ in range(graph.nodes)]
    for left, right in sorted(graph.edges):
        adjacency[left].append(right)
    parents: dict[int, int | None] = {graph.start: None}
    pending = deque([graph.start])
    order: list[int] = []
    while pending:
        node = pending.popleft()
        order.append(node)
        for neighbor in adjacency[node]:
            if neighbor not in parents:
                parents[neighbor] = node
                pending.append(neighbor)
    return order, parents


def evaluate(request: AlgorithmInput) -> object:
    args = request.arguments
    name = request.algorithm
    if name == "string-matches":
        assert isinstance(args, StringInput)
        return [
            i
            for i in range(len(args.text) - len(args.pattern) + 1)
            if all(args.text[i + j] == char for j, char in enumerate(args.pattern))
        ]
    if isinstance(args, GraphInput):
        order, parents = _breadth_first(args)
        if name == "breadth-first":
            return order
        assert isinstance(args, PathInput)
        if args.target not in parents:
            return None
        path: list[int] = []
        node: int | None = args.target
        while node is not None:
            path.append(node)
            node = parents[node]
        return list(reversed(path))
    assert isinstance(args, SequenceInput)
    if name == "insertion-sort":
        return insertion_sort(args.values)
    if name == "merge-sort":
        return merge_sort(args.values)
    if name == "sum-integers":
        total = 0
        for value in args.values:
            total += value
        return total
    assert isinstance(args, SearchInput)
    if name == "binary-first":
        return binary_first(args.values, args.target)
    if name == "sorted-membership":
        return binary_first(tuple(merge_sort(args.values)), args.target) is not None
    if name == "linear-membership":
        return any(value == args.target for value in args.values)
    raise AlgorithmError("unknown installed implementation")


def _distances(graph: GraphInput) -> list[int | None]:
    """Independent bounded edge relaxation oracle, not the queue traversal implementation."""
    distances: list[int | None] = [None] * graph.nodes
    distances[graph.start] = 0
    for _ in range(graph.nodes - 1):
        changed = False
        for left, right in graph.edges:
            a, b = distances[left], distances[right]
            if a is not None and (b is None or a + 1 < b):
                distances[right] = a + 1
                changed = True
        if not changed:
            break
    return distances


def _integer_list(value: object) -> list[int] | None:
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        return None
    return value


def verify_output(request: AlgorithmInput, value: object) -> bool:
    """Accept postconditions independently; Python bool is never accepted as an integer."""
    args, name = request.arguments, request.algorithm
    if isinstance(args, StringInput):
        positions: list[int] = []
        offset = args.text.find(args.pattern)
        while offset >= 0:
            positions.append(offset)
            offset = args.text.find(args.pattern, offset + 1)
        return _integer_list(value) == positions
    if isinstance(args, GraphInput):
        distances = _distances(args)
        if isinstance(args, PathInput):
            length = distances[args.target]
            if length is None:
                return value is None
            path = _integer_list(value)
            return (
                bool(path)
                and path is not None
                and (
                    path[0] == args.start
                    and path[-1] == args.target
                    and len(path) == length + 1
                    and all(edge in args.edges for edge in pairwise(path))
                )
            )
        order = _integer_list(value)
        if not order or order[0] != args.start or len(set(order)) != len(order):
            return False
        if set(order) != {i for i, d in enumerate(distances) if d is not None}:
            return False
        levels = [distances[node] for node in order]
        # Same-level order may vary without changing the traversal contract.
        return all(a is not None and b is not None and a <= b for a, b in pairwise(levels))
    assert isinstance(args, SequenceInput)
    if name in {"insertion-sort", "merge-sort"}:
        values = _integer_list(value)
        return (
            values is not None
            and Counter(values) == Counter(args.values)
            and all(a <= b for a, b in pairwise(values))
        )
    if name == "sum-integers":
        return type(value) is int and value == sum(args.values)
    assert isinstance(args, SearchInput)
    if name == "binary-first":
        expected = next((i for i, item in enumerate(args.values) if item == args.target), None)
        return value is None if expected is None else type(value) is int and value == expected
    return type(value) is bool and value == (args.target in args.values)
