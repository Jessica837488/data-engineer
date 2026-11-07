# -*- coding: utf-8 -*-
"""
模块 2 统一入口：执行 q2_*.sql 并把查询结果留存为 query_results.md

独立运行方式（仓库根目录）：
    python q2_sql_analysis/run_sql.py          # 执行全部三条 SQL
    python q2_sql_analysis/run_sql.py q2_1     # 只执行文件名含 q2_1 的 SQL

说明：
- 凭据从根目录 .env 读取（与 load_olist.py 相同约定）。
- .sql 文件中允许 -- 注释与多语句（按分号切分）。
- 结果以 Markdown 表格写入 q2_sql_analysis/query_results.md，作为可提交的验证证据。
"""

import logging
import re
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_sql")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS_MD = HERE / "query_results.md"


def get_engine():
    import os
    load_dotenv(ROOT / ".env")
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = os.getenv("MYSQL_PORT", "3306")
    user = os.getenv("MYSQL_USER", "root")
    pwd = os.getenv("MYSQL_PASSWORD")
    if not pwd:
        logger.error("未找到 MYSQL_PASSWORD：请确认根目录 .env 已配置")
        sys.exit(1)
    return create_engine(
        f"mysql+pymysql://{user}:{pwd}@{host}:{port}/olist?charset=utf8mb4"
    )


def split_statements(sql_text: str) -> list[str]:
    """按分号切分 SQL 文件为单条语句；剥离纯注释段，保留行内 -- 注释给引擎。"""
    statements = []
    for chunk in sql_text.split(";"):
        # 去掉整段的 -- 注释行后若还有内容，才是真正要执行的语句
        lines = [ln for ln in chunk.strip().splitlines() if not ln.strip().startswith("--")]
        body = "\n".join(lines).strip()
        if body:
            statements.append(body)
    return statements


def run_file(engine, sql_path: Path) -> list[tuple[str, pd.DataFrame]]:
    """执行单个 .sql 文件的全部语句，返回 [(语句摘要, 结果集)]。"""
    sql_text = sql_path.read_text(encoding="utf-8")
    out = []
    with engine.connect() as conn:
        for i, stmt in enumerate(split_statements(sql_text), 1):
            head = next((ln for ln in stmt.splitlines() if ln.strip()), "")[:80]
            logger.info("执行 %s 第 %d 条语句: %s...", sql_path.name, i, head)
            df = pd.read_sql(text(stmt), conn)
            out.append((f"`{sql_path.name}` 语句 #{i}", df))
    return out


def main(argv: list[str]) -> int:
    flt = argv[1] if len(argv) > 1 else "q2_"
    sql_files = sorted(p for p in HERE.glob("*.sql") if flt in p.name)
    if not sql_files:
        logger.error("未找到匹配 %s 的 .sql 文件", flt)
        return 1

    engine = get_engine()
    sections = [f"# 模块 2 SQL 查询结果\n\n> 由 run_sql.py 自动生成，"
                f"执行环境：本机 MySQL 8.0（olist 库），原始灌库 schema（无二级索引）。\n"]
    for sql_path in sql_files:
        logger.info("==== %s ====", sql_path.name)
        sections.append(f"\n## {sql_path.name}\n")
        try:
            for label, df in run_file(engine, sql_path):
                sections.append(f"\n### {label}（{len(df)} 行）\n")
                sections.append(df.to_markdown(index=False))
        except Exception as exc:  # noqa: BLE001 单文件失败不阻塞其他文件
            logger.error("%s 执行失败: %s", sql_path.name, exc)
            sections.append(f"\n> 执行失败：{exc}")
            return 1

    RESULTS_MD.write_text("\n".join(sections), encoding="utf-8")
    logger.info("结果已写入 %s", RESULTS_MD)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
