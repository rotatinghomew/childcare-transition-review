"""把各层结果装配为一份结构化分析报告，并提供可读文本渲染。"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from transition.cohort import cohort_table, horizon_snapshot, maturity_flags
from transition.io_utils import Bundle
from transition.journey import WAITING_STATUS, build_journeys
from transition.planning import plan_extension, plan_regular_openings
from transition.replies import compare_views


def build_report(bundle: Bundle, regular_openings: int = 2) -> dict[str, Any]:
    """生成覆盖过渡结果、阶段比较、安排试算与答复的完整报告。"""

    journeys = build_journeys(bundle)
    cutoffs = bundle.review.cutoffs
    primary_cutoff = cutoffs[0]
    final_cutoff = cutoffs[-1]

    anomalies = [
        anomaly
        for journey in journeys
        for anomaly in journey.anomalies
    ]

    cutoff_views = {}
    for cutoff in cutoffs:
        cutoff_views[cutoff.isoformat()] = {
            "registered": cohort_table(journeys, cutoff, "registered"),
            "corrected": cohort_table(journeys, cutoff, "corrected"),
        }

    horizon_sections = [
        horizon_snapshot(
            journeys,
            horizon_days=horizon,
            cutoff=final_cutoff,
            view="corrected",
            minimum_observations=bundle.review.minimum_observations,
        )
        for horizon in bundle.review.horizons_days
    ]
    flags = maturity_flags(
        journeys,
        bundle.review.horizons_days,
        final_cutoff,
        bundle.review.minimum_observations,
    )

    extension = plan_extension(bundle, journeys)
    openings = plan_regular_openings(bundle, journeys, openings=regular_openings)

    reply_views = {
        cutoff.isoformat(): compare_views(journeys, cutoff, openings)
        for cutoff in cutoffs
    }

    late_receipts = _late_receipt_section(bundle, journeys, primary_cutoff)
    return_trials = [
        {
            "child": journey.child_id,
            "cohort": journey.cohort.isoformat(),
            "return_trials": journey.return_trials,
            "first_terminal": (
                journey.first_terminal["kind"] if journey.first_terminal else None
            ),
        }
        for journey in journeys
        if journey.return_trials
    ]

    return {
        "meta": {
            "child_count": len(journeys),
            "cohort_count": len({journey.cohort for journey in journeys}),
            "cutoffs": [day.isoformat() for day in cutoffs],
            "horizons_days": list(bundle.review.horizons_days),
            "minimum_observations": bundle.review.minimum_observations,
            "regular_openings_requested": regular_openings,
            "calendar_authority": "周几时段为营业标签，以 calendar 为准，不按真实星期推断",
        },
        "data_quality": {
            "attendance_anomalies": anomalies,
            "anomaly_count": len(anomalies),
            "note": "异常只标注、不改写机会计量；机会一律按 calendar 与孩子接受场次计算。",
        },
        "cohort_outcomes": cutoff_views,
        "horizon_comparison": horizon_sections,
        "maturity_flags": flags,
        "return_trial_episodes": return_trials,
        "late_receipts": late_receipts,
        "scenario_extension": extension,
        "scenario_regular_openings": openings,
        "parent_replies": reply_views,
    }


def _late_receipt_section(
    bundle: Bundle, journeys: list[Any], cutoff: date
) -> dict[str, Any]:
    """迟到回执：发生日不晚于截止日、登记日晚于截止日的接收事件。"""

    late_events = []
    for journey in journeys:
        for admission in journey.admissions:
            if admission["date"] <= cutoff < admission["reported"]:
                registered_status = journey.status_at(cutoff, "registered")
                corrected_status = journey.status_at(cutoff, "corrected")
                late_events.append(
                    {
                        "child": journey.child_id,
                        "event_date": admission["date"].isoformat(),
                        "reported_date": admission["reported"].isoformat(),
                        "room": admission["room"],
                        "seat": admission["seat"],
                        "status_registered_view": registered_status,
                        "status_corrected_view": corrected_status,
                        "explanation_correction": (
                            f"登记口径下该孩子为{registered_status}；补登后按发生日修正为"
                            f"{corrected_status}，并自发生日起按四周窗口占班，"
                            "已安顿后不重新争位。"
                        ),
                    }
                )
    return {
        "cutoff": cutoff.isoformat(),
        "events": late_events,
        "count": len(late_events),
    }


# ---- 文本渲染 -------------------------------------------------------------

def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    meta = report["meta"]
    lines.append("托育适应期过渡结果与继续预约 — 分析报告")
    lines.append(
        f"孩子 {meta['child_count']} 人 / 批次 {meta['cohort_count']} 个；"
        f"截止日 {', '.join(meta['cutoffs'])}；观察阶段 {meta['horizons_days']} 天；"
        f"成熟门槛 {meta['minimum_observations']} 观测。"
    )
    lines.append("")

    lines.append("一、批次终态（修正口径，分母为整批首次接受人数）")
    for cutoff, views in report["cohort_outcomes"].items():
        lines.append(f"  截止 {cutoff}")
        for row in views["corrected"]:
            counts = "，".join(
                f"{status} {n} 人" for status, n in sorted(row["status_counts"].items())
            )
            waiting = "，".join(
                f"{reason} {n} 人"
                for reason, n in sorted(row["waiting_reasons"].items())
            )
            lines.append(
                f"    批次 {row['cohort']}（n={row['n']}）：{counts}"
            )
            if waiting:
                lines.append(f"      仍在等原因：{waiting}")
    lines.append("")

    lines.append("二、同一可观察阶段的转换机会（未成熟/未完整积累不引用转入率）")
    for section in report["horizon_comparison"]:
        overall = section["overall"]
        lines.append(
            f"  阶段 {section['horizon_days']} 天：可观测 n={overall['n']}，"
            f"成熟={overall['mature']}，转换 {overall['converted']} 人，"
            f"可用试读机会中位数={overall['available_opportunities']['median']}"
        )
        for name, group in sorted(section["by_cohort"].items()):
            lines.append(
                f"    批次 {name}：observable={group['observable']}，"
                f"mature={group['mature']}，n={group['n']}，rate={group['conversion_rate']}"
            )
        slot_line = "，".join(
            f"{name}(mature={group['mature']}, 自选不可随机比) n={group['n']}"
            for name, group in sorted(section["by_slot_choice"].items())
        )
        lines.append(f"    预约结构分层（描述性）：{slot_line}")
        if section["truncated_children"]:
            lines.append(
                "    未完整积累：" + "、".join(section["truncated_children"])
            )
    lines.append("")

    late = report["late_receipts"]
    lines.append(f"三、迟到接收回执（截止 {late['cutoff']}，共 {late['count']} 条）")
    for event in late["events"]:
        lines.append(
            f"  {event['child']}：发生 {event['event_date']} / 登记 {event['reported_date']}，"
            f"口径纠正 {event['status_registered_view']} → {event['status_corrected_view']}"
        )
    lines.append("")

    lines.append("四、安排试算")
    extension = report["scenario_extension"]
    overbooked = extension["overbooked_admissions"]
    lines.append(
        f"  延长过渡：基线不可兑现接收 {len(overbooked)} 条；"
        f"教师冲突 {len(extension['teacher_conflicts'])} 处；不新增常规承诺。"
    )
    for item in overbooked:
        lines.append(
            f"    无法承诺 {item['child']} 的 {item['room']}/{item['slot']}："
            + "；".join(item["reasons"])
        )
    openings = report["scenario_regular_openings"]
    lines.append(
        f"  开放常规预约：申请 {openings['requested_openings']} 个，"
        f"可承诺 {openings['committed_openings']} 个。"
    )
    for proposal in openings["proposals"]:
        verdict = "可行" if proposal["feasible"] else "不可行"
        lines.append(
            f"    {proposal['child']} {proposal['room']}/{proposal['slot']} {verdict}"
            + ("" if proposal["feasible"] else "：" + "；".join(proposal["reasons"]))
        )
    for conflict in openings["teacher_conflicts"]:
        lines.append(f"  教师冲突：{conflict['unkeepable']}（{conflict['day']} {conflict['slot']}）")
    lines.append("")

    lines.append("五、家长答复（首个截止日，登记 vs 修正口径）")
    replies = report["parent_replies"][meta["cutoffs"][0]]
    dist_registered = Counter(item["reply"] for item in replies["registered"])
    dist_corrected = Counter(item["reply"] for item in replies["corrected"])
    lines.append(
        "  登记口径："
        + "，".join(f"{kind} {n}" for kind, n in sorted(dist_registered.items()))
    )
    lines.append(
        "  修正口径："
        + "，".join(f"{kind} {n}" for kind, n in sorted(dist_corrected.items()))
    )
    changed = [
        (left["child"], left["reply"], right["reply"])
        for left, right in zip(replies["registered"], replies["corrected"])
        if left["reply"] != right["reply"]
    ]
    for child, before, after in changed:
        lines.append(f"    {child} 答复纠正：{before} → {after}")
    lines.append("")

    lines.append(
        f"六、数据质量：{report['data_quality']['anomaly_count']} 条到场与日历/接受场次不符，"
        "已在结构化结果中逐条列出，不并入任何机会或终态计数。"
    )
    return "\n".join(lines)
