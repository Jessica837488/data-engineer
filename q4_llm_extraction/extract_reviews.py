# -*- coding: utf-8 -*-
"""
模块 4：Olist 差评（review_score <= 3）结构化抽取

独立运行方式（仓库根目录）：
    python q4_llm_extraction/extract_reviews.py

三层流水线（分流策略，决策依据见 pipeline_design.md）：
  L0 规则层   无文本 / 短模板消息 -> 直接映射，不进 LLM（约 9,000 条）
  L1 缓存层   相同消息只请求一次 LLM，结果按消息复用（消息去重率 ~6%+）
  L2 LLM 层   有实质内容的差评 -> 批量请求（8 条/次），OpenAI 兼容协议

成本控制：
  - 请求前估算、运行中熔断（BUDGET_USD 上限，默认 $3）
  - 增量缓存 _llm_cache.json：中断重跑不重复付费
  - token 用量实记，写入 _cost_stats.json 供 cost_report.md 引用

产出：extracted_issues.json（每条差评一条记录，source 标注 rule/llm/cache）
"""

import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CSV_PATH = ROOT / "data" / "olist" / "olist_order_reviews_dataset.csv"
OUT_PATH = HERE / "extracted_issues.json"
CACHE_PATH = HERE / "_llm_cache.json"
STATS_PATH = HERE / "_cost_stats.json"
LOG_PATH = HERE / "extract_log.txt"

sys.path.insert(0, str(ROOT))
from prompt_loader import load_prompts  # noqa: E402  解析 prompt_template.txt（交付物本体）

PROMPTS = load_prompts(HERE / "prompt_template.txt")

BUDGET_USD = 3.0
BATCH_SIZE = 16         # 每次请求打包的评论数（成本对比实验见 cost_report）
MAX_WORKERS = 8         # 并发线程数（免费档限速下实测调整）
MAX_RETRIES = 1

# 参考单价（USD / 1M tokens），用于"实际成本估算"；智谱 flash 档免费则为 0
PRICE = {"input_per_m": 0.0, "output_per_m": 0.0,
         # 参照 DeepSeek-V3 定价折算的"影子成本"，用于成本报告对比
         "shadow_input_per_m": 0.27, "shadow_output_per_m": 1.10}

# ---------- L0 规则层：葡萄牙语高频模板与极简消息词典 ----------
# 依据 _probe_m4.py 实测的 ≤3 分差评高频消息构建；每条都来自真实数据
RULE_BOOK = {
    # "Não recebi" 家族（实测 TOP1：Não recebi o produto 86 次）
    "nao recebi o produto": {"issues": [{"category": "delivery_late", "severity": "high",
                                         "evidence_quote": "Não recebi o produto",
                                         "evidence_quote_en": "I did not receive the product"}],
                             "sentiment_score": -0.8, "actionable_for_seller": False},
    "ainda nao recebi o produto": {"issues": [{"category": "delivery_late", "severity": "high",
                                               "evidence_quote": "ainda não recebi o produto",
                                               "evidence_quote_en": "I still have not received the product"}],
                                   "sentiment_score": -0.8, "actionable_for_seller": False},
    "nao recebi": {"issues": [{"category": "delivery_late", "severity": "high",
                               "evidence_quote": "Não recebi", "evidence_quote_en": "I did not receive it"}],
                   "sentiment_score": -0.8, "actionable_for_seller": False},
    "produto nao entregue": {"issues": [{"category": "delivery_late", "severity": "high",
                                         "evidence_quote": "Produto não entregue",
                                         "evidence_quote_en": "Product not delivered"}],
                             "sentiment_score": -0.9, "actionable_for_seller": False},
    # 打低分却写好话的"矛盾样本"（实测：Bom 75 次、ok 20 次、recomendo 17 次）
    "bom": {"issues": [], "sentiment_score": 0.3, "actionable_for_seller": False},
    "ok": {"issues": [], "sentiment_score": 0.0, "actionable_for_seller": False},
    "boa": {"issues": [], "sentiment_score": 0.3, "actionable_for_seller": False},
    "recomendo": {"issues": [], "sentiment_score": 0.4, "actionable_for_seller": False},
    "otimo": {"issues": [], "sentiment_score": 0.5, "actionable_for_seller": False},
    "muito bom": {"issues": [], "sentiment_score": 0.5, "actionable_for_seller": False},
    "pessimo": {"issues": [{"category": "other", "severity": "medium",
                            "evidence_quote": "Péssimo", "evidence_quote_en": "Terrible"}],
                "sentiment_score": -0.9, "actionable_for_seller": False},
    "ruim": {"issues": [{"category": "other", "severity": "medium",
                         "evidence_quote": "Ruim", "evidence_quote_en": "Bad"}],
             "sentiment_score": -0.7, "actionable_for_seller": False},
    "gostei": {"issues": [], "sentiment_score": 0.4, "actionable_for_seller": False},
    "excelente": {"issues": [], "sentiment_score": 0.6, "actionable_for_seller": False},
    "regular": {"issues": [], "sentiment_score": -0.2, "actionable_for_seller": False},
}

