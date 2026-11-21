"""批次与可观察阶段层面的汇总。

三条硬约束：

* 分母始终是整批首次接受人数，终态、等待、原因未明全部保留；
* 同一观察阶段（horizon）才能比较，未在截止日前完整积累的批次/阶段标记
  ``observable=False``，观测数低于 ``minimum_observations`` 标记 ``mature=False``；
* 预约日结构（slot_choice）是家长自选的分层变量，只做描述性并列，不做随机可比
  推断（``self_selected=True``）。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from statistics import median
from typing import Any

from transition.journey import (
    TERMINAL_STATUS,
    WAITING_STATUS,
    Journey,
)


def _distribution(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"min": None, "median": None, "max": None, "n": 0}
    ordered = sorted(values)
    med = median(ordered)
    return {
        "min": ordered[0],
        "median": int(med),
        "max": ordered[-1],
        "n": len(ordered),
    }


def cohort_table(
    journeys: list[Journey], cutoff: date, view: str
) -> list[dict[str, Any]]:
    """每个首次接受批次一行：终态分布、等待原因与两套时间计量。"""

    rows: list[dict[str, Any]] = []
    grouped: dict[date, list[Journey]] = defaultdict(list)
    for journey in journeys:
        grouped[journey.cohort].append(journey)

    for cohort_day in sorted(grouped):
        members = grouped[cohort_day]
        metrics = [journey.metrics_at(cutoff, view) for journey in members]
        status_counts = Counter(item["status"] for item in metrics)
        waiting_reasons = Counter(
            item["waiting_reason"]
            for item in metrics
            if item["status"] == WAITING_STATUS
        )

        time_by_status: dict[str, dict[str, Any]] = {}
        for status in sorted(TERMINAL_STATUS | {WAITING_STATUS}):
            subset = [item for item in metrics if item["status"] == status]
            if not subset:
                continue
            time_by_status[status] = {
                "count": len(subset),
                "calendar_days": _distribution(
                    [item["calendar_days"] for item in subset]
                ),
                "available_opportunities": _distribution(
                    [item["available_opportunities"] for item in subset]
                ),
                "center_closed_sessions": _distribution(
                    [item["center_closed_sessions"] for item in subset]
                ),
                "family_paused_sessions": _distribution(
                    [item["family_paused_sessions"] for item in subset]
                ),
            }

        rows.append(
            {
                "cohort": cohort_day.isoformat(),
                "cutoff": cutoff.isoformat(),
                "view": view,
                "n": len(members),
                "status_counts": dict(status_counts),
                "waiting_reasons": dict(waiting_reasons),
                "pending_request_count": sum(
                    1
                    for item in metrics
                    if item["has_pending_request"]
                    and item["status"] == WAITING_STATUS
                ),
                "time_by_status": time_by_status,
                "slot_choice_breakdown": dict(
                    Counter(item["slot_choice"] for item in metrics)
                ),
                "room_breakdown": dict(
                    Counter(item["room"] for item in metrics)
                ),
            }
        )
    return rows


def _summarize_group(
    entries: list[dict[str, Any]],
    minimum_observations: int,
    self_selected: bool,
) -> dict[str, Any]:
    converted = sum(1 for entry in entries if entry["converted_by_stage_end"])
    status_counts = Counter(entry["status"] for entry in entries)
    return {
        "n": len(entries),
        "observable": all(entry["observable"] for entry in entries),
        "mature": len(entries) >= minimum_observations,
        "self_selected": self_selected,
        "converted": converted,
        "conversion_rate": round(converted / len(entries), 4) if entries else None,
        "status_counts": dict(status_counts),
        "available_opportunities": _distribution(
            [entry["available_opportunities"] for entry in entries]
        ),
        "calendar_days": _distribution(
            [entry["calendar_days"] for entry in entries]
        ),
        "center_closed_sessions": _distribution(
            [entry["center_closed_sessions"] for entry in entries]
        ),
        "family_paused_sessions": _distribution(
            [entry["family_paused_sessions"] for entry in entries]
        ),
    }


def horizon_snapshot(
    journeys: list[Journey],
    horizon_days: int,
    cutoff: date,
    view: str,
    minimum_observations: int,
) -> dict[str, Any]:
    """同一可观察阶段的横截面：整体、按房间、按预约结构、按批次。

    只有 ``observable=True`` 的孩子进入阶段统计；未成熟组保留行但带显式标记，
    不输出可直接引用的转入率结论。预约结构分层带 ``self_selected=True``。
    """

    entries = [
        journey.horizon_metrics(horizon_days, cutoff, view) for journey in journeys
    ]
    observable_entries = [entry for entry in entries if entry["observable"]]
    truncated = [entry["child"] for entry in entries if not entry["observable"]]

    def group(key: str, self_selected: bool) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in observable_entries:
            buckets[entry[key]].append(entry)
        return {
            name: _summarize_group(items, minimum_observations, self_selected)
            for name, items in sorted(buckets.items())
        }

    return {
        "horizon_days": horizon_days,
        "cutoff": cutoff.isoformat(),
        "view": view,
        "minimum_observations": minimum_observations,
        "overall": _summarize_group(
            observable_entries, minimum_observations, self_selected=False
        ),
        "by_room": group("room", self_selected=False),
        "by_slot_choice": group("slot_choice", self_selected=True),
        "by_cohort": group("cohort", self_selected=False),
        "truncated_children": truncated,
        "note": (
            "by_slot_choice 为家长自选预约日的分层，仅作描述性并列，不可当作随机可比；"
            "mature=False 或 observable 不足的组不得引用其转入率结论。"
        ),
    }


def maturity_flags(
    journeys: list[Journey],
    horizons_days: tuple[int, ...],
    cutoff: date,
    minimum_observations: int,
) -> list[dict[str, Any]]:
    """逐批次×阶段标注是否已完整积累、观测数是否达到成熟门槛。"""

    flags: list[dict[str, Any]] = []
    grouped: dict[date, list[Journey]] = defaultdict(list)
    for journey in journeys:
        grouped[journey.cohort].append(journey)
    for cohort_day in sorted(grouped):
        members = grouped[cohort_day]
        for horizon_days in horizons_days:
            observable_count = sum(
                1
                for journey in members
                if journey.horizon_observable(horizon_days, cutoff)
            )
            flags.append(
                {
                    "cohort": cohort_day.isoformat(),
                    "horizon_days": horizon_days,
                    "cohort_size": len(members),
                    "observable_n": observable_count,
                    "fully_observable": observable_count == len(members),
                    "mature": observable_count >= minimum_observations
                    and observable_count == len(members),
                }
            )
    return flags
