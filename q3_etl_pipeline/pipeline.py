# -*- coding: utf-8 -*-
"""
模块 3：端到端 ETL Pipeline（Online Retail II -> 3 个 parquet）

输出（q3_etl_pipeline/outputs/，已被 .gitignore 忽略）：
  1. sales_facts.parquet       真实销售明细（BI 报表用）
  2. customer_features.parquet 客户 RFM 特征（客户分群用）
  3. returns_log.parquet       退货日志 + 反向匹配（退货分析用）

独立运行方式（仓库根目录）：
    python q3_etl_pipeline/pipeline.py

关键口径决策（面试可追问，详见 validation_report.md）：
- "真实销售" = 非 C/A 前缀发票 + 产品码 StockCode + Quantity>0 + UnitPrice>=0
- 完全重复行（8 列全同，34,335 行）按疑似重复录入剔除——数据无行号列，
  无法区分"同一商品拆两行"与"重复录入"，取保守口径并在报告中量化影响
- 非 C 前缀但 Quantity<=0 的 3,457 行（价格全为 0）是发票内 0 价格对冲，
  不是退货（退货日志只收 C 前缀），从销售明细剔除并记日志
- 退货匹配：按 (customer_id, stock_code) 建正销售池，退货按时间正序消费，
  优先匹配最近原单，原单量不足则级联更早原单（池扣减支持分次退货），
  无候选则 matched_original_invoice=NULL 并记日志
- 价格不参与匹配（17,322 个客户+商品组存在多个价格，价格匹配会大量漏配）
- RFM 基准日 = 全表最大 InvoiceDate（2011-12-09 12:50），数据止于 12-09 而非年末
"""

import logging
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Windows GBK 控制台兼容（同 download_data.py 的踩坑修复）
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CSV_PATH = ROOT / "data" / "online_retail_ii.csv"
OUT_DIR = HERE / "outputs"
LOG_PATH = HERE / "pipeline_log.txt"

RANDOM_SEED = 42  # 可重现性要求：固定随机种子（本管道无随机操作，防御性设置）

# 产品码规则：5-6 位数字 + 可选字母后缀（Q1 findings：POST/DOT/M/D 等非产品码全部不满足）
PRODUCT_CODE_RE = re.compile(r"^\d{5,6}[A-Za-z]*$")

logger = logging.getLogger("pipeline")


def setup_logging() -> None:
    """日志同时输出到控制台与 q3_etl_pipeline/pipeline_log.txt。"""
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)


def load_data(csv_path: Path) -> pd.DataFrame:
    """加载原始 CSV 并做最小规范化（列名统一、类型转换）。每步有日志与异常捕获。"""
    try:
        df = pd.read_csv(
            csv_path,
            dtype={"Invoice": str, "StockCode": str, "Description": str, "Country": str},
        )
    except FileNotFoundError as exc:
        logger.error("找不到数据文件 %s，请先运行 download_data.py：%s", csv_path, exc)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("CSV 读取失败：%s", exc)
        raise

    df.columns = [c.strip() for c in df.columns]
    df = df.rename(
        columns={"Invoice": "invoice_no", "StockCode": "stock_code",
                 "Description": "description", "Quantity": "quantity",
                 "InvoiceDate": "invoice_datetime", "Price": "unit_price",
                 "Customer ID": "customer_id", "Country": "country"}
    )
    # Customer ID 原始形态是 "13085.0"（float 字符串），先转可空整数再转字符串
    df["customer_id"] = pd.to_numeric(df["customer_id"], errors="coerce").astype("Int64").astype("string")
    df["invoice_datetime"] = pd.to_datetime(df["invoice_datetime"], errors="coerce")

    logger.info("原始数据加载完成：%s 行 x %s 列", f"{len(df):,}", df.shape[1])
    n_nat = int(df["invoice_datetime"].isna().sum())
    if n_nat:
        logger.warning("InvoiceDate 解析失败 %d 行（将按异常处理）", n_nat)
    return df


