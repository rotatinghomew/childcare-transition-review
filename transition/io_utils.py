"""原始资料读取与引用完整性校验。

六份资料的角色（见 README）：

* children：首次接受批次（cohort）、房间、家长已同意的可选时段；
* timeline：按发生日登记的多段经过，含独立的登记日 reported；
* calendar：房间×场次×日历日的营业标签，open=false 为园方停开；
* capacity：房间与教师同时满足时的每场常规位/试读位；
* staff：教师可覆盖的房间；
* review：截止日、观察阶段（天）、最少观测数、已确认保留时段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

TERMINAL_KINDS_RAW = (
    "常规班实际接收",
    "另选服务",
    "家长撤回",
    "原因未明",
)
REQUEST_KIND = "申请转班"
TRIAL_ATTEND = "试读到场"
RETURN_TRIAL = "另次试读"
FAMILY_PAUSE = "家长暂停"
FAMILY_RESUME = "恢复试读"


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


@dataclass(frozen=True)
class CapacityRow:
    room: str
    slot: str
    teacher: str
    regular_seats: int
    trial_seats: int


@dataclass(frozen=True)
class Reservation:
    child: str
    room: str
    slot: str
    from_date: date
    weeks: int


@dataclass(frozen=True)
class ReviewConfig:
    cutoffs: tuple[date, ...]
    horizons_days: tuple[int, ...]
    minimum_observations: int
    reservations: tuple[Reservation, ...]


@dataclass
class Bundle:
    """六份资料的只读装载结果，日期已转换为 ``date``。"""

    children: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    calendar: list[dict[str, Any]]
    capacity: list[CapacityRow]
    staff: dict[str, tuple[str, ...]]
    review: ReviewConfig
    source_dir: Path

    def child_by_id(self) -> dict[str, dict[str, Any]]:
        return {row["id"]: row for row in self.children}

    def open_sessions(self) -> dict[tuple[str, str, str], bool]:
        """键为 (日历日, 房间, 场次标签)，值为是否开放。"""
        return {
            (row["day"], row["room"], row["session"]): bool(row["open"])
            for row in self.calendar
        }


def load_bundle(folder: str | Path) -> Bundle:
    """从资料目录读取 JSON，并做引用与形式校验。"""

    folder = Path(folder)
    raw = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.json"))
    }
    required = {"children", "timeline", "calendar", "capacity", "staff", "review"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"缺少资料文件：{sorted(missing)}")

    children = raw["children"]
    timeline = raw["timeline"]
    calendar = raw["calendar"]

    child_ids = {row["id"] for row in children}
    unknown = {event["child"] for event in timeline} - child_ids
    if unknown:
        raise ValueError(f"timeline 引用了不存在的孩子：{sorted(unknown)}")

    # 日期统一转换，保持原列表顺序（同孩子按发生日、登记日排序在 journey 层完成）。
    for event in timeline:
        event["date"] = _parse_date(event["date"])
        event["reported"] = _parse_date(event["reported"])
        if event["reported"] < event["date"]:
            raise ValueError(
                f"登记日早于发生日：{event['child']} {event['kind']} "
                f"{event['date']} / {event['reported']}"
            )
    for row in calendar:
        row["day"] = _parse_date(row["day"])
    for row in children:
        row["cohort"] = _parse_date(row["cohort"])

    capacity = [
        CapacityRow(
            room=row["room"],
            slot=row["slot"],
            teacher=row["teacher"],
            regular_seats=int(row["regular_seats"]),
            trial_seats=int(row["trial_seats"]),
        )
        for row in raw["capacity"]
    ]
    staff = {teacher: tuple(rooms) for teacher, rooms in raw["staff"].items()}

    # 容量行的教师必须具备该房间资格。
    for row in capacity:
        if row.room not in staff.get(row.teacher, ()):
            raise ValueError(f"教师 {row.teacher} 不具备房间 {row.room} 资格")

    review_raw = raw["review"]
    reservations = tuple(
        Reservation(
            child=row["child"],
            room=row["room"],
            slot=row["slot"],
            from_date=_parse_date(row["from_date"]),
            weeks=int(row["weeks"]),
        )
        for row in review_raw.get("reserved", [])
    )
    for reservation in reservations:
        if reservation.child not in child_ids:
            raise ValueError(f"保留时段引用了不存在的孩子：{reservation.child}")

    review = ReviewConfig(
        cutoffs=tuple(_parse_date(value) for value in review_raw["cutoffs"]),
        horizons_days=tuple(int(value) for value in review_raw["horizons_days"]),
        minimum_observations=int(review_raw["minimum_observations"]),
        reservations=reservations,
    )

    bundle = Bundle(
        children=children,
        timeline=timeline,
        calendar=calendar,
        capacity=capacity,
        staff=staff,
        review=review,
        source_dir=folder,
    )
    _validate_calendar_references(bundle)
    return bundle


def _validate_calendar_references(bundle: Bundle) -> None:
    open_sessions = bundle.open_sessions()
    known_rooms = {row.room for row in bundle.capacity}
    known_slots = {row.slot for row in bundle.capacity}
    for (day, room, slot), _open in open_sessions.items():
        if room not in known_rooms:
            raise ValueError(f"calendar 出现未知房间：{room}（{day}）")
        if slot not in known_slots:
            raise ValueError(f"calendar 出现未知场次：{slot}（{day}）")