VALID_CATEGORIES = {"delivery_late", "wrong_product", "quality_defect",
                    "missing_item", "seller_communication", "packaging", "other"}

# L0.5 规则兜底：LLM 返回空 issues 时的高置信关键词模式（证据取自原文匹配子串）
# 模式依据试跑抽查与 Olist 差评常见主题；正则按葡语常见变体（含/不含重音）
RULE_RESCUE = [
    (r"n[aã]o receb\w*|n[aã]o chegou|nao chegou", "delivery_late", "high"),
    (r"produto (errado|diferente|outro)", "wrong_product", "high"),
    (r"avariad\w+|defeito|quebrad\w+|danificad\w+", "quality_defect", "medium"),
    (r"faltand\w+|veio incompleto|falta(ndo)? (pe|itens)", "missing_item", "medium"),
    (r"mal embalad\w+|embalagem", "packaging", "low"),
]


def rule_rescue(msg: str, result: dict) -> bool:
    """空 issues 时用关键词补一条 issue（证据=原文实际子串）。返回是否补了。"""
    if result.get("issues"):
        return False
    m_norm = norm_msg(msg)
    for pat, cat, sev in RULE_RESCUE:
        m = re.search(pat, m_norm)
        if m:
            # 用原始消息中同位置子串作为证据（避免规范化文本与原文不一致）
            start = re.search(pat, msg, flags=re.IGNORECASE)
            quote = msg[start.start():start.end()] if start else m.group(0)
            result["issues"] = [{"category": cat, "severity": sev,
                                 "evidence_quote": quote,
                                 "evidence_quote_en": ""}]
            result["confidence"] = min(result.get("confidence", 0.5), 0.4)
            return True
    return False

logger = logging.getLogger("extract")


