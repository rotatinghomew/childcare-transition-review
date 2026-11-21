"""单个孩子的多段经过与时间计量。

设计原则（对应业务口径）：

1. 入口固定为 children 中的首次接受批次 cohort，中途离开、撤回、原因未明的孩子
   始终保留在分母里，不做幸存者筛选。
2. 每个截止日支持两种口径：
   - ``registered``：只承认登记日（reported）不晚于截止日的事件，即工作人员当时
     实际能看到的信息；
   - ``corrected``：按发生日（date）回看，迟到登记的事件补入后的修正口径。
3. 时间同时保留两套互不可替代的计量：
   - 日历等待：cohort 到计量端点的自然日；
   - 实际可用试读机会：calendar 中该孩子房间、家长已接受场次且开放、又不落在
     家庭暂停区间内的场次。园方停开（open=false）与家庭主动暂停分别计数，不互换。
4. ``申请转班`` 不是终态；``另次试读`` 是独立的后续段，不延长初次批次的观察。
5. ``原因未明`` 单独成类，绝不并入家长撤回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from transition.io_utils import (
    Bundle,
    FAMILY_PAUSE,
    FAMILY_RESUME,
    REQUEST_KIND,
    RETURN_TRIAL,
    TERMINAL_KINDS_RAW,
    TRIAL_ATTEND,
)

# 对外暴露的状态标签（终态 + 截止时仍在等）。
TERMINAL_KINDS = TERMINAL_KINDS_RAW
TERMINAL_STATUS = frozenset(TERMINAL_KINDS_RAW)
WAITING_STATUS = "仍在等"

WAIT_REASON_PENDING_RECEIPT = "待常规班回执"
WAIT_REASON_FAMILY_PAUSE = "家庭主动暂停中"
WAIT_REASON_OBSERVING = "仍在试读观察中"


@dataclass(frozen=True)
class PauseInterval:
    """家庭主动暂停的闭开区间 [start, end)：恢复日当天视为可试读。"""

    start: date
    end: date

    def covers(self, day: date) -> bool:
        return self.start <= day < self.end


@dataclass
class Journey:
    child_id: str
    cohort: date
    room: str
    accepted_slots: tuple[str, ...]
    events: list[dict[str, Any]]
    pause_intervals: tuple[PauseInterval, ...]
    open_sessions: dict[tuple[date, str], bool]
    anomalies: list[dict[str, Any]] = field(default_factory=list)

    # ---- 事件视图 -------------------------------------------------------

    @property
    def requests(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["kind"] == REQUEST_KIND]

    @property
    def terminal_events(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["kind"] in TERMINAL_KINDS]

    @property
    def first_terminal(self) -> dict[str, Any] | None:
        return self.terminal_events[0] if self.terminal_events else None

    @property
    def admissions(self) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event["kind"] == "常规班实际接收"
        ]

    @property
    def return_trials(self) -> list[dict[str, Any]]:
        """另次试读事件，并标注是否发生在首个终态之后（回流段）。"""

        terminal = self.first_terminal
        result = []
        for event in self.events:
            if event["kind"] == RETURN_TRIAL:
                result.append(
                    {
                        "date": event["date"],
                        "reported": event["reported"],
                        "post_terminal": bool(
                            terminal and event["date"] > terminal["date"]
                        ),
                    }
                )
        return result

    def is_paused_on(self, day: date) -> bool:
        return any(interval.covers(day) for interval in self.pause_intervals)

    # ---- 截止日口径 -----------------------------------------------------

    def visible_events(self, cutoff: date, view: str) -> list[dict[str, Any]]:
        """截止日按指定口径可见的事件（保持时间顺序）。"""

        if view not in {"registered", "corrected"}:
            raise ValueError("view 只能是 registered 或 corrected")
        stamp = "reported" if view == "registered" else "date"
        return [event for event in self.events if event[stamp] <= cutoff]

    def status_at(self, cutoff: date, view: str) -> str:
        visible = self.visible_events(cutoff, view)
        terminals = [event for event in visible if event["kind"] in TERMINAL_KINDS]
        if terminals:
            return min(terminals, key=lambda event: event["date"])["kind"]
        return WAITING_STATUS

    def waiting_reason_at(self, cutoff: date, view: str) -> str | None:
        """仅对仍在等的孩子给出有证据支撑的等待原因。"""

        if self.status_at(cutoff, view) != WAITING_STATUS:
            return None
        visible = self.visible_events(cutoff, view)
        if any(event["kind"] == REQUEST_KIND for event in visible):
            return WAIT_REASON_PENDING_RECEIPT
        if self.is_paused_on(cutoff):
            return WAIT_REASON_FAMILY_PAUSE
        return WAIT_REASON_OBSERVING

    # ---- 时间计量 -------------------------------------------------------

    def _session_ledger(self, end_day: date) -> dict[str, int]:
        """统计 [cohort, end_day] 内与该孩子接受场次相关的日历场次。

        三类互斥计数：
        * available：开放且不在家庭暂停区间内（实际可用试读机会）；
        * family_paused：开放但落在家庭主动暂停区间内；
        * center_closed：calendar 标记 open=false（园方停开，与家庭暂停不互换）。
        """

        ledger = {"available": 0, "family_paused": 0, "center_closed": 0}
        for (day, room, slot), is_open in self.open_sessions.items():
            if room != self.room or slot not in self.accepted_slots:
                continue
            if not (self.cohort <= day <= end_day):
                continue
            if not is_open:
                ledger["center_closed"] += 1
            elif self.is_paused_on(day):
                ledger["family_paused"] += 1
            else:
                ledger["available"] += 1
        return ledger

    def metrics_at(self, cutoff: date, view: str) -> dict[str, Any]:
        """截止日计量：终态孩子量到首个终态发生日，等待孩子量到截止日。"""

        status = self.status_at(cutoff, view)
        visible = self.visible_events(cutoff, view)
        if status in TERMINAL_STATUS:
            end_day = min(
                event["date"]
                for event in visible
                if event["kind"] in TERMINAL_KINDS
            )
        else:
            end_day = cutoff
        ledger = self._session_ledger(end_day)
        attendance = sum(
            1 for event in visible if event["kind"] == TRIAL_ATTEND
        )
        return_trials = sum(
            1 for event in visible if event["kind"] == RETURN_TRIAL
        )
        return {
            "child": self.child_id,
            "cohort": self.cohort.isoformat(),
            "room": self.room,
            "slot_choice": " + ".join(self.accepted_slots),
            "status": status,
            "waiting_reason": self.waiting_reason_at(cutoff, view),
            "end_date": end_day.isoformat(),
            "calendar_days": (end_day - self.cohort).days,
            "available_opportunities": ledger["available"],
            "family_paused_sessions": ledger["family_paused"],
            "center_closed_sessions": ledger["center_closed"],
            "scheduled_sessions": sum(ledger.values()),
            "attendance_count": attendance,
            "return_trial_count": return_trials,
            "has_pending_request": any(
                event["kind"] == REQUEST_KIND for event in visible
            ),
        }

    def horizon_end(self, horizon_days: int) -> date:
        return self.cohort + timedelta(days=horizon_days)

    def horizon_observable(self, horizon_days: int, cutoff: date) -> bool:
        """该观察阶段是否在截止日之前完整积累（不得把未成熟阶段装成熟）。"""

        return self.horizon_end(horizon_days) <= cutoff

    def horizon_metrics(
        self, horizon_days: int, cutoff: date, view: str
    ) -> dict[str, Any]:
        """固定阶段 [cohort, cohort+horizon_days] 的转换机会计量。

        阶段端点早于截止日时按阶段端点回看；阶段未完整积累时显式标注
        ``observable=False``，仍返回截断值但不参与成熟组比较。
        """

        stage_end = self.horizon_end(horizon_days)
        observable = stage_end <= cutoff
        effective_end = min(stage_end, cutoff)
        stamp = "reported" if view == "registered" else "date"
        visible = [
            event
            for event in self.events
            if event[stamp] <= effective_end
        ]
        terminals = [event for event in visible if event["kind"] in TERMINAL_KINDS]
        status = (
            min(terminals, key=lambda event: event["date"])["kind"]
            if terminals
            else WAITING_STATUS
        )
        ledger = self._session_ledger(effective_end)
        return {
            "child": self.child_id,
            "cohort": self.cohort.isoformat(),
            "room": self.room,
            "slot_choice": " + ".join(self.accepted_slots),
            "horizon_days": horizon_days,
            "observable": observable,
            "stage_end": stage_end.isoformat(),
            "status": status,
            "converted_by_stage_end": status == "常规班实际接收",
            "calendar_days": (effective_end - self.cohort).days,
            "available_opportunities": ledger["available"],
            "family_paused_sessions": ledger["family_paused"],
            "center_closed_sessions": ledger["center_closed"],
        }


def build_journeys(bundle: Bundle) -> list[Journey]:
    """把六份资料接到同一个孩子上，生成全部 Journey 并做形式校验。"""

    events_by_child: dict[str, list[dict[str, Any]]] = {
        row["id"]: [] for row in bundle.children
    }
    for event in bundle.timeline:
        events_by_child[event["child"]].append(dict(event))

    open_sessions = bundle.open_sessions()
    journeys: list[Journey] = []
    for child in bundle.children:
        events = sorted(
            events_by_child[child["id"]],
            key=lambda event: (event["date"], event["reported"], event["kind"]),
        )
        pauses = _pair_pause_intervals(child["id"], events)
        journey = Journey(
            child_id=child["id"],
            cohort=child["cohort"],
            room=child["room"],
            accepted_slots=tuple(child["accepted"]),
            events=events,
            pause_intervals=tuple(pauses),
            open_sessions=open_sessions,
        )
        journey.anomalies = _detect_anomalies(journey, open_sessions)
        _validate_admission_request(journey)
        journeys.append(journey)
    return journeys


def _pair_pause_intervals(
    child_id: str, events: list[dict[str, Any]]
) -> list[PauseInterval]:
    intervals: list[PauseInterval] = []
    start: date | None = None
    for event in events:
        if event["kind"] == FAMILY_PAUSE:
            if start is not None:
                raise ValueError(f"{child_id} 出现连续两次家长暂停，缺少恢复")
            start = event["date"]
        elif event["kind"] == FAMILY_RESUME:
            if start is None:
                raise ValueError(f"{child_id} 的恢复试读没有对应的家长暂停")
            if event["date"] < start:
                raise ValueError(f"{child_id} 恢复试读早于暂停开始")
            intervals.append(PauseInterval(start=start, end=event["date"]))
            start = None
    if start is not None:
        raise ValueError(f"{child_id} 的家长暂停缺少恢复试读")
    return intervals


def _validate_admission_request(journey: Journey) -> None:
    """每次常规班实际接收之前必须有一次更早的申请转班（申请本身不算完成）。"""

    request_dates = [event["date"] for event in journey.requests]
    for admission in journey.admissions:
        if not any(day < admission["date"] for day in request_dates):
            raise ValueError(
                f"{journey.child_id} 的常规班接收 {admission['date']} 缺少更早的转班申请"
            )


def _detect_anomalies(
    journey: Journey,
    open_sessions: dict[tuple[date, str, str], bool],
) -> list[dict[str, Any]]:
    """到场记录与日历/接受场次不符的数据质量问题（只标注，不改写计量）。"""

    anomalies: list[dict[str, Any]] = []
    for event in journey.events:
        if event["kind"] not in (TRIAL_ATTEND, RETURN_TRIAL):
            continue
        day = event["date"]
        matching = [
            (slot, open_sessions.get((day, journey.room, slot)))
            for slot in journey.accepted_slots
        ]
        bookable = [
            (slot, is_open)
            for slot, is_open in matching
            if is_open is True
        ]
        if not bookable:
            reasons = []
            if not any(is_open is not None for _, is_open in matching):
                reasons.append("当日日历无孩子已接受场次")
            if any(is_open is False for _, is_open in matching):
                reasons.append("当日对应场次园方停开")
            anomalies.append(
                {
                    "child": journey.child_id,
                    "date": day.isoformat(),
                    "kind": event["kind"],
                    "reported": event["reported"].isoformat(),
                    "reason": "；".join(reasons) or "场次不可预约",
                }
            )
    return anomalies
