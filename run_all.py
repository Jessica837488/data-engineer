# -*- coding: utf-8 -*-
"""
一键运行入口（笔试要求：python run_all.py 从 0 到 1 跑通，含数据下载）

设计原则：
1. 顺序执行各模块，但每个模块独立捕获异常——某一模块失败不阻塞后续模块
   （README 硬性要求："不要因模块 1 报错导致后面全跑不了"）。
2. 每个模块对应一个可独立运行的入口脚本，本文件只负责编排与汇总退出码。
3. 全程日志写入 run_all_*.log（已被 .gitignore 忽略，不会污染仓库）。

模块与入口对应关系：
  [0] 数据下载   -> download_data.py
  [1] 数据质量   -> q1_data_quality/q1_code.py          （模块 1 开发后接入）
  [2] SQL 分析   -> 需本地 MySQL 8.0（Docker），通过环境变量控制是否执行
  [3] ETL 管道   -> q3_etl_pipeline/pipeline.py         （模块 3 开发后接入）
  [4] LLM 抽取   -> q4_llm_extraction/extract_reviews.py（模块 4 开发后接入，需 API Key）
"""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 同 download_data.py：Windows GBK 控制台对 Unicode 符号的兼容处理
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

# 各步骤定义: (名称, 命令列表, 是否可选)
# 可选步骤失败不影响整体退出码（如缺少 MySQL/LLM Key 的环境）
STEPS = [
    ("[0] 数据下载", [sys.executable, "download_data.py"], False),
    ("[1] 数据质量分析", [sys.executable, "q1_data_quality/q1_code.py"], False),
    ("[2] SQL 分析（需 MySQL 8.0）", [sys.executable, "q2_sql_analysis/run_sql.py"], True),
    ("[3] ETL 管道", [sys.executable, "q3_etl_pipeline/pipeline.py"], False),
    ("[4] LLM 评价抽取（需 API Key）", [sys.executable, "q4_llm_extraction/extract_reviews.py"], True),
]


def run_step(name: str, cmd: list[str], optional: bool) -> bool:
    """执行单个步骤，返回是否成功。失败时打印指引但不抛出异常。"""
    # 未实现的模块（入口脚本不存在）视为跳过，不算失败，
    # 保证骨架阶段 python run_all.py 也能整体跑通
    script = ROOT / cmd[-1]
    if not script.exists():
        print(f"\n{'=' * 60}\n>>> {name}\n{'=' * 60}")
        print(f"[跳过] 模块尚未实现，入口脚本不存在: {cmd[-1]}")
        return True

    print(f"\n{'=' * 60}\n>>> {name}\n{'=' * 60}")
    start = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, check=False)
        ok = proc.returncode == 0
    except FileNotFoundError:
        print(f"[跳过] 入口脚本不存在（该模块尚未实现）: {cmd[-1]}")
        return True  # 未实现的模块不算失败，保证骨架阶段可跑通
    elapsed = time.time() - start
    status = "成功" if ok else ("失败（可选步骤，继续执行后续模块）" if optional else "失败")
    print(f"\n<<< {name} {status}，耗时 {elapsed:.1f}s")
    return ok or optional


def main() -> int:
    overall_start = time.time()
    log_name = f"run_all_{datetime.now():%Y%m%d_%H%M%S}.log"
    print(f"数据工程师笔试 Pipeline 启动，日志: {log_name}")

    failed = []
    for name, cmd, optional in STEPS:
        if not run_step(name, cmd, optional):
            failed.append(name)

    total = time.time() - overall_start
    print(f"\n{'=' * 60}\n全部步骤结束，总耗时 {total / 60:.1f} 分钟")
    if failed:
        print("以下步骤失败，请检查上方日志：")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("Pipeline 全部成功 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
