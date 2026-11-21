"""试读到常规班过渡分析。

模块划分：

* ``io_utils``：读取与校验原始 JSON 资料；
* ``journey``：以首次接受批次为入口的单个孩子多段经过与时间计量；
* ``cohort``：批次/阶段汇总、竞争风险口径与成熟度判定；
* ``planning``：四周连续承接的容量台账、承诺冲突与两种安排试算；
* ``replies``：给家长的继续/候补/暂不能安排答复；
* ``report``：汇总为结构化结果与可读文本。

所有日期均为当地日历日期，以 ``datetime.date`` 表示；空值语义为"无观测"，
绝不按数值零处理。仅使用 Python 标准库。
"""

from transition.io_utils import load_bundle, Bundle
from transition.journey import (
    Journey,
    TERMINAL_KINDS,
    TERMINAL_STATUS,
    WAITING_STATUS,
    build_journeys,
)
from transition.cohort import horizon_snapshot, cohort_table, maturity_flags
from transition.planning import (
    CapacityLedger,
    plan_extension,
    plan_regular_openings,
    apply_late_receipt,
)
from transition.replies import build_replies

__all__ = [
    "load_bundle",
    "Bundle",
    "Journey",
    "TERMINAL_KINDS",
    "TERMINAL_STATUS",
    "WAITING_STATUS",
    "build_journeys",
    "horizon_snapshot",
    "cohort_table",
    "maturity_flags",
    "CapacityLedger",
    "plan_extension",
    "plan_regular_openings",
    "apply_late_receipt",
    "build_replies",
]
