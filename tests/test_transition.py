"""过渡分析业务口径测试。

覆盖：整批分母不剔除、双时间计量与两类停开不互换、申请不算完成、原因未明不并入
撤回、迟到回执双口径、四周连续承接、教师跨房间冲突、成熟度门槛、预约结构不自选
可比、迟到回执不抢占已安顿家庭、回流不延长初次批次。
"""

import unittest
from datetime import date
from pathlib import Path

from transition.io_utils import load_bundle
from transition.journey import (
    TERMINAL_STATUS,
    WAITING_STATUS,
    WAIT_REASON_PENDING_RECEIPT,
    build_journeys,
)
from transition.cohort import cohort_table, horizon_snapshot, maturity_flags
from transition.planning import (
    CapacityLedger,
    build_baseline_ledger,
    plan_extension,
    plan_regular_openings,
    apply_late_receipt,
)
from transition.replies import build_replies, REPLY_SETTLED, REPLY_CONTINUE

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
CUT1 = date(2026, 9, 11)
CUT2 = date(2026, 9, 26)


class TransitionTestBase(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(SAMPLES)
        self.journeys = build_journeys(self.bundle)
        self.by_id = {journey.child_id: journey for journey in self.journeys}


class TestCohortIntegrity(TransitionTestBase):
    def test_full_batch_kept_in_denominator(self):
        """任何口径下批次 n 都等于首次接受人数，离开家庭不被抹去。"""

        for view in ("registered", "corrected"):
            table = cohort_table(self.journeys, CUT2, view)
            self.assertEqual([row["n"] for row in table], [12] * 5)
            for row in table:
                self.assertEqual(sum(row["status_counts"].values()), row["n"])

    def test_five_statuses_distinct_and_unknown_not_withdrawal(self):
        table = cohort_table(self.journeys, CUT2, "corrected")
        statuses = set()
        for row in table:
            statuses.update(row["status_counts"])
        self.assertEqual(
            statuses,
            TERMINAL_STATUS | {WAITING_STATUS},
        )
        # 原因未明是独立类别，绝不能出现在家长撤回的计数里（计数键各自独立）。
        for row in table:
            self.assertIn("原因未明", row["status_counts"])
            self.assertIn("家长撤回", row["status_counts"])

    def test_request_alone_is_not_terminal(self):
        """截止 cut1：026/032 已有申请但回执迟到，登记口径必须仍是仍在等。"""

        for child_id in ("幼026", "幼032"):
            journey = self.by_id[child_id]
            self.assertTrue(journey.requests)
            self.assertEqual(
                journey.status_at(CUT1, "registered"), WAITING_STATUS
            )
            self.assertEqual(
                journey.waiting_reason_at(CUT1, "registered"),
                WAIT_REASON_PENDING_RECEIPT,
            )

    def test_late_receipt_corrected_view(self):
        for child_id in ("幼026", "幼032"):
            journey = self.by_id[child_id]
            self.assertEqual(
                journey.status_at(CUT1, "registered"), WAITING_STATUS
            )
            self.assertEqual(
                journey.status_at(CUT1, "corrected"), "常规班实际接收"
            )
        # cut2 时登记也已到达，两口径一致。
        for child_id in ("幼026", "幼032"):
            journey = self.by_id[child_id]
            self.assertEqual(
                journey.status_at(CUT2, "registered"), "常规班实际接收"
            )

    def test_admission_always_has_earlier_request(self):
        # build_journeys 已对每个孩子校验，这里再显式断言数量关系。
        admissions = sum(len(journey.admissions) for journey in self.journeys)
        requests = sum(len(journey.requests) for journey in self.journeys)
        self.assertEqual(admissions, 10)
        self.assertEqual(requests, 10)


class TestTimeAccounting(TransitionTestBase):
    def test_two_clocks_kept_and_closures_not_swapped(self):
        """日历等待与可用机会并存；园方停开与家庭暂停互斥计数。"""

        # 幼037（乙，周一+周三）：暂停 09-12 至 09-15（闭开区间），
        # 09-12 是乙/周三开放日落入家庭暂停；cut2 计量。
        journey = self.by_id["幼037"]
        metrics = journey.metrics_at(CUT2, "corrected")
        self.assertGreater(metrics["calendar_days"], metrics["available_opportunities"])
        self.assertGreaterEqual(metrics["family_paused_sessions"], 1)
        self.assertGreater(metrics["center_closed_sessions"], 0)
        # 三类互斥且合计等于排定场次。
        self.assertEqual(
            metrics["family_paused_sessions"]
            + metrics["center_closed_sessions"]
            + metrics["available_opportunities"],
            metrics["scheduled_sessions"],
        )

    def test_center_closure_never_counted_as_family_pause(self):
        # 幼013（乙，双场次）暂停 08-31 至 09-03，区间内乙没有开放场次：
        # 家庭暂停计数必须为 0，等待只表现为园方停开，两类原因不互换。
        metrics = self.by_id["幼013"].metrics_at(CUT2, "corrected")
        self.assertEqual(metrics["family_paused_sessions"], 0)
        self.assertGreater(metrics["center_closed_sessions"], 0)

    def test_family_pause_does_not_inflate_available_opportunities(self):
        # 对照：暂停期内确有开放场次的孩子（幼001，08-27 乙/周一开放），
        # 该场计入家庭暂停而非可用机会。
        metrics = self.by_id["幼001"].metrics_at(CUT2, "corrected")
        self.assertEqual(metrics["family_paused_sessions"], 1)
        self.assertEqual(
            metrics["family_paused_sessions"]
            + metrics["center_closed_sessions"]
            + metrics["available_opportunities"],
            metrics["scheduled_sessions"],
        )

    def test_resume_day_is_available(self):
        # 恢复日当天视为可试读，不计入家庭暂停。
        journey = self.by_id["幼037"]
        self.assertFalse(journey.is_paused_on(date(2026, 9, 15)))
        self.assertTrue(journey.is_paused_on(date(2026, 9, 12)))

    def test_time_to_admission_uses_event_date(self):
        # 全部 10 个接收：cohort 到接收发生日均为 9 个日历日。
        for journey in self.journeys:
            if journey.admissions:
                metrics = journey.metrics_at(CUT2, "corrected")
                self.assertEqual(metrics["status"], "常规班实际接收")
                self.assertEqual(metrics["calendar_days"], 9)


class TestReturnTrialsAndEpisodes(TransitionTestBase):
    def test_return_trial_does_not_extend_initial_cohort(self):
        journey = self.by_id["幼020"]
        returns = journey.return_trials
        self.assertTrue(any(item["post_terminal"] for item in returns))
        # 初次批次入口与终态时间不随后续另次试读改变。
        metrics = journey.metrics_at(CUT2, "corrected")
        self.assertEqual(metrics["end_date"], "2026-09-04")
        self.assertEqual(journey.cohort, date(2026, 8, 26))

    def test_return_trial_after_other_service_is_new_episode(self):
        journey = self.by_id["幼010"]
        self.assertEqual(journey.first_terminal["kind"], "另选服务")
        self.assertTrue(journey.return_trials)
        # 终态仍是另选服务，不被回流改写。
        self.assertEqual(
            journey.status_at(CUT2, "corrected"), "另选服务"
        )


class TestMaturityAndComparability(TransitionTestBase):
    def test_horizon_truncation_flagged(self):
        snapshot = horizon_snapshot(
            self.journeys,
            horizon_days=21,
            cutoff=CUT2,
            view="corrected",
            minimum_observations=self.bundle.review.minimum_observations,
        )
        # 最后两个批次共 24 人未完整积累 21 天。
        self.assertEqual(len(snapshot["truncated_children"]), 24)
        # 未完整积累的批次不进入阶段分组，避免假装成熟。
        self.assertNotIn("2026-09-13", snapshot["by_cohort"])
        self.assertNotIn("2026-09-07", snapshot["by_cohort"])

    def test_observable_horizon_is_mature(self):
        snapshot14 = horizon_snapshot(
            self.journeys, 14, CUT2, "corrected",
            minimum_observations=6,
        )
        self.assertTrue(snapshot14["overall"]["mature"])
        self.assertEqual(snapshot14["overall"]["n"], 48)
        snapshot7 = horizon_snapshot(
            self.journeys, 7, CUT2, "corrected", minimum_observations=6
        )
        self.assertEqual(snapshot7["overall"]["n"], 60)

    def test_maturity_flags_per_cohort(self):
        flags = maturity_flags(
            self.journeys, (7, 14, 21), CUT2, minimum_observations=6
        )
        last_21 = next(
            item
            for item in flags
            if item["cohort"] == "2026-09-13" and item["horizon_days"] == 21
        )
        self.assertFalse(last_21["fully_observable"])
        self.assertFalse(last_21["mature"])
        first_7 = next(
            item
            for item in flags
            if item["cohort"] == "2026-08-20" and item["horizon_days"] == 7
        )
        self.assertTrue(first_7["mature"])

    def test_slot_choice_marked_self_selected(self):
        snapshot = horizon_snapshot(
            self.journeys, 7, CUT2, "corrected", minimum_observations=6
        )
        for group in snapshot["by_slot_choice"].values():
            self.assertTrue(group["self_selected"])
        for group in snapshot["by_room"].values():
            self.assertFalse(group["self_selected"])

    def test_room_strata_below_threshold_not_mature(self):
        # 单批次内每房间仅 4 人，低于 minimum_observations=6；用高门槛演示未成熟标记。
        snapshot = horizon_snapshot(
            self.journeys, 7, CUT2, "corrected", minimum_observations=21
        )
        # 单房间跨批累计 20 人 < 21，房间分层必须标记为未成熟。
        for group in snapshot["by_room"].values():
            self.assertFalse(group["mature"])


class TestCapacityAndPlanning(TransitionTestBase):
    def test_four_week_window_uses_open_days(self):
        ledger = CapacityLedger(self.bundle)
        window = ledger.four_week_window("丙", "周三上午", date(2026, 9, 11))
        self.assertEqual(
            tuple(day.isoformat() for day in window),
            ("2026-09-12", "2026-09-19", "2026-09-26", "2026-10-03"),
        )

    def test_baseline_overbooking_named_with_holders(self):
        ledger, _ = build_baseline_ledger(self.bundle, self.journeys)
        failed = [item for item in ledger.attempts if not item.feasible]
        succeeded = [item for item in ledger.attempts if item.feasible]
        # 成功尝试 4 个：002/008/050 三个接收 + 002 的保留（同一孩子合并计数）；
        # 失败尝试 7 个，全部是接收。
        self.assertEqual(len(succeeded), 4)
        self.assertEqual(len(failed), 7)
        self.assertTrue(
            all(item.source == "admission" for item in failed)
        )
        for attempt in failed:
            self.assertTrue(attempt.reasons)
            self.assertTrue(any("常规位" in reason for reason in attempt.reasons))

    def test_reservation_extends_existing_holder_without_double_count(self):
        ledger, _ = build_baseline_ledger(self.bundle, self.journeys)
        # 002 的保留使其持有到 10-03；该日与 050 同场，同孩子不重复占容量。
        holders_1003 = ledger.holds[
            ("丙", "周三上午", date(2026, 10, 3))
        ]
        self.assertIn("幼002", holders_1003)
        self.assertEqual(holders_1003, {"幼002", "幼050"})

    def test_trial_seats_are_not_regular_commitments(self):
        result = plan_extension(self.bundle, self.journeys)
        self.assertEqual(result["new_regular_commitments"], [])
        for row in result["waiting_placements"]:
            self.assertFalse(row["regular_commitment_offered"])
            for option in row["accepted_slot_options"]:
                self.assertIn("不构成常规班承诺", option["note"])

    def test_teacher_conflict_between_rooms_is_surfaced(self):
        """构造同一教师在甲、乙同时段的承诺，台账必须指出无法同时兑现。"""

        ledger = CapacityLedger(self.bundle)
        first = ledger.commit(
            "虚拟甲", "甲", "周一上午", "test", date(2026, 10, 1)
        )
        second = ledger.commit(
            "虚拟乙", "乙", "周一上午", "test", date(2026, 10, 1)
        )
        self.assertTrue(first.feasible)
        self.assertFalse(second.feasible)
        joined_reasons = "；".join(second.reasons)
        self.assertIn("教师一", joined_reasons)
        self.assertIn("甲", joined_reasons)
        self.assertIn("乙", joined_reasons)
        conflicts = ledger.teacher_conflict_report()
        self.assertTrue(
            any("甲" in item["rooms"] and "乙" in item["rooms"] for item in conflicts)
        )

    def test_inquiry_only_for_unaccepted_slots(self):
        result = plan_regular_openings(self.bundle, self.journeys, openings=2)
        for item in result["inquiry_suggestions"]:
            self.assertFalse(item["family_accepted"])
            journey = self.by_id[item["child"]]
            self.assertNotIn(item["slot"], journey.accepted_slots)

    def test_late_receipt_does_not_displace_settled_family(self):
        """补登后的承诺落账不抢占已安顿孩子；002/008 始终在持。"""

        ledger, _ = build_baseline_ledger(self.bundle, self.journeys)
        self.assertTrue({"幼002", "幼008"} <= ledger.settled_children)
        for day in (
            date(2026, 9, 12),
            date(2026, 9, 19),
            date(2026, 9, 26),
        ):
            holders = ledger.holds[("丙", "周三上午", day)]
            self.assertIn("幼002", holders)

    def test_apply_late_receipt_corrects_explanation_and_updates_slots(self):
        """迟到回执：纠正解释、按能否占班更新可约时段、不重开已安顿位子的竞争。"""

        # 026 的回执发生在 09-10、补登在 09-13；丙/周三常规位已被 002/008 占满，
        # 因此该回执无法真的占班，但解释必须纠正，且已安顿孩子不受影响。
        result = apply_late_receipt(
            self.bundle,
            self.journeys,
            child_id="幼026",
            receipt_date=date(2026, 9, 10),
            reported_date=date(2026, 9, 13),
            room="丙",
            slot="周三上午",
        )
        self.assertEqual(
            result["explanation_correction"]["registered_view_before"],
            WAITING_STATUS,
        )
        self.assertEqual(
            result["explanation_correction"]["corrected_view_after"],
            "常规班实际接收",
        )
        self.assertFalse(result["seat_taken"])
        self.assertTrue(result["attempt"]["reasons"])
        self.assertIn("幼002", result["settled_families_protected"])
        self.assertIn("幼008", result["settled_families_protected"])
        # 受影响场次仍显示满员，没有因为补登而挤掉任何人。
        for row in result["future_availability"]:
            self.assertEqual(row["free"], 0)
            self.assertIn("幼002", row["holders"])

    def test_apply_late_receipt_to_free_room_takes_seat(self):
        """有空位房间的迟到回执可以占班，并给出四周窗口的剩余容量变化。"""

        result = apply_late_receipt(
            self.bundle,
            self.journeys,
            child_id="幼000",
            receipt_date=date(2026, 9, 26),
            reported_date=date(2026, 9, 28),
            room="甲",
            slot="周三上午",
        )
        self.assertTrue(result["seat_taken"])
        self.assertEqual(len(result["attempt"]["window_dates"]), 4)
        self.assertTrue(
            all(row["held"] >= 1 for row in result["future_availability"])
        )


class TestReplies(TransitionTestBase):
    def test_reply_view_correction_for_late_receipt(self):
        registered = {
            item["child"]: item
            for item in build_replies(self.journeys, CUT1, "registered")
        }
        corrected = {
            item["child"]: item
            for item in build_replies(self.journeys, CUT1, "corrected")
        }
        self.assertNotEqual(
            registered["幼026"]["reply"], REPLY_SETTLED
        )
        self.assertEqual(corrected["幼026"]["reply"], REPLY_SETTLED)
        self.assertTrue(corrected["幼026"]["committed"])

    def test_unknown_reason_reply_not_labelled_withdrawal(self):
        corrected = {
            item["child"]: item
            for item in build_replies(self.journeys, CUT2, "corrected")
        }
        journey = next(
            journey
            for journey in self.journeys
            if journey.first_terminal and journey.first_terminal["kind"] == "原因未明"
        )
        reply = corrected[journey.child_id]
        self.assertTrue(
            any("原因尚未确定" in text for text in reply["uncertainty"])
        )
        self.assertFalse(
            any("撤回" in text for text in reply["uncertainty"])
        )

    def test_continue_reply_keeps_commitment_and_uncertainty_apart(self):
        registered = build_replies(self.journeys, CUT1, "registered")
        continue_reply = next(
            item for item in registered if item["reply"] == REPLY_CONTINUE
        )
        self.assertTrue(continue_reply["committed"])
        self.assertTrue(continue_reply["uncertainty"])


if __name__ == "__main__":
    unittest.main()