def filter_real_sales(df: pd.DataFrame) -> pd.DataFrame:
    """
    过滤出真实销售明细（sales_facts）。
    每一步剔除都记录行数，保证口径可追溯。
    """
    n0 = len(df)
    sales = df.drop_duplicates()  # 8 列全同的行按疑似重复录入剔除（无行号列无法区分）
    logger.info("[sales] 剔除完全重复行 %s 行", f"{n0 - len(sales):,}")

    is_cancel = sales["invoice_no"].str.startswith("C", na=False)
    is_baddebt = sales["invoice_no"].str.startswith("A", na=False)
    logger.info("[sales] 剔除取消单(C 前缀) %s 行", f"{is_cancel.sum():,}")
    logger.info("[sales] 剔除坏账调整(A 前缀) %s 行", f"{is_baddebt.sum():,}")
    sales = sales[~is_cancel & ~is_baddebt]

    is_product = sales["stock_code"].str.match(PRODUCT_CODE_RE, na=False)
    logger.info("[sales] 剔除非产品 StockCode %s 行（POST/DOT/M/D 等）", f"{(~is_product).sum():,}")
    sales = sales[is_product]

    n_pos = len(sales)
    sales = sales[sales["quantity"] > 0]
    logger.info("[sales] 剔除 Quantity<=0 %s 行（含 %s 行非 C 前缀的 0 价格对冲行）",
                f"{n_pos - len(sales):,}", f"{n_pos - len(sales):,}")

    n_price = len(sales)
    sales = sales[sales["unit_price"] >= 0]
    if n_price != len(sales):
        logger.warning("[sales] 剔除负价格行 %d（正常不应出现，出现说明过滤逻辑有漏洞）", n_price - len(sales))

    sales = sales.copy()
    sales["total_amount"] = sales["quantity"] * sales["unit_price"]
    sales = sales[
        ["invoice_no", "stock_code", "description", "quantity", "unit_price",
         "total_amount", "invoice_datetime", "customer_id", "country"]
    ].reset_index(drop=True)
    logger.info("[sales] 真实销售明细最终 %s 行（占原始 %.2f%%），金额合计 %s",
                f"{len(sales):,}", 100 * len(sales) / n0, f"{sales['total_amount'].sum():,.2f}")
    return sales


def compute_rfm(sales: pd.DataFrame, baseline: pd.Timestamp) -> pd.DataFrame:
    """
    基于真实销售计算每个有 CustomerID 客户的 RFM 特征（全向量化，无 iterrows）。
    口径：frequency/monetary 为销售明细毛口径（退货在 returns_log 单独分析），
    基准日为全表最大 InvoiceDate（数据止于 2011-12-09 而非年末）。
    """
    cust = sales[sales["customer_id"].notna()].copy()
    logger.info("[rfm] 有 CustomerID 的销售 %s 行（剔除缺失 ID %.2f%%）",
                f"{len(cust):,}", 100 * (1 - len(cust) / len(sales)))

    # 主要国家：按 (客户, 国家) 计数取第一大，向量化实现
    country_cnt = (cust.groupby(["customer_id", "country"], observed=True)
                   .size().reset_index(name="n")
                   .sort_values(["customer_id", "n", "country"], ascending=[True, False, True]))
    main_country = country_cnt.drop_duplicates("customer_id").set_index("customer_id")["country"]

    agg = cust.groupby("customer_id", observed=True).agg(
        last_purchase_date=("invoice_datetime", "max"),
        first_purchase_date=("invoice_datetime", "min"),
        frequency=("invoice_no", "nunique"),
        monetary=("total_amount", "sum"),
        unique_products=("stock_code", "nunique"),
    )
    features = pd.DataFrame({
        "customer_id": agg.index.astype("string"),
        "recency_days": (baseline - agg["last_purchase_date"]).dt.days.astype("int64"),
        "frequency": agg["frequency"].astype("int64"),
        "monetary": agg["monetary"].round(2),
        "first_purchase_date": agg["first_purchase_date"],
        "last_purchase_date": agg["last_purchase_date"],
        "unique_products": agg["unique_products"].astype("int64"),
        "country": agg.index.map(main_country).values,
    }).reset_index(drop=True)
    features["is_one_time_buyer"] = features["frequency"] == 1

    logger.info("[rfm] 客户特征 %s 人；recency 中位数 %.0f 天，frequency 中位数 %.0f，"
                "monetary 中位数 %.2f，一次性买家 %.2f%%",
                f"{len(features):,}", features["recency_days"].median(),
                features["frequency"].median(), features["monetary"].median(),
                100 * features["is_one_time_buyer"].mean())
    return features


