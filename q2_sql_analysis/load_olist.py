# -*- coding: utf-8 -*-
"""
模块 2 准备工作：把 Olist 9 张 CSV 灌进本机 MySQL 8.0 的 olist 库

运行前提（二选一）：
1. 仓库根目录创建 .env 文件（已被 .gitignore 忽略，不会入库）：
       MYSQL_HOST=127.0.0.1
       MYSQL_PORT=3306
       MYSQL_USER=root
       MYSQL_PASSWORD=你的密码
2. 或设置同名环境变量。

独立运行方式（仓库根目录）：
    python q2_sql_analysis/load_olist.py

设计说明：
- 表名去掉 olist_ 前缀和 _dataset 后缀（customers / orders / order_items /
  order_reviews / products / ... / product_category_name_translation），
  与题目 SQL 中的表名（orders、customers、order_items、order_reviews）直接对齐。
- 灌库用 pandas.to_sql + SQLAlchemy + pymysql（README 官方 TOOLS.md 建议路径）。
- 不建二级索引：Q2.4 要求分析慢查询，先保留"裸表"作为性能基线。
"""

import logging
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_olist")

ROOT = Path(__file__).resolve().parent.parent
OLIST_DIR = ROOT / "data" / "olist"

# CSV 文件名 -> 目标表名
TABLE_MAP = {
    "olist_customers_dataset.csv": "customers",
    "olist_geolocation_dataset.csv": "geolocation",
    "olist_orders_dataset.csv": "orders",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "product_category_name_translation.csv": "product_category_name_translation",
}


def get_engine():
    """从 .env / 环境变量读取凭据，连接 MySQL 并确保 olist 库存在。"""
    import os
    load_dotenv(ROOT / ".env")
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = os.getenv("MYSQL_PORT", "3306")
    user = os.getenv("MYSQL_USER", "root")
    pwd = os.getenv("MYSQL_PASSWORD")
    if not pwd:
        logger.error("未找到 MYSQL_PASSWORD：请在仓库根目录 .env 或环境变量中提供")
        sys.exit(1)

    server_url = f"mysql+pymysql://{user}:{pwd}@{host}:{port}/?charset=utf8mb4"
    server = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        ver = conn.execute(text("SELECT VERSION()")).scalar()
        logger.info("连接成功，MySQL 版本: %s", ver)
        conn.execute(text("CREATE DATABASE IF NOT EXISTS olist CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))

    return create_engine(f"mysql+pymysql://{user}:{pwd}@{host}:{port}/olist?charset=utf8mb4")


def load_all(engine) -> None:
    """逐表灌库。已存在的表先删除重建，保证脚本可重复执行。"""
    for csv_name, table in TABLE_MAP.items():
        path = OLIST_DIR / csv_name
        if not path.exists():
            logger.warning("缺少 %s，跳过表 %s（请先运行 download_data.py）", csv_name, table)
            continue
        df = pd.read_csv(path)
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        df.to_sql(table, engine, if_exists="append", index=False, chunksize=50_000)
        logger.info("%-38s -> %-22s %7d 行", csv_name, table, len(df))


def verify(engine) -> None:
    """灌库后行数核对。"""
    with engine.connect() as conn:
        total = 0
        for table in TABLE_MAP.values():
            n = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            total += n
            logger.info("校验 %-30s %7d 行", table, n)
        logger.info("9 张表合计 %d 行", total)


def main() -> int:
    engine = get_engine()
    load_all(engine)
    verify(engine)
    logger.info("Olist 灌库完成 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
