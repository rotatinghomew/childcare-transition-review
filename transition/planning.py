"""容量台账与两种衔接安排试算。

关键口径：

* 容量区分 ``regular_seats`` 与 ``trial_seats``：短试读能塞进的空位不等于可以
  答应长期班；常规承接按"连续四场"核对（calendar 中该房间场次的连续四个开放日）。
* 已确认的 reserved 按登记日起生效，先于同日接收处理；同一位子由同一孩子持有时
  合并计数，不重复占容量。
* 接收承诺一旦落账即稳定：迟到补登的回执不能要求已安顿家庭重新争位；新承诺采用
  全有或全无的可行性判断，任一场容量或教师冲突即整体不可承诺，并写明原因。
* 同一区间多个班指望同一位教师时，台账统一核对，直接指出冲突双方，而不是各班
  单独估计都说能接。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from transition.io_utils import Bundle, CapacityRow, Reservation
from transition.journey import Journey, WAITING_STATUS


@dataclass
class HoldAttempt:
    child: str
    room: str
    slot: str
    source: str  # reservation / admission / proposed_opening
    effective_date: date
    window_dates: tuple[str, ...]
    feasible: bool
    reasons: tuple[str, ...]


@dataclass
class CapacityLedger:
    bundle: Bundle
    # (room, slot) -> 有序开放日
    open_dates: dict[tuple[str, str], list[date]] = field(init=False)
    capacity_index: dict[tuple[str, str], CapacityRow] = field(init=False)
    # (room, slot, day) -> 持有孩子集合
    holds: dict[tuple[str, str, date], set[str]] = field(init=False, default_factory=lambda: defaultdict(set))
    attempts: list[HoldAttempt] = field(init=False, default_factory=list)
    # 被拒绝的尝试仍保留，用于呈现"哪个承诺无法兑现"的跨房间冲突
    rejected: list[HoldAttempt] = field(init=False, default_factory=list)
    # 已安顿（成功持有常规承诺）的孩子，迟到回执不得抢占
    settled_children: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        self.open_dates = defaultdict(list)
        for row in self.bundle.calendar:
            if row["open"]:
                self.open_dates[(row["room"], row["session"])].append(row["day"])
        for key in self.open_dates:
            self.open_dates[key].sort()
        self.capacity_index = {
            (row.room, row.slot): row for row in self.bundle.capacity
        }

    # ---- 四周连续窗口 ---------------------------------------------------

    def four_week_window(self, room: str, slot: str, start: date) -> tuple[date, ...]:
        """从 start 起该房间场次的连续四个开放日（连续承接核对窗口）。"""

        days = self.open_dates[(room, slot)]
        window = [day for day in days if day >= start]
        if len(window) < 4:
            raise ValueError(
                f"{room}/{slot} 自 {start} 起不足四个开放日，无法核对四周承接"
            )
        return tuple(window[:4])

    # ---- 容量与教师核对 -------------------------------------------------

    def _teacher_conflicts(
        self, room: str, slot: str, day: date, child: str
    ) -> list[str]:
        """同一日历日、同一场次标签下，同教师跨房间的占用冲突。"""

        cap = self.capacity_index.get((room, slot))
        if cap is None:
            return [f"无 {room}/{slot} 的容量配置"]
        conflicts: list[str] = []
        for other_room in self.bundle.staff.get(cap.teacher, ()):
            if other_room == room:
                continue
            holders = self.holds.get((other_room, slot, day), set())
            other_children = holders - {child}
            if other_children:
                conflicts.append(
                    f"{day} {cap.teacher} 已在{other_room}房间承担{slot}（"
                    + "、".join(sorted(other_children))
                    + "），无法同时覆盖{room}房间".format(room=room)
                )
        return conflicts

    def check_feasible(
        self, room: str, slot: str, window: tuple[date, ...], child: str
    ) -> tuple[bool, tuple[str, ...]]:
        """全有或全无：四场中任一场超容量或教师冲突即整体不可承诺。"""

        cap = self.capacity_index.get((room, slot))
        if cap is None:
            return False, (f"无 {room}/{slot} 的容量配置",)
        reasons: list[str] = []
        for day in window:
            holders = self.holds.get((room, slot, day), set())
            # 同一孩子重复持有（保留延长既有接收）不重复计数。
            other = holders - {child}
            if len(other) >= cap.regular_seats:
                reasons.append(
                    f"{day} {room}/{slot} 常规位 {cap.regular_seats} 个已满"
                    f"（在持：{'、'.join(sorted(other))}）"
                )
            reasons.extend(self._teacher_conflicts(room, slot, day, child))
        return (not reasons), tuple(reasons)

    def commit(
        self,
        child: str,
        room: str,
        slot: str,
        source: str,
        effective_date: date,
    ) -> HoldAttempt:
        """尝试落账一个四周连续承诺；不可行时不写入任何占用。"""

        window = self.four_week_window(room, slot, effective_date)
        feasible, reasons = self.check_feasible(room, slot, window, child)
        attempt = HoldAttempt(
            child=child,
            room=room,
            slot=slot,
            source=source,
            effective_date=effective_date,
            window_dates=tuple(day.isoformat() for day in window),
            feasible=feasible,
            reasons=reasons,
        )
        self.attempts.append(attempt)
        if feasible:
            for day in window:
                self.holds[(room, slot, day)].add(child)
            self.settled_children.add(child)
        else:
            self.rejected.append(attempt)
        return attempt

    # ---- 占用导出 -------------------------------------------------------

    def regular_occupancy(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (room, slot), cap in sorted(self.capacity_index.items()):
            for day in self.open_dates[(room, slot)]:
                holders = sorted(self.holds.get((room, slot, day), set()))
                if not holders:
                    continue
                rows.append(
                    {
                        "day": day.isoformat(),
                        "room": room,
                        "slot": slot,
                        "regular_seats": cap.regular_seats,
                        "held": len(holders),
                        "free": cap.regular_seats - len(holders),
                        "holders": holders,
                    }
                )
        return rows

    def trial_slack(self, journeys: list[Journey]) -> list[dict[str, Any]]:
        """试读位视角的剩余空间（短试读空位不可直接当作长期承诺）。

        试读占用按截止前仍在等/观察中的孩子，在其接受场次的开放日上估算；
        已安顿家庭不再占试读位。
        """

        cutoff = max(self.bundle.review.cutoffs)
        trial_used: dict[tuple[str, str, date], set[str]] = defaultdict(set)
        for journey in journeys:
            if journey.child_id in self.settled_children:
                continue
            if journey.status_at(cutoff, "corrected") != WAITING_STATUS:
                continue
            for (day, room, slot), is_open in journey.open_sessions.items():
                if (
                    is_open
                    and room == journey.room
                    and slot in journey.accepted_slots
                    and day >= journey.cohort
                ):
                    trial_used[(room, slot, day)].add(journey.child_id)

        rows: list[dict[str, Any]] = []
        for (room, slot), cap in sorted(self.capacity_index.items()):
            for day in self.open_dates[(room, slot)]:
                if day < cutoff:
                    continue
                used = len(trial_used.get((room, slot, day), set()))
                rows.append(
                    {
                        "day": day.isoformat(),
                        "room": room,
                        "slot": slot,
                        "trial_seats": cap.trial_seats,
                        "trial_used_estimate": used,
                        "trial_free_estimate": max(cap.trial_seats - used, 0),
                    }
                )
        return rows

    def teacher_conflict_report(self) -> list[dict[str, Any]]:
        """跨房间教师冲突：同一场次日多个班指望同一位教师的承诺对。

        包含两类：已落账占用之间的冲突（数据中通常为空，因为提交时已被拦截），
        以及被拒绝尝试与既有占用之间的冲突——后者明确指出哪个承诺无法兑现。
        """

        # (day, slot, teacher) -> room -> children
        index: dict[tuple[date, str, str], dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for (room, slot, day), children in self.holds.items():
            cap = self.capacity_index.get((room, slot))
            if cap:
                for child in children:
                    index[(day, slot, cap.teacher)][room].add(child)

        report: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for (day, slot, teacher), rooms in sorted(index.items()):
            if len(rooms) > 1:
                key = (day.isoformat(), slot, teacher, ",".join(sorted(rooms)))
                seen.add(key)
                report.append(
                    {
                        "day": day.isoformat(),
                        "slot": slot,
                        "teacher": teacher,
                        "rooms": {
                            room: sorted(children) for room, children in rooms.items()
                        },
                        "unkeepable": f"{teacher} 无法同时覆盖 {'、'.join(sorted(rooms))}",
                        "rejected_commitments": [],
                    }
                )

        # 被拒绝的跨房间承诺：把原因中的教师冲突结构化呈现。
        for attempt in self.rejected:
            teacher_reasons = [
                reason for reason in attempt.reasons if "教师" in reason
            ]
            if not teacher_reasons:
                continue
            cap = self.capacity_index[(attempt.room, attempt.slot)]
            for reason in teacher_reasons:
                # 原因文本以冲突日 ISO 日期开头，直接解析回日历日。
                day = date.fromisoformat(reason.split(" ", 1)[0])
                rooms = dict(index.get((day, attempt.slot, cap.teacher), {}))
                rooms.setdefault(attempt.room, set()).add(attempt.child)
                report.append(
                    {
                        "day": day.isoformat(),
                        "slot": attempt.slot,
                        "teacher": cap.teacher,
                        "rooms": {
                            room: sorted(children) for room, children in rooms.items()
                        },
                        "unkeepable": reason,
                        "rejected_commitments": [
                            {
                                "child": attempt.child,
                                "room": attempt.room,
                                "slot": attempt.slot,
                            }
                        ],
                    }
                )
        report.sort(key=lambda item: (item["day"], item["slot"], item["teacher"]))
        return report


# ---- 基线承诺装载 ---------------------------------------------------------

def build_baseline_ledger(bundle: Bundle, journeys: list[Journey]) -> CapacityLedger:
    """按时间顺序装载已确认保留与全部常规班接收（修正口径）。

    保留按登记日（from_date）起生效；接收按发生日落账。同效日先装保留，
    保证保留时段被优先兑现。
    """

    ledger = CapacityLedger(bundle=bundle)
    journey_index = {journey.child_id: journey for journey in journeys}

    actions: list[tuple[date, int, str, Any]] = []
    for reservation in bundle.review.reservations:
        actions.append((reservation.from_date, 0, "reservation", reservation))
    for journey in journeys:
        for admission in journey.admissions:
            actions.append(
                (
                    admission["date"],
                    1,
                    "admission",
                    admission | {"child": journey.child_id},
                )
            )

    def _sort_key(item: tuple[date, int, str, Any]) -> tuple[date, int, str]:
        payload = item[3]
        child = payload.child if isinstance(payload, Reservation) else payload["child"]
        return (item[0], item[1], child)

    for _day, _order, kind, payload in sorted(actions, key=_sort_key):
        if kind == "reservation":
            reservation: Reservation = payload
            ledger.commit(
                child=reservation.child,
                room=reservation.room,
                slot=reservation.slot,
                source="reservation",
                effective_date=reservation.from_date,
            )
        else:
            ledger.commit(
                child=payload["child"],
                room=payload["room"],
                slot=payload["seat"],
                source="admission",
                effective_date=payload["date"],
            )

    return ledger, journey_index


# ---- 两种安排试算 ---------------------------------------------------------

def plan_extension(
    bundle: Bundle, journeys: list[Journey]
) -> dict[str, Any]:
    """安排一：延长过渡，不新增常规承诺，只在试读位内安排等待家庭。"""

    ledger, _ = build_baseline_ledger(bundle, journeys)
    cutoff = max(bundle.review.cutoffs)
    waiting = [
        journey
        for journey in journeys
        if journey.status_at(cutoff, "corrected") == WAITING_STATUS
    ]
    trial_slack = ledger.trial_slack(journeys)

    placement_rows = []
    for journey in waiting:
        reason = journey.waiting_reason_at(cutoff, "corrected")
        # 试读位按该孩子已接受场次的最近开放日核对；不产生任何常规承诺。
        options = []
        for slot in journey.accepted_slots:
            cap = ledger.capacity_index.get((journey.room, slot))
            if cap is None:
                continue
            future_open = [
                day.isoformat()
                for day in ledger.open_dates[(journey.room, slot)]
                if day >= cutoff
            ][:4]
            options.append(
                {
                    "slot": slot,
                    "family_accepted": True,
                    "next_open_days": future_open,
                    "trial_seats": cap.trial_seats,
                    "regular_seats": cap.regular_seats,
                    "note": "仅可安排延长试读；试读空位不构成常规班承诺",
                }
            )
        placement_rows.append(
            {
                "child": journey.child_id,
                "room": journey.room,
                "waiting_reason": reason,
                "accepted_slot_options": options,
                "regular_commitment_offered": False,
            }
        )

    return {
        "scenario": "延长过渡",
        "cutoff": cutoff.isoformat(),
        "baseline_attempts": [_attempt_to_dict(item) for item in ledger.attempts],
        "overbooked_admissions": [
            _attempt_to_dict(item)
            for item in ledger.attempts
            if item.source == "admission" and not item.feasible
        ],
        "teacher_conflicts": ledger.teacher_conflict_report(),
        "trial_slack": trial_slack,
        "waiting_placements": placement_rows,
        "new_regular_commitments": [],
        "principle": "已答应保留的时段继续保留；不新增常规承诺，等待家庭只在试读位内延长。",
    }


def plan_regular_openings(
    bundle: Bundle,
    journeys: list[Journey],
    openings: int = 2,
) -> dict[str, Any]:
    """安排二：开放少量常规预约，候选承诺逐个做四周连续与教师冲突核对。

    openings 为拟新增的常规预约名额上限；只对家长已同意过的时段给出可承诺项，
    未同意时段仅作问询建议。
    """

    ledger, _ = build_baseline_ledger(bundle, journeys)
    cutoff = max(bundle.review.cutoffs)
    waiting = [
        journey
        for journey in journeys
        if journey.status_at(cutoff, "corrected") == WAITING_STATUS
    ]

    proposals: list[dict[str, Any]] = []
    offered = 0
    for journey in waiting:
        for slot in journey.accepted_slots:
            if offered >= openings:
                break
            start = cutoff
            window = ledger.four_week_window(journey.room, slot, start)
            feasible, reasons = ledger.check_feasible(
                journey.room, slot, window, journey.child_id
            )
            proposal = {
                "child": journey.child_id,
                "room": journey.room,
                "slot": slot,
                "family_accepted": True,
                "window_dates": tuple(day.isoformat() for day in window),
                "feasible": feasible,
                "reasons": reasons,
                "kind": "commit_candidate" if feasible else "unkeepable",
            }
            if feasible:
                ledger.commit(
                    child=journey.child_id,
                    room=journey.room,
                    slot=slot,
                    source="proposed_opening",
                    effective_date=start,
                )
                offered += 1
            proposals.append(proposal)
        if offered >= openings:
            break

    # 家长未同意过的时段：只作为问询建议，绝不进入承诺台账。
    inquiry_suggestions = []
    all_slots = sorted({row.slot for row in bundle.capacity})
    for journey in waiting:
        for slot in all_slots:
            if slot not in journey.accepted_slots:
                inquiry_suggestions.append(
                    {
                        "child": journey.child_id,
                        "room": journey.room,
                        "slot": slot,
                        "family_accepted": False,
                        "kind": "inquiry_only",
                        "note": "家长未同意过该时段，仅可问询，不得直接承诺",
                    }
                )

    return {
        "scenario": "开放少量常规预约",
        "cutoff": cutoff.isoformat(),
        "requested_openings": openings,
        "committed_openings": offered,
        "baseline_attempts": [_attempt_to_dict(item) for item in ledger.attempts],
        "overbooked_admissions": [
            _attempt_to_dict(item)
            for item in ledger.attempts
            if item.source == "admission" and not item.feasible
        ],
        "teacher_conflicts": ledger.teacher_conflict_report(),
        "proposals": proposals,
        "inquiry_suggestions": inquiry_suggestions,
        "regular_occupancy": ledger.regular_occupancy(),
        "principle": (
            "常规承接按四周连续核对；任一承诺不可兑现即整体拒绝并写明原因；"
            "未同意时段只作问询。"
        ),
    }


def _attempt_to_dict(attempt: HoldAttempt) -> dict[str, Any]:
    return {
        "child": attempt.child,
        "room": attempt.room,
        "slot": attempt.slot,
        "source": attempt.source,
        "effective_date": attempt.effective_date.isoformat(),
        "window_dates": list(attempt.window_dates),
        "feasible": attempt.feasible,
        "reasons": list(attempt.reasons),
    }


def apply_late_receipt(
    bundle: Bundle,
    journeys: list[Journey],
    child_id: str,
    receipt_date: date,
    reported_date: date,
    room: str,
    slot: str,
) -> dict[str, Any]:
    """把一条迟到的接收回执应用到当前台账。

    返回三块结果，对应题面的三个要求：

    * ``explanation_correction``：登记口径下该孩子的旧解释 → 修正口径下的新状态；
    * ``seat_taken``：回执是否真的占班（四周连续窗口可行才占班）；
    * ``future_availability``：占班后受影响场次的剩余常规位；无论是否占班，
      已安顿家庭都不会被要求重新争位。

    ``receipt_date`` 为回执记载的发生日，``reported_date`` 为实际补登日。
    """

    if reported_date < receipt_date:
        raise ValueError("补登登记日不能早于回执发生日")
    journey = next(item for item in journeys if item.child_id == child_id)
    # "补登前"快照取补登日前一天：此时该回执在登记口径下尚不可见。
    before_day = reported_date - timedelta(days=1)
    old_registered = journey.status_at(before_day, "registered")
    old_corrected = journey.status_at(reported_date, "corrected")

    ledger, _ = build_baseline_ledger(bundle, journeys)
    already_settled = child_id in ledger.settled_children

    attempt = ledger.commit(
        child=child_id,
        room=room,
        slot=slot,
        source="late_receipt",
        effective_date=receipt_date,
    )

    affected = [
        row
        for row in ledger.regular_occupancy()
        if row["room"] == room
        and row["slot"] == slot
        and row["day"] in attempt.window_dates
    ]
    return {
        "child": child_id,
        "receipt_event_date": receipt_date.isoformat(),
        "receipt_reported_date": reported_date.isoformat(),
        "explanation_correction": {
            "snapshot_before_day": before_day.isoformat(),
            "registered_view_before": old_registered,
            "corrected_view_after": old_corrected,
            "note": (
                f"按发生日 {receipt_date.isoformat()} 修正过渡解释；"
                "登记口径快照保留用于向家长说明此前答复依据。"
            ),
        },
        "already_settled": already_settled,
        "seat_taken": attempt.feasible,
        "attempt": _attempt_to_dict(attempt),
        "future_availability": affected,
        "settled_families_protected": sorted(ledger.settled_children),
        "principle": (
            "已安顿家庭保持原持有，不参与重新竞争；回执无法占班时只更新解释与候补顺序，"
            "不挤占任何在位子。"
        ),
    }