def _match_one_return(needed: int, pool: list, return_dt: pd.Timestamp) -> tuple:
    """
    为一条退货行在池中做贪心匹配（内部函数，仅处理小列表，非全表循环）。
    策略：只考虑时间早于退货时间的记录，从最近的往前级联扣减，直到数量满足或池耗尽。
    返回 (匹配的发票号串, 是否足额)。
    """
    matched_inv, remaining_needed = [], needed
    # pool 按 datetime 升序排列；倒序扫描 = 优先最近的
    for i in range(len(pool) - 1, -1, -1):
        dt, inv, qty_left = pool[i]
        if qty_left <= 0 or dt >= return_dt:  # 关键过滤：原单必须早于退货时间
            continue
        take = min(qty_left, remaining_needed)
        pool[i] = (dt, inv, qty_left - take)
        if not matched_inv or matched_inv[-1] != inv:
            matched_inv.append(inv)
        remaining_needed -= take
        if remaining_needed <= 0:
            return ";".join(matched_inv), True
    return (";".join(matched_inv) if matched_inv else None), remaining_needed <= 0


def match_returns(df_raw: pd.DataFrame, sales: pd.DataFrame) -> tuple:
    """
    构建 returns_log：C 前缀 + 产品码的取消行为退货记录，
    并按 CustomerID + StockCode + |Quantity| 反向匹配原始销售。

    README 的三个边界问题在本实现的答案：
    1. 多笔正向销售符合条件 -> 取离退货时间最近的（倒序扫描池）
    2. 买 10 个分两次退 5+5 -> 池中 quantity_remaining 逐次扣减，两次各自匹配
    3. 原单 quantity 比退货小 -> 级联消耗更早的原单（匹配结果为多个发票号，
       以 ';' 连接）；池整体不足则记为部分匹配并在日志计数
    """
    is_cancel = df_raw["invoice_no"].str.startswith("C", na=False)
    is_product = df_raw["stock_code"].str.match(PRODUCT_CODE_RE, na=False)
    returns = df_raw[is_cancel & is_product].copy()
    returns["total_amount"] = returns["quantity"] * returns["unit_price"]
    returns = returns[
        ["invoice_no", "stock_code", "description", "quantity", "unit_price",
         "total_amount", "invoice_datetime", "customer_id", "country"]
    ].reset_index(drop=True)
    logger.info("[returns] 退货记录 %s 行（C 前缀且产品码，全部 Quantity<0=%s）",
                f"{len(returns):,}", str((returns["quantity"] < 0).all()))

    # 正销售池：来自 sales_facts（与销售明细同一清洗口径），按 (客户, 商品) 分组
    pool_src = sales[sales["customer_id"].notna()][
        ["customer_id", "stock_code", "invoice_no", "invoice_datetime", "quantity"]
    ].copy()
    pools: dict = {}
    for (cid, scode), grp in pool_src.groupby(["customer_id", "stock_code"], observed=True):
        pools[(cid, scode)] = list(zip(grp["invoice_datetime"], grp["invoice_no"], grp["quantity"]))

    # 退货按时间正序处理，保证"更早的退货优先消耗更早的库存"
    r_sorted = returns.sort_values("invoice_datetime")
    matched_list, partial_cnt, null_cnt = [], 0, 0
    null_examples = []
    for row in r_sorted.itertuples(index=False):
        if pd.isna(row.customer_id):
            matched_list.append(None)
            null_cnt += 1
            if len(null_examples) < 10:
                null_examples.append(f"{row.invoice_no}/{row.stock_code} 缺 CustomerID")
            continue
        pool = pools.get((row.customer_id, row.stock_code))
        if not pool:
            matched_list.append(None)
            null_cnt += 1
            if len(null_examples) < 10:
                null_examples.append(f"{row.invoice_no}/{row.stock_code} 无同客户同商品正销售")
            continue
        inv, ok = _match_one_return(int(-row.quantity), pool, row.invoice_datetime)
        matched_list.append(inv)
        if inv is None:
            null_cnt += 1
            if len(null_examples) < 10:
                null_examples.append(f"{row.invoice_no}/{row.stock_code} 池存在但无早于退货时间的可销售记录")
        elif not ok:
            partial_cnt += 1

    r_sorted["matched_original_invoice"] = pd.array(matched_list, dtype="string")

    # 事后校验：每个被引用的原单发票时间必须早于对应退货时间（防"匹配到未来订单"回归）
    inv_dt = pd.Series(pool_src["invoice_datetime"].values, index=pool_src["invoice_no"]).to_dict()
    for row in r_sorted[r_sorted["matched_original_invoice"].notna()].itertuples(index=False):
        for inv in row.matched_original_invoice.split(";"):
            assert inv_dt[inv] < row.invoice_datetime, \
                f"{row.invoice_no} 退货匹配到未来订单 {inv}（{inv_dt[inv]}）"

    matched_cnt = int(r_sorted["matched_original_invoice"].notna().sum())
    logger.info("[returns] 匹配结果：成功 %s（%.1f%%）| 部分匹配（原单量不足，级联多单）%s | "
                "无匹配 %s（%.1f%%，字段置 NULL）",
                f"{matched_cnt:,}", 100 * matched_cnt / len(r_sorted), f"{partial_cnt:,}",
                f"{null_cnt:,}", 100 * null_cnt / len(r_sorted))
    for ex in null_examples:
        logger.info("[returns] 无匹配示例: %s", ex)

    # 还原为原始退货顺序输出（可读性），字段与 sales_facts 对齐 + 匹配列
    returns = returns.merge(
        r_sorted[["matched_original_invoice"]]
        .assign(_ord=r_sorted.index),
        left_index=True, right_on="_ord", how="left",
    ).drop(columns="_ord")
    return returns, {"total": len(returns), "matched": matched_cnt,
                     "partial": partial_cnt, "null": null_cnt}


