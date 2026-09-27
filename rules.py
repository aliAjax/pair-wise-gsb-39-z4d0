"""Pure domain rules for disruption-aware routing.

This module owns no persistence: callers pass base network data and the
changes of a single version, and receive a graph / shortest route back.
Keeping rules separate from storage (app.py) and the page (static/index.html)
lets published snapshots keep computing with their own frozen change set.
"""
from __future__ import annotations

import heapq
import math
from typing import Any, Callable, Iterable

WINDOW_MIN = 0
WINDOW_MAX = 2880

CHANGE_KINDS = {"stop_closure", "skip_stop", "detour", "accessibility_change", "stop_relocation"}


def within_window(value: int, start: int | None, end: int | None) -> bool:
    if start is None and end is None:
        return True
    if start is None or end is None:
        return False
    return start <= value <= end


def normalize_window(start: Any, end: Any) -> tuple[int | None, int | None]:
    if start is None and end is None:
        return None, None
    try:
        start_i, end_i = int(start), int(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("生效时间必须是服务日分钟数") from exc
    if start_i < WINDOW_MIN or end_i <= start_i or end_i >= WINDOW_MAX:
        raise ValueError("生效时间范围不合法")
    return start_i, end_i


def active_changes(changes: Iterable[dict[str, Any]], at_minute: int) -> list[dict[str, Any]]:
    return [c for c in changes if within_window(at_minute, c.get("effective_start_minute"), c.get("effective_end_minute"))]


def build_graph(
    stops: dict[int, dict[str, Any]],
    memberships: dict[int, list[dict[str, Any]]],
    changes: Iterable[dict[str, Any]],
    at_minute: int,
    require_accessible: bool,
) -> dict[int, list[tuple[int, int, dict[str, Any]]]]:
    """Build the riding/walking graph at one service-day minute.

    Edge metadata carries ``kind``: ``route`` for onboard riding, ``detour``
    for a replacement vehicle link and ``walk`` for the transfer between a
    relocated stop and its temporary replacement.

    A relocation substitutes the temporary stop for the original at the same
    position of every line that serves it (segment travel times are kept),
    then adds a bidirectional walking transfer. Outside the change window the
    relocation is filtered out and the original stop is served again.

    When the temporary stop is not accessible, accessible routing must not
    push wheelchair passengers onto it: the substitution is suppressed, the
    original stop cannot be boarded either (vehicles do not stop there), and
    the search falls back to other rideable stops/lines instead.
    """
    windowed = active_changes(changes, at_minute)
    closed: set[int] = set()
    inaccessible: set[int] = set()
    detours: list[dict[str, Any]] = []
    # original stop id -> (temporary stop id, walking minutes, change id)
    relocations: dict[int, tuple[int, int, int]] = {}
    for change in windowed:
        kind = change["kind"]
        if kind in {"stop_closure", "skip_stop"}:
            closed.add(int(change["stop_id"]))
        elif kind == "accessibility_change" and not bool(change.get("accessible")):
            inaccessible.add(int(change["stop_id"]))
        elif kind == "detour":
            detours.append(change)
        elif kind == "stop_relocation":
            original = int(change["stop_id"])
            relocations[original] = (int(change["to_stop_id"]), int(change["travel_minutes"]), int(change["id"]))

    def usable(stop_id: int) -> bool:
        """Whether a passenger may board/alight at this stop."""
        if stop_id in inaccessible:
            return not require_accessible
        stop = stops.get(stop_id)
        if stop is None or stop_id in closed:
            return False
        if require_accessible and not bool(stop.get("accessible")):
            return False
        return True

    # Only relocations whose temporary platform is actually usable take
    # effect. The rest leave the original stop unservable for this query so
    # routing fails over to another corridor rather than inventing a stop.
    redirect: dict[int, int] = {}
    walk_legs: list[tuple[int, int, int, int]] = []
    for original, (temporary, minutes, change_id) in relocations.items():
        if not usable(temporary):
            continue
        redirect[original] = temporary
        walk_legs.append((original, temporary, minutes, change_id))

    graph: dict[int, list[tuple[int, int, dict[str, Any]]]] = {sid: [] for sid in stops}

    for line_id, rows in memberships.items():
        if not rows:
            continue
        ordered = sorted(rows, key=lambda r: int(r["sequence"]))
        # Effective boarding nodes in line order. A relocated original stop is
        # replaced by its temporary platform; closed/inaccessible stops and
        # unusable relocations are dropped (the vehicle still passes them, so
        # travel time over the gap rolls into the next riding edge).
        nodes: list[tuple[int, dict[str, Any]]] = []
        seen: set[int] = set()
        for row in ordered:
            original = int(row["stop_id"])
            effective = redirect.get(original, original)
            if effective in relocations and effective not in redirect:
                continue
            if not usable(effective):
                continue
            if effective in seen:
                continue
            seen.add(effective)
            nodes.append((effective, row))
        for (cur_id, current), (nxt_id, nxt) in zip(nodes, nodes[1:]):
            between = [r for r in ordered if int(current["sequence"]) < int(r["sequence"]) <= int(nxt["sequence"])]
            minutes = sum(int(r["travel_minutes_from_previous"]) for r in between)
            edge = {"line_id": line_id, "kind": "route",
                    "from_sequence": current["sequence"], "to_sequence": nxt["sequence"]}
            graph[cur_id].append((nxt_id, minutes, edge))
            graph[nxt_id].append((cur_id, minutes, {**edge,
                                                   "from_sequence": nxt["sequence"],
                                                   "to_sequence": current["sequence"]}))

    # Walking transfers, usable in both directions regardless of line direction.
    for original, temporary, minutes, change_id in walk_legs:
        meta = {"kind": "walk", "change_id": change_id,
                "original_stop_id": original, "temporary_stop_id": temporary}
        graph[original].append((temporary, minutes, {**meta, "direction": "to_temporary"}))
        graph[temporary].append((original, minutes, {**meta, "direction": "to_original"}))

    for change in detours:
        src = int(change["from_stop_id"])
        dst = int(change["to_stop_id"])
        minutes = int(change["travel_minutes"])
        if src in relocations or dst in relocations or not usable(src) or not usable(dst):
            continue
        meta = {"line_id": change.get("line_id"), "kind": "detour", "change_id": change["id"]}
        graph[src].append((dst, minutes, meta))
        graph[dst].append((src, minutes, meta))

    return graph


def shortest_path(
    graph: dict[int, list[tuple[int, int, dict[str, Any]]]],
    from_stop_id: int,
    to_stop_id: int,
) -> tuple[int | None, list[int], list[dict[str, Any]]]:
    queue: list[tuple[int, int]] = [(0, from_stop_id)]
    distance = {from_stop_id: 0}
    predecessor: dict[int, tuple[int, dict[str, Any]]] = {}
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != distance.get(node):
            continue
        if node == to_stop_id:
            break
        for nxt, weight, edge in graph.get(node, []):
            new_cost = cost + weight
            if new_cost < distance.get(nxt, math.inf):
                distance[nxt] = new_cost
                predecessor[nxt] = (node, edge)
                heapq.heappush(queue, (new_cost, nxt))
    if to_stop_id not in distance:
        return None, [], []
    path = [to_stop_id]
    legs: list[dict[str, Any]] = []
    cursor = to_stop_id
    while cursor != from_stop_id:
        previous, edge = predecessor[cursor]
        legs.append({"from_stop_id": previous, "to_stop_id": cursor,
                     "minutes": distance[cursor] - distance[previous], **edge})
        path.append(previous)
        cursor = previous
    path.reverse()
    legs.reverse()
    return distance[to_stop_id], path, legs


def validate_relocation(payload: dict[str, Any], stop_exists: Callable[[int], bool]) -> dict[str, Any]:
    """Validate a stop_relocation change payload, returning normalized fields."""
    original = payload.get("stop_id")
    temporary = payload.get("to_stop_id")
    minutes = payload.get("travel_minutes")
    if original is None or temporary is None or minutes is None:
        raise ValueError("站点迁移需要原站 stop_id、临时站 to_stop_id 和步行分钟 travel_minutes")
    try:
        original_i, temporary_i, minutes_i = int(original), int(temporary), int(minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError("站点和步行分钟必须是整数") from exc
    if original_i == temporary_i:
        raise ValueError("临时站点不能与原站相同")
    if minutes_i < 0:
        raise ValueError("步行时间不能为负")
    if not stop_exists(original_i) or not stop_exists(temporary_i):
        raise LookupError("站点不存在")
    start, end = normalize_window(payload.get("effective_start_minute"), payload.get("effective_end_minute"))
    return {"stop_id": original_i, "to_stop_id": temporary_i,
            "travel_minutes": minutes_i,
            "effective_start_minute": start, "effective_end_minute": end}
