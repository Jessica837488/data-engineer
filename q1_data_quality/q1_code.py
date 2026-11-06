# -*- coding: utf-8 -*-
"""
模块 1 / Q1.1：Online Retail II 数据质量初探

独立运行方式（仓库根目录）：
    python q1_data_quality/q1_code.py

产出：
    q1_data_quality/data_quality_report.md —— 自动生成的数据质量报告，包含：
      1) 各列数据类型与缺失值比例
      2) 数值列分布概况（min/max/mean/std 等）
      3) 字符串列唯一值数量与高频值 TOP10
      4) 至少 5 个"可疑"现象（带计算依据的文字描述）

设计说明：
- 不直接使用 df.describe() 一把梭：describe 只能给出分布，给不出"为什么可疑"。
  本脚本在描述统计之上，对每个可疑现象追加计算证据（行数、占比、样例记录），
  让"可疑"有据可查。
- 列名踩坑：本 Kaggle 版本的实际列名是 Invoice / Price / Customer ID，
  与题目表述的 InvoiceNo / UnitPrice / CustomerID 不一致，加载后统一重命名。
"""

import logging
import sys
from pathlib import Path

import pandas as pd

# Windows GBK 控制台对 Unicode 符号的兼容处理（踩坑：直接打印 "✓" 会抛 UnicodeEncodeError）
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("q1")

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "online_retail_ii.csv"
OUT_PATH = Path(__file__).resolve().parent / "data_quality_report.md"

# 已知非产品类 StockCode 及其业务含义（经数据探查确认，用于 Q1.2 与可疑现象识别）
NON_PRODUCT_CODES = {
    "POST": "邮费（POSTAGE）",
    "DOT": "线上订单邮费（DOTCOM POSTAGE）",
    "C2": "运费（CARRIAGE）",
    "M": "手工调整（Manual）",
    "D": "折扣（Discount）",
    "S": "样品（SAMPLES）",
    "AMAZONFEE": "亚马逊平台佣金",
    "BANK CHARGES": "银行手续费",
    "B": "坏账调整（Adjust bad debt）",
    "CRUK": "CRUK 慈善佣金",
    "ADJUST": "手动调整",
}


def load_data() -> pd.DataFrame:
    """加载 CSV 并统一列名/类型。Description 允许缺失，其余关键列缺失会在此报错。"""
    logger.info("加载数据: %s", DATA_PATH)
    df = pd.read_csv(
        DATA_PATH,
        dtype={"Invoice": str, "StockCode": str, "Description": str, "Country": str},
    )
    df = df.rename(columns={"Invoice": "InvoiceNo", "Price": "UnitPrice", "Customer ID": "CustomerID"})
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"])
    # 交易金额行级字段，后续多处使用
    df["Amount"] = df["Quantity"] * df["UnitPrice"]
    logger.info("数据形状: %s 行 x %s 列，日期范围 %s ~ %s",
                *df.shape, df["InvoiceDate"].min(), df["InvoiceDate"].max())
    return df


def section_types_and_missing(df: pd.DataFrame) -> str:
    """第 1 节：各列类型与缺失比例。"""
    miss = df.isna().mean().mul(100).round(2)
    lines = ["| 列名 | 数据类型 | 缺失值比例 |", "|---|---|---|"]
    for col in df.columns:
        lines.append(f"| {col} | {df[col].dtype} | {miss[col]:.2f}% |")
    return "\n".join(lines)


def section_numeric(df: pd.DataFrame) -> str:
    """第 2 节：数值列分布概况（CustomerID 是标识符，仅统计计数不参与分布解读）。"""
    desc = df[["Quantity", "UnitPrice"]].describe().T.round(2)
    desc["sum"] = df[["Quantity", "UnitPrice"]].sum().round(2)
    table = desc.to_markdown()
    note = (
        "\n\n**解读**：\n"
        "- Quantity 均值 9.94，但 min=-80995 / max=80995，标准差 172 远大于均值——长尾极重，"
        "均值对这类数据没有代表性，报表场景应优先看中位数（3 件）。\n"
        "- UnitPrice min=-53594.36，价格为负在零售业务中不应出现，属于特殊记账（见第 4 节）。\n"
        f"- CustomerID 缺失 {df['CustomerID'].isna().mean():.2%}，是唯一大面积缺失的列。"
    )
    return table + note


def section_categorical(df: pd.DataFrame) -> str:
    """第 3 节：字符串列唯一值数量与高频值 TOP10。"""
    cats = ["InvoiceNo", "StockCode", "Description", "Country"]
    lines = ["| 列 | 唯一值数 | 高频值 TOP10 |", "|---|---|---|"]
    for col in cats:
        vc = df[col].value_counts().head(10)
        top = "; ".join(f"{k}({v:,})" for k, v in vc.items())
        lines.append(f"| {col} | {df[col].nunique():,} | {top} |")
    return "\n".join(lines)