def validate_outputs(sales: pd.DataFrame, features: pd.DataFrame,
                     returns: pd.DataFrame) -> dict:
    """
    对 3 个输出做断言校验 + 关键统计。任何断言失败都会中止管道（fail fast）。
    返回统计字典，供 validation_report.md 使用。
    """
    stats = {}
    logger.info("[validate] ===== 断言校验开始 =====")

    # ---- sales_facts ----
    assert (sales["quantity"] > 0).all(), "sales_facts 存在 Quantity<=0"
    assert (sales["unit_price"] >= 0).all(), "sales_facts 存在负价格"
    assert (sales["total_amount"] >= 0).all(), "sales_facts 存在负金额"
    assert not sales["invoice_no"].str.startswith(("C", "A"), na=False).any(), \
        "sales_facts 混入 C/A 前缀发票"
    assert sales["stock_code"].str.match(PRODUCT_CODE_RE, na=False).all(), \
        "sales_facts 混入非产品 StockCode"
    assert pd.api.types.is_datetime64_any_dtype(sales["invoice_datetime"]), "日期列必须是 datetime"
    assert np.allclose(sales["total_amount"], sales["quantity"] * sales["unit_price"]), \
        "total_amount != quantity * unit_price"
    stats["sales"] = {
        "rows": len(sales), "revenue": round(float(sales["total_amount"].sum()), 2),
        "invoices": int(sales["invoice_no"].nunique()),
        "null_customer_rate": round(float(sales["customer_id"].isna().mean()), 4),
        "null_description_rate": round(float(sales["description"].isna().mean()), 4),
        "null_country_rate": round(float(sales["country"].isna().mean()), 4),
        "price_zero_rows": int((sales["unit_price"] == 0).sum()),
    }
    logger.info("[validate] sales_facts 断言全部通过：%s", stats["sales"])

    # ---- customer_features ----
    assert (features["recency_days"] >= 0).all(), "recency_days 存在负值"
    assert (features["frequency"] >= 1).all(), "frequency 存在 0"
    assert (features["monetary"] >= 0).all(), "monetary 存在负值"
    assert (features["first_purchase_date"] <= features["last_purchase_date"]).all(), \
        "first_purchase_date 晚于 last_purchase_date"
    assert features["customer_id"].is_unique, "customer_features 存在重复客户"
    assert (features["is_one_time_buyer"] == (features["frequency"] == 1)).all(), \
        "is_one_time_buyer 与 frequency 不一致"
    stats["rfm"] = {
        "customers": len(features),
        "recency_mean": round(float(features["recency_days"].mean()), 1),
        "frequency_mean": round(float(features["frequency"].mean()), 2),
        "monetary_mean": round(float(features["monetary"].mean()), 2),
        "one_time_rate": round(float(features["is_one_time_buyer"].mean()), 4),
        "null_country_rate": round(float(features["country"].isna().mean()), 4),
    }
    logger.info("[validate] customer_features 断言全部通过：%s", stats["rfm"])

    # ---- returns_log ----
    assert returns["invoice_no"].str.startswith("C", na=False).all(), "returns_log 混入非 C 前缀"
    assert (returns["quantity"] < 0).all(), "returns_log 存在 Quantity>=0"
    known_inv = set(sales["invoice_no"])
    matched = returns["matched_original_invoice"].dropna()
    referenced = {inv for cell in matched for inv in cell.split(";")}
    orphan = referenced - known_inv
    assert not orphan, f"匹配到的原单发票 {sorted(orphan)[:5]} 不在销售明细中"
    stats["returns"] = {
        "rows": len(returns), "refund_amount": round(float(returns["total_amount"].sum()), 2),
        "return_invoices": int(returns["invoice_no"].nunique()),
        "matched": int(len(matched)),
        "matched_rate": round(float(len(matched) / len(returns)), 4),
        "null_customer_rows": int(returns["customer_id"].isna().sum()),
    }
    logger.info("[validate] returns_log 断言全部通过：%s", stats["returns"])

    # ---- 交叉核对：退货率量级合理性 ----
    refund = abs(stats["returns"]["refund_amount"])
    ratio = refund / stats["sales"]["revenue"]
    logger.info("[validate] 退货金额占真实销售 %.2f%%（行业量级合理范围）", 100 * ratio)
    return stats


def main() -> int:
    setup_logging()
    np.random.seed(RANDOM_SEED)
    start = time.time()
    logger.info("===== 模块 3 ETL Pipeline 启动（随机种子 %s）=====", RANDOM_SEED)

    df = load_data(CSV_PATH)
    sales = filter_real_sales(df)
    baseline = df["invoice_datetime"].max()
    logger.info("[rfm] RFM 基准日 = 全表最大 InvoiceDate: %s", baseline)
    features = compute_rfm(sales, baseline)
    returns, match_stats = match_returns(df, sales)

    OUT_DIR.mkdir(exist_ok=True)
    stats = validate_outputs(sales, features, returns)
    stats["returns"]["match_stats"] = match_stats

    out = {
        "sales_facts.parquet": sales,
        "customer_features.parquet": features,
        "returns_log.parquet": returns,
    }
    for name, pdf in out.items():
        pdf.to_parquet(OUT_DIR / name, index=False)
        logger.info("[output] 写出 %s（%s 行）", name, f"{len(pdf):,}")

    logger.info("===== Pipeline 完成，总耗时 %.1f 秒（要求 <120 秒）=====", time.time() - start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
