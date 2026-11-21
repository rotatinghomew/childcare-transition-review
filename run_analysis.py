"""命令行入口：生成过渡分析的结构化 JSON 与可读文本报告。

用法：
    python3 run_analysis.py [--samples samples] [--openings 2] [--json-out out.json]

仅使用标准库；读取资料时不依赖工作目录。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# 允许以 `python3 -I run_analysis.py` 方式运行：显式补上脚本所在目录。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from transition.io_utils import load_bundle
from transition.report import build_report, render_text


def _json_default(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"不可序列化的类型：{type(value)!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="托育适应期过渡分析")
    parser.add_argument(
        "--samples",
        default=str(Path(__file__).resolve().parent / "samples"),
        help="原始 JSON 资料目录",
    )
    parser.add_argument(
        "--openings",
        type=int,
        default=2,
        help="安排二拟开放的少量常规预约名额上限",
    )
    parser.add_argument("--json-out", default=None, help="结构化结果输出路径")
    args = parser.parse_args()

    bundle = load_bundle(args.samples)
    report = build_report(bundle, regular_openings=args.openings)
    print(render_text(report))

    if args.json_out:
        out = Path(args.json_out)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        print(f"\n结构化结果已写入 {out}")


if __name__ == "__main__":
    main()
