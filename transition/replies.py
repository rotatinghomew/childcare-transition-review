"""给家长的答复生成：继续、候补、暂不能安排。

每条答复都显式区分：

* ``committed``：园方当下能承诺的具体范围（房间/场次/生效日/连续窗口）；
* ``uncertainty``：仍未能确定的部分及有证据的原因；
* ``inquiry_only``：家长未同意过、只能问询的时段。

迟到接收回执在修正口径下会改写该孩子的答复（从等待类变为已安顿），并且不要求
其重新争位；登记口径快照保留，便于向家长解释"过去为什么那样答复"。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from transition.journey import (
    WAIT_REASON_FAMILY_PAUSE,
    WAIT_REASON_OBSERVING,
    WAIT_REASON_PENDING_RECEIPT,
    WAITING_STATUS,
    Journey,
)

REPLY_CONTINUE = "继续"
REPLY_WAITLIST = "候补"
REPLY_UNAVAILABLE = "暂不能安排"
REPLY_SETTLED = "已安顿"

REASON_TEXT = {
    WAIT_REASON_PENDING_RECEIPT: "转班申请已登记，常规班回执尚未到达",
    WAIT_REASON_FAMILY_PAUSE: "家庭主动暂停试读中，恢复日后继续累计试读机会",
    WAIT_REASON_OBSERVING: "仍处于试读观察阶段，尚未出现终态事件",
}


def _uncertainty_for(journey: Journey, cutoff: date, view: str) -> str:
    reason = journey.waiting_reason_at(cutoff, view)
    return REASON_TEXT.get(reason, "截止资料中没有可确定终态的观测")


def build_replies(
    journeys: list[Journey],
    cutoff: date,
    view: str,
    planning_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按截止口径为每个孩子生成一条家长答复。

    planning_result 可选；提供开放常规预约试算结果时，把可行候选承诺写入
    ``committed``，不可行原因写入 ``uncertainty``。
    """

    proposal_index: dict[str, dict[str, Any]] = {}
    if planning_result and "proposals" in planning_result:
        for proposal in planning_result["proposals"]:
            proposal_index.setdefault(proposal["child"], proposal)

    replies: list[dict[str, Any]] = []
    for journey in sorted(journeys, key=lambda item: item.child_id):
        status = journey.status_at(cutoff, view)
        base: dict[str, Any] = {
            "child": journey.child_id,
            "cohort": journey.cohort.isoformat(),
            "room": journey.room,
            "view": view,
            "cutoff": cutoff.isoformat(),
            "accepted_slots": list(journey.accepted_slots),
        }

        if status == "常规班实际接收":
            admission = min(
                journey.admissions,
                key=lambda event: event["date"],
            )
            replies.append(
                base
                | {
                    "reply": REPLY_SETTLED,
                    "committed": [
                        {
                            "room": admission["room"],
                            "slot": admission["seat"],
                            "effective_date": admission["date"].isoformat(),
                            "basis": "常规班实际接收回执（按发生日占班）",
                        }
                    ],
                    "uncertainty": [],
                    "inquiry_only": [],
                    "explanation": "已安顿家庭的位子保持稳定，不因迟到补登或新安排重新竞争。",
                }
            )
            continue

        if status in {"另选服务", "家长撤回", "原因未明"}:
            label = {
                "另选服务": "家庭已明确改去其他服务",
                "家长撤回": "家长已主动撤回申请",
                "原因未明": "结束已发生但原因尚未确定，保持独立分类待补充证据",
            }[status]
            replies.append(
                base
                | {
                    "reply": REPLY_UNAVAILABLE,
                    "committed": [],
                    "uncertainty": [label],
                    "inquiry_only": [],
                    "explanation": "初次批次名额不因此从分母抹除；后续若回流，按新试读段登记。",
                }
            )
            continue

        # 仍在等：根据等待原因与试算可行性决定继续 / 候补 / 暂不能安排。
        uncertainty = _uncertainty_for(journey, cutoff, view)
        proposal = proposal_index.get(journey.child_id)
        inquiry = [
            item
            for item in (planning_result or {}).get("inquiry_suggestions", [])
            if item["child"] == journey.child_id
        ]

        if proposal and proposal["feasible"]:
            reply_kind = REPLY_WAITLIST
            committed = [
                {
                    "room": proposal["room"],
                    "slot": proposal["slot"],
                    "window_dates": list(proposal["window_dates"]),
                    "basis": "开放常规预约试算可行，待家长确认后转为承诺",
                }
            ]
            uncertainty_lines = [uncertainty]
        elif proposal and not proposal["feasible"]:
            reply_kind = REPLY_UNAVAILABLE
            committed = []
            uncertainty_lines = [uncertainty, *proposal["reasons"]]
        else:
            # 无新增常规名额的安排：试读可继续，常规班暂不可承诺。
            reply_kind = REPLY_CONTINUE
            committed = [
                {
                    "room": journey.room,
                    "slots": list(journey.accepted_slots),
                    "basis": "延长过渡：仅承诺试读位内的继续安排，不构成常规班承诺",
                }
            ]
            uncertainty_lines = [uncertainty]

        replies.append(
            base
            | {
                "reply": reply_kind,
                "committed": committed,
                "uncertainty": uncertainty_lines,
                "inquiry_only": inquiry,
                "explanation": (
                    "可承诺范围与未确定原因分列；问询建议未获家长同意前不得承诺。"
                ),
            }
        )

    return replies


def compare_views(
    journeys: list[Journey],
    cutoff: date,
    planning_result: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """同一截止日的登记口径与修正口径答复对照，用于迟到回执的解释纠正。"""

    return {
        "registered": build_replies(journeys, cutoff, "registered", planning_result),
        "corrected": build_replies(journeys, cutoff, "corrected", planning_result),
    }