def setup_logging() -> None:
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for h in (logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        logger.addHandler(h)


def norm_msg(s: str) -> str:
    """规则匹配用的消息规范化：小写、压空白、去首尾标点。"""
    s = re.sub(r"\s+", " ", str(s).strip().lower())
    return s.strip(" .!?")


def load_bad_reviews() -> pd.DataFrame:
    """加载 ≤3 分且含文本的差评。"""
    df = pd.read_csv(CSV_PATH)
    bad = df[(df["review_score"] <= 3) & df["review_comment_message"].notna()].copy()
    logger.info("≤3 分且有文本的差评：%s 条（≤3 总量 %s，无文本 %s 条走输出占位）",
                f"{len(bad):,}", f"{len(df[df['review_score'] <= 3]):,}", len(df[(df['review_score'] <= 3) & df['review_comment_message'].isna()]))
    return bad


def rule_extract(msg: str):
    """L0 规则层：命中返回规则结果，否则 None。"""
    return RULE_BOOK.get(norm_msg(msg))


def build_llm_client():
    """OpenAI 兼容客户端（DeepSeek/GLM/OpenAI 均适用，凭 .env 切换）。"""
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        logger.error("缺少 LLM_API_KEY（.env）。请先配置提供商凭据。")
        raise SystemExit(2)
    from openai import OpenAI  # 延迟导入：未配置时不强依赖 SDK
    return OpenAI(api_key=api_key,
                  base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")), \
        os.environ.get("LLM_MODEL", "deepseek-chat")


def _parse_json_array(text: str):
    """宽容解析：剥 markdown 围栏、允许控制字符、接受对象包裹的数组。"""
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    data = json.loads(text, strict=False)
    if isinstance(data, dict):
        data = data.get("results", data.get("reviews", [data]))
    return data if isinstance(data, list) else None


def _chat(client, model: str, messages: list, usage: dict) -> str:
    resp = client.chat.completions.create(model=model, messages=messages,
                                          temperature=0.1, max_tokens=3800)
    u = resp.usage
    usage["input_tokens"] += u.prompt_tokens
    usage["output_tokens"] += u.completion_tokens
    usage["requests"] += 1
    return resp.choices[0].message.content


def call_llm(client, model: str, items: list, usage: dict) -> list:
    """
    一次批量请求：items 为 [(idx, score, message)]。
    解析按输出中的 "id" 字段对齐（防漏答整批作废），缺失条目返回 None 由上层单条重试。
    """
    listing = "\n\n".join(
        f"[{i}] (nota {s}/5)\n\"\"\"{m}\"\"\"" for i, s, m in items)
    user = PROMPTS["USER_TEMPLATE"].format(n_items=len(items), listing=listing)
    msgs = [{"role": "system", "content": PROMPTS["SYSTEM_PROMPT"]},
            {"role": "user", "content": user}]
    aligned = {i: None for i, _, _ in items}
    for attempt in range(1 + MAX_RETRIES):
        try:
            text = _chat(client, model, msgs, usage)
            data = _parse_json_array(text)
            got = 0
            for obj in data or []:
                if isinstance(obj, dict) and obj.get("id") in aligned:
                    aligned[obj["id"]] = obj
                    got += 1
            if got == len(items):
                break
            logger.warning("批量 id 对齐 %d/%d（第 %d 次尝试），缺失条目将单条重试",
                           got, len(items), attempt + 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量解析失败（第 %d 次，%d 条）：%s", attempt + 1, len(items), str(exc)[:150])
    return [aligned[i] for i, _, _ in items]


def call_single(client, model: str, score: int, message: str, usage: dict):
    """批量缺失条目的单条补漏请求（用 SINGLE_TEMPLATE）。"""
    try:
        text = _chat(client, model,
                     [{"role": "system", "content": PROMPTS["SYSTEM_PROMPT"]},
                      {"role": "user", "content": PROMPTS["SINGLE_TEMPLATE"].format(
                          score=score, message=message)}], usage)
        data = _parse_json_array(text)
        return data[0] if isinstance(data, list) and data else data
    except Exception as exc:  # noqa: BLE001
        logger.warning("单条补漏失败：%s", str(exc)[:100])
        return None


def sanitize(obj, message: str):
    """防御性校验：类别白名单、severity 白名单、证据必须出自原文（防幻觉）。"""
    def _n(s: str) -> str:  # 空白规范化后比对，容忍引号/换行差异
        return re.sub(r"\s+", " ", str(s)).strip().lower()

    if not isinstance(obj, dict):
        return None
    issues = []
    for iss in obj.get("issues") or []:
        if not isinstance(iss, dict):
            continue
        quote = str(iss.get("evidence_quote", ""))
        if quote and _n(quote) not in _n(message):   # 幻觉证据 -> 丢弃该 issue
            continue
        cat = iss.get("category", "other")
        sev = iss.get("severity", "medium")
        if cat not in VALID_CATEGORIES:
            cat = "other"
        issues.append({"category": cat,
                       "severity": sev if sev in {"low", "medium", "high"} else "medium",
                       "evidence_quote": quote,
                       "evidence_quote_en": str(iss.get("evidence_quote_en", ""))})
    try:
        sent = float(obj.get("sentiment_score", 0.0))
    except (TypeError, ValueError):
        sent = 0.0
    try:
        conf = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return {"issues": issues,
            "sentiment_score": max(-1.0, min(1.0, sent)),
            "actionable_for_seller": bool(obj.get("actionable_for_seller", False)),
            "confidence": max(0.0, min(1.0, conf))}


def main() -> int:
    setup_logging()
    load_dotenv(ROOT / ".env")
    # 幂等保护：产物已存在时默认跳过（run_all 回归友好），
    # 设置 EXTRACT_FORCE=1 可强制重跑全量 LLM 抽取
    if OUT_PATH.exists() and not os.environ.get("EXTRACT_FORCE"):
        logger.info("[跳过] %s 已存在（默认不重复付费/耗时）。"
                    "如需强制重跑请设置环境变量 EXTRACT_FORCE=1", OUT_PATH.name)
        return 0
    t0 = time.time()
    logger.info("===== M4 差评抽取启动（batch=%d, workers=%d, budget=$%.2f）=====",
                BATCH_SIZE, MAX_WORKERS, BUDGET_USD)

    bad = load_bad_reviews()
    records = []
    llm_queue = {}          # norm 消息 -> [(行索引, score, message)]

    # ---- 分流 ----
    n_rule = 0
    for idx, row in bad.iterrows():
        msg = str(row["review_comment_message"])
        rule = rule_extract(msg)
        if rule:
            records.append({"review_id": row["review_id"], "order_id": row["order_id"],
                            "review_score": int(row["review_score"]), "message": msg,
                            "source": "rule", "result": rule})
            n_rule += 1
        else:
            llm_queue.setdefault(norm_msg(msg), []).append((idx, int(row["review_score"]), msg))
    logger.info("[分流] 规则层命中 %s 条；LLM 待处理不同消息 %s 种（覆盖 %s 条）",
                f"{n_rule:,}", f"{len(llm_queue):,}", f"{sum(len(v) for v in llm_queue.values()):,}")

    # ---- 缓存 ----
    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    todo = [k for k in llm_queue if k not in cache]
    logger.info("[缓存] 已有 %s 条缓存，本次需请求 %s 种消息", f"{len(cache):,}", f"{len(todo):,}")

    # ---- LLM 批量（带成本熔断与并发）----
    usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    client, model = build_llm_client()
    # EXTRACT_LIMIT 环境变量：试跑模式，只请求前 N 种消息（不设置则全量）
    limit = os.environ.get("EXTRACT_LIMIT")
    if limit:
        todo = todo[:int(limit)]
        logger.info("[试跑] EXTRACT_LIMIT=%s，只请求 %d 种消息", limit, len(todo))
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    est_in = sum(len(llm_queue[k][0][2]) + 30 for k in todo) / 1e6 * PRICE["shadow_input_per_m"]
    logger.info("[成本] 预估影子成本（按 DeepSeek 单价折算）约 $%.2f，超过 $%.2f 将熔断", est_in, BUDGET_USD)

    def shadow_cost(u):
        return (u["input_tokens"] / 1e6 * PRICE["shadow_input_per_m"] +
                u["output_tokens"] / 1e6 * PRICE["shadow_output_per_m"])

    done_batches = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(call_llm, client, model,
                               [llm_queue[k][0] for k in batch], usage): batch
                   for batch in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            results = fut.result()
            # 批量缺失的条目做一次单条补漏（质量优先于请求成本）
            for j, (key, res) in enumerate(zip(batch, results)):
                if res is None:
                    idx, score, msg = llm_queue[key][0]
                    res = call_single(client, model, score, msg, usage)
                    if res is not None:
                        usage["single_retries"] = usage.get("single_retries", 0) + 1
                        results[j] = res
                cache[key] = res
            done_batches += 1
            if shadow_cost(usage) > BUDGET_USD:
                logger.error("[熔断] 影子成本超过 $%.2f，停止发起新请求（已完成 %d/%d 批）",
                             BUDGET_USD, done_batches, len(batches))
                pool.shutdown(wait=False, cancel_futures=True)
                break
            if done_batches % 50 == 0:
                logger.info("[进度] %d/%d 批，token in=%s out=%s，影子成本 $%.3f",
                            done_batches, len(batches), f"{usage['input_tokens']:,}",
                            f"{usage['output_tokens']:,}", shadow_cost(usage))
                CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # ---- 汇总输出 ----
    n_llm, n_halluc_drop, n_rescued = 0, 0, 0
    for key, occurrences in llm_queue.items():
        res = cache.get(key)
        for idx, score, msg in occurrences:
            if res is None:
                result = {"issues": [], "sentiment_score": -0.5,
                          "actionable_for_seller": False, "confidence": 0.0}
                src = "fallback"
            else:
                result = res
                src = "llm"
                n_llm += 1
            cleaned = sanitize(result, msg)
            if cleaned and len(cleaned["issues"]) != len(result.get("issues", [])):
                n_halluc_drop += 1
            if cleaned and rule_rescue(msg, cleaned):
                n_rescued += 1
            row = bad.loc[idx]
            records.append({"review_id": row["review_id"], "order_id": row["order_id"],
                            "review_score": score, "message": msg, "source": src,
                            "result": cleaned or result})
    # 无文本差评占位记录（保证输出覆盖全部 ≤3 差评）
    df_all = pd.read_csv(CSV_PATH)
    for _, row in df_all[(df_all["review_score"] <= 3) & df_all["review_comment_message"].isna()].iterrows():
        records.append({"review_id": row["review_id"], "order_id": row["order_id"],
                        "review_score": int(row["review_score"]), "message": None,
                        "source": "no_text",
                        "result": {"issues": [], "sentiment_score": -0.6,
                                   "actionable_for_seller": False, "confidence": 1.0}})

    OUT_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    stats = {"usage": usage, "real_cost_usd": 0.0,
             "shadow_cost_usd": round(shadow_cost(usage), 4),
             "records": len(records), "rule": n_rule, "llm": n_llm,
             "fallback": sum(1 for r in records if r["source"] == "fallback"),
             "no_text": sum(1 for r in records if r["source"] == "no_text"),
             "halluc_evidence_dropped": n_halluc_drop,
             "rule_rescued": n_rescued,
             "elapsed_sec": round(time.time() - t0, 1)}
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("===== 完成：%s 条记录（rule=%s llm=%s no_text=%s fallback=%s），"
                "丢弃幻觉证据 %s 条、规则兜底补回 %s 条，耗时 %.0fs =====",
                f"{len(records):,}", f"{n_rule:,}", f"{n_llm:,}", stats["no_text"],
                stats["fallback"], n_halluc_drop, n_rescued, stats["elapsed_sec"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
