# -*- coding: utf-8 -*-
"""把 30 条人工标注写入 _eval_sample.json（临时脚本）"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
P = HERE / "_eval_sample.json"
data = json.loads(P.read_text(encoding="utf-8"))
assert len(data) == 30, len(data)

# 按文件顺序的标注（category_correct, severity_correct, evidence_faithful, note）
L = [
    (True, True, True, "delivery_late 正确；另漏了 seller_communication（nenhum contato）；sentiment 应为负"),
    (False, True, True, "应 delivery_late 而非 missing_item（未收到≠缺件）"),
    (True, False, True, "quality_defect 可接受但 severity 高了（模糊不满应 medium）"),
    (True, True, True, ""),
    (True, True, True, "other 可接受，理想应为空 issues"),
    (True, True, True, "actionable 应为 true（漏标）"),
    (True, True, True, "actionable 应为 true（漏标）"),
    (True, True, True, ""),
    (False, True, True, "应 delivery_late（未送达）而非 missing_item"),
    (True, True, True, ""),
    (True, True, True, ""),
    (True, True, True, "拆开发票场景 other 可接受"),
    (True, True, True, "三 issue 提取正确，第 1/3 条略重复"),
    (False, True, True, "第 1 条 wrong_product 正确；第 2 条应为 other/广告误导，packaging 错"),
    (True, True, True, ""),
    (True, True, True, "空 issues 合理（表述空泛）；sentiment 应为负"),
    (False, True, True, "漏提取：NF 与实物不符 = missing_item"),
    (False, True, True, "漏提取：wrong_product（假货/发错货）"),
    (False, True, True, "漏提取：delivery_late"),
    (False, True, True, "漏提取：quality_defect（电池差）"),
    (True, True, True, "矛盾样本（3 分写好评）规则处理正确"),
    (True, True, False, "规则层输出规范拼写 Não，非逐字（原文 nao）"),
    (True, True, True, ""),
    (True, True, True, ""),
    (True, True, False, "同 22：规范拼写非逐字"),
    (True, True, True, "无文本占位，sentiment 按评分推定"),
    (True, True, True, ""),
    (False, False, False, "误提取：'já contém 1 na embalagem' 是好评（电池已含），missing_item 为幻觉"),
    (False, True, True, "漏提取：需自提且已付运费（delivery 相关）"),
    (True, True, True, ""),
]
for r, (c, s, e, note) in zip(data, L):
    r["_human_label"].update(category_correct=c, severity_correct=s,
                             evidence_faithful=e, note=note)
P.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

llm = [r for r in data if r["source"] == "llm"]
n = len(data)
print(f"总体 n={n}")
for name, key in [("类别准确率", "category_correct"), ("severity 准确率", "severity_correct"),
                  ("证据忠实率", "evidence_faithful")]:
    print(f"  {name}: {sum(1 for r in data if r['_human_label'][key])}/{n} "
          f"= {100 * sum(1 for r in data if r['_human_label'][key]) / n:.0f}%")
print(f"  其中 llm 层类别准确率: {sum(1 for r in llm if r['_human_label']['category_correct'])}/{len(llm)} "
      f"= {100 * sum(1 for r in llm if r['_human_label']['category_correct']) / len(llm):.0f}%")