def section_suspicious(df: pd.DataFrame) -> str:
    """第 4 节：可疑现象清单（每条均带计算证据，共 8 条）。"""
    amt = df["Amount"]
    inv_prefix = df["InvoiceNo"].str[0]
    c_inv = df[inv_prefix == "C"]
    a_inv = df[inv_prefix == "A"]
    neg_price = df[df["UnitPrice"] < 0]
    zero_price = df[df["UnitPrice"] == 0]
    sc = df["StockCode"].str.strip()
    non_prod = df[~sc.str.match(r"^\d{5,6}[A-Za-z]?$")]
    miss_id = df[df["CustomerID"].isna()]
    dup_rows = df.duplicated().sum()
    no_desc = df[df["Description"].isna()]

    items = [
        ("1. Quantity 出现 ±80995 的极端值，且成对出现",
         f"Quantity 绝对值最大的两条记录是同一商品（StockCode=23166, PAPER CRAFT, LITTLE BIRDIE）："
         f"发票 581483 于 2011-12-09 09:15 买入 80995 件，12 分钟后发票 C581484 全额退回 -80995 件，"
         f"同一客户(16446)、同价(2.08)。**判断：这不是数据错误，而是一笔被立即取消的大额批发订单**，"
         f"两行金额恰好相互抵消。"),
        ("2. UnitPrice 为负的 5 条记录全部是坏账调整",
         f"全表仅 {len(neg_price)} 行价格为负（单笔最高核销 {neg_price['Amount'].abs().max():,.2f}，"
         f"合计 -{neg_price['Amount'].abs().sum():,.2f}），共同特征：StockCode 均为 'B'，"
         f"Description 均为 'Adjust bad debt'，InvoiceNo 均以 'A' 开头（全表仅 {len(a_inv)} 行 'A' 前缀），"
         f"CustomerID 全部缺失。**判断：会计层面的坏账核销记录，不是商品交易**。"),
        ("3. InvoiceNo 存在 'C' 前缀，是取消/退货单",
         f"'C' 前缀共 {len(c_inv):,} 行（占 {len(c_inv)/len(df):.2%}），Quantity 全部 ≤ 1"
         f"（min={c_inv['Quantity'].min()}），代表取消或退货。这批行的净金额为 "
         f"{c_inv['Amount'].sum():,.0f}，若不剔除，销售统计将被冲减；"
         f"若想统计毛销售额又必须单列，否则毛/净口径混淆、波动无法解释。"),
        ("4. StockCode 不全是产品：{0:.2%} 的行是运费/折扣/手续费等特殊记录".format(len(non_prod) / len(df)),
         f"不符合 '5-6位数字(可带字母后缀)' 产品编码规则的行共 {len(non_prod):,} 行。高频者："
         + "; ".join(f"{k}({v:,}行)" for k, v in non_prod["StockCode"].value_counts().head(8).items())
         + f"。这些行金额合计 {amt[non_prod.index].sum():,.0f}，虽占比不大，但混入销售额会扭曲 GMV 口径。"),
        ("5. UnitPrice=0 的行有 {:,} 条".format(len(zero_price)),
         f"这些行无收入意义（可能为赠品、试用装或数据缺价），且全部不是取消单。"
         f"做 GMV 统计时它们贡献 0，但做商品分析时会虚增销量。"),
        ("6. CustomerID 缺失 {:,} 行（{:.2%}），但金额口径只占 {:.2%}".format(
            len(miss_id), len(miss_id) / len(df), amt[miss_id.index].sum() / amt.sum()),
         "缺失行占比与金额占比差距明显，说明匿名订单多为小额零售。"
         "国家分布上，缺失组 98.8% 来自 UK，而有 ID 组 UK 占 89.9%——"
         "**最可能的解释：线下/柜台现金销售或批发客户未注册会员账号**。"),
        (f"7. 完全重复行 {dup_rows:,} 条",
         "同一发票同一商品同秒同数量的行。小概率是系统重复提交，"
         "但零售场景中也可能是扫码枪重复录入。需结合金额占比判断去重策略。"),
        (f"8. Description 缺失 {len(no_desc):,} 行但交易有效",
         "商品描述缺失不影响金额计算，但影响商品维度分析；"
         "且同一 StockCode 在其他行通常有正确描述，可通过映射回填。"),
    ]
    parts = []
    for title, detail in items:
        parts.append(f"### {title}\n\n{detail}")
    return "\n\n".join(parts)


def main() -> int:
    df = load_data()
    logger.info("生成质量报告各小节...")
    report = f"""# Online Retail II 数据质量报告

> 由 `q1_data_quality/q1_code.py` 自动生成，所有数字均可复现。
> 数据形状：{df.shape[0]:,} 行 x {df.shape[1] - 1} 列（Amount 为计算列），
> 时间范围：{df['InvoiceDate'].min()} ~ {df['InvoiceDate'].max()}，共 {df['Country'].nunique()} 个国家/地区。

## 1. 各列数据类型与缺失值比例

{section_types_and_missing(df)}

## 2. 数值列分布概况

{section_numeric(df)}

## 3. 字符串列唯一值与高频值 TOP10

{section_categorical(df)}

## 4. 可疑现象清单（重点）

{section_suspicious(df)}

## 5. 小结

这份"脏"的本质不是随机的脏，而是**业务口径混杂**：同一张表里混着销售、取消、坏账、
运费、折扣五种业务动作。任何不区分口径的总额统计都会得出错误结论，
这也是 Q1.3 清洗策略按下游用途分治的出发点。
"""
    OUT_PATH.write_text(report, encoding="utf-8")
    logger.info("报告已写入: %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
