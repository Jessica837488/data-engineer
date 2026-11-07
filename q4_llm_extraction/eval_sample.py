# -*- coding: utf-8 -*-
"""
Q4.4 评估抽样工具：从 extracted_issues.json 分层抽取 30 条生成标注工作表。
分层维度：source（rule/llm）× 是否有 issue × 类别。
人工标注流程：打开 _eval_sample.json，对每条 human_label 填：
  category_correct: true/false（issues 为空时填 agree: true/false）
  severity_correct: true/false
  evidence_faithful: true/false（证据是否忠实原文）
完成后运行本脚本 --score 汇总指标写入 accuracy_evaluation.md 的数据。
"""
import json
import random
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "extracted_issues.json"
OUT = HERE / "_eval_sample.json"

N = 30


def make_sample():
    random.seed(42)  # 可重现
    data = json.loads(SRC.read_text(encoding="utf-8"))
    llm = [r for r in data if r["source"] == "llm"]
    rule = [r for r in data if r["source"] == "rule"]

    def bucket(r):
        cats = sorted({i["category"] for i in r["result"]["issues"]})
        return (r["source"], "no_issue" if not cats else cats[0])

    groups = {}
    for r in data:
        groups.setdefault(bucket(r), []).append(r)
    print("分层分布：")
    for k, v in sorted(groups.items(), key=lambda x: -len(x[1])):
        print(f"  {k}: {len(v)}")

    # 配额：llm 有 issue 15、llm 无 issue 5、rule 5、其余 5
    quotas = [("llm", 15, True), ("llm", 5, False), ("rule", 5, None), (None, 5, None)]
    sample = []
    for src, n, has_issue in quotas:
        pool = [r for r in (llm if src == "llm" else rule if src == "rule" else data)
                if (bool(r["result"]["issues"]) == has_issue) or has_issue is None]
        # 组内按类别再均衡
        by_cat = {}
        for r in pool:
            b = bucket(r)
            by_cat.setdefault(b, []).append(r)
        cats = sorted(by_cat, key=lambda k: -len(by_cat[k]))
        picked, ci = [], 0
        while len(picked) < n and any(by_cat.values()):
            k = cats[ci % len(cats)]
            if by_cat.get(k):
                picked.append(by_cat[k].pop(random.randrange(len(by_cat[k]))))
            ci += 1
        sample.extend(picked[:n])

    for r in sample:
        r["_human_label"] = {"category_correct": None, "severity_correct": None,
                             "evidence_faithful": None, "note": ""}
    OUT.write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已生成 {len(sample)} 条标注工作表 -> {OUT.name}")
    print("来源分布:", Counter(r["source"] for r in sample))


def score():
    data = json.loads(OUT.read_text(encoding="utf-8"))
    labeled = [r for r in data if r["_human_label"]["category_correct"] is not None]
    n = len(labeled)
    if n == 0:
        print("还没有标注数据")
        return
    cat_ok = sum(1 for r in labeled if r["_human_label"]["category_correct"])
    sev_ok = sum(1 for r in labeled if r["_human_label"]["severity_correct"])
    ev_ok = sum(1 for r in labeled if r["_human_label"]["evidence_faithful"])
    print(f"n={n} 类别准确率={cat_ok / n:.0%} severity 准确率={sev_ok / n:.0%} "
          f"证据忠实率={ev_ok / n:.0%}")


if __name__ == "__main__":
    if "--score" in sys.argv:
        score()
    else:
        make_sample()
