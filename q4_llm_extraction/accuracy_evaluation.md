# Q4.4 准确率评估（accuracy_evaluation.md）

## 评估方法（无 ground truth 的做法）

1. **机械校验（全量，代码实现）**：`sanitize()` 强制 evidence_quote 必须是原文
   （空白规范化后）子串，不匹配即丢弃——全量运行共丢弃 370 条幻觉证据；
   类别/severity 白名单兜底。这是"防幻觉"的第一道闸。
2. **分层人工标注（30 条）**：`eval_sample.py` 按 source × 是否有 issue × 类别
   分层抽样（seed=42，可复现），标注维度：category 正误 / severity 正误 /
   evidence 忠实度。标注工作表 `_eval_sample.json`，标注结论由 AI 辅助判定 +
   候选人复核（每条的 note 记录了判定理由，欢迎面试时逐条追问）。
3. **分层逻辑**：llm 有 issue 15 条、llm 无 issue 5 条、rule 5 条、no_text 等其余 5 条，
   覆盖全部 8 个来源-类别桶。

## 结果

| 指标 | 全体 30 条 | 其中 llm 层（24 条） |
|---|---|---|
| **类别准确率** | **70%**（21/30） | 62%（15/24） |
| **severity 准确率** | **93%**（28/30） | — |
| **证据忠实率** | **90%**（27/30） | 100%（机械校验保障） |

> 证据忠实率的两处失败都在 rule 层：规则词典输出规范拼写（"Não recebi o produto"），
> 而原文是无重音的 "nao recebi o produto"——不是幻觉，是"纠正式引用"，
> 修复方式是规则层直接摘录原文子串。

## 失败分类（9 条错误的结构）

| 类型 | 条数 | 典型案例 |
|---|---|---|
| **空 issues 漏提取** | 5 | "Não entregou até agora"（未送达）被返回空 issues——模型在批量模式下对短句偷懒 |
| **类别误判** | 3 | "Não recebi o produto ainda"→missing_item（应 delivery_late：未收到≠缺件）；反讽好评 "já contém 1 na embalagem（电池已含）" 被误判 missing_item |
| **部分正确** | 1 | 双 issue 中一条对一条错（广告误导被归 packaging） |
| **引用不逐字** | 2 | rule 层规范拼写（见上） |

**误提取率（幻觉 issue）**：30 条中 1 条（3.3%），且证据被机械校验拦截的
370 条未进入最终输出——**防幻觉机制有效但非完美**。

## 系统性发现（比准确率数字更有价值）

1. **sentiment_score 字段系统性失效**：glm-4-flash 几乎对所有文本返回 0.0
   （温度 0.1 下模型倾向中性默认值）。30 条中应有 ~25 条为负。**修复建议**：
   不信任 LLM 的 sentiment，改由 review_score 锚定 + issues 严重度加权推导——
   这属于"用规则修复 LLM 系统性偏差"的典型案例。
2. **actionable_for_seller 偏保守**：包装/发错货等卖家可行动的场景多被判 false
   （prompt 规则 7 过严），若下游用于卖家工单，需放宽该规则。
3. **模型分级策略的实证依据**：llm 层类别准确率 62% 说明 glm-4-flash 只够做
   "粗筛+引用定位"；生产环境建议 flash 档做分流与粗分类、置信度 <0.6 的
   子集升级到大模型复判——这与 Q5 的成本分层方案形成闭环。
4. **评估方法的局限**：标注者是 AI 辅助（已如实声明），边界样本（如"未收到"
   归 delivery_late 还是 missing_item）依赖 taxonomy 定义清晰度；题面未给出
   类别定义文档，这本身是 Q4 题目可改进点（已列入 REFLECTION）。
