# Q2.4 - 慢查询优化分析

## 原始 SQL

```sql
SELECT 
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS orders,
    SUM(oi.price) AS revenue,
    AVG(r.review_score) AS avg_score
FROM orders o
LEFT JOIN customers c ON o.customer_id = c.customer_id
LEFT JOIN order_items oi ON o.order_id = oi.order_id
LEFT JOIN order_reviews r ON o.order_id = r.order_id
WHERE o.order_status = 'delivered'
  AND o.order_purchase_timestamp >= '2017-01-01'
  AND LOWER(c.customer_city) LIKE '%sao%'
GROUP BY c.customer_state
ORDER BY revenue DESC;
```

> **实验环境说明（影响所有数字的解读）**：本卷的 olist 库是用 `pandas.to_sql` 直接灌入的
> （README TOOLS.md 建议路径），这个方法的副作用是**所有字符串列都成了 TEXT 类型、
> 无主键无索引、日期是 TEXT**。这不是"理想 schema"，反而让本题更有意思：
> 它暴露了"盲目加索引"和"不看列类型谈优化"都会翻车。以下所有数字均为本机
> MySQL 8.0.23 实测，EXPLAIN 证据见文末附录。

---

## 第 1 问：性能问题分析（至少 3 个）

### 问题 1：`LIKE '%sao%'` 前导通配符使索引彻底失效

`%` 在最前面，B+ 树按前缀有序的原理决定了**没有任何索引能服务这个条件**，
无论 `customer_city` 上建什么索引都必须全表扫描 98,753 行 customers。
`LOWER()` 函数包裹进一步放弃了大小写不敏感 collation（utf8mb4_unicode_ci 本身就
不区分大小写）带来的优化可能。

**为什么不能简单回答"加索引"**：给 city 加普通索引对这个查询毫无用处；
真正的出路是 ngram 全文索引（`WITH PARSER ngram`）或维护一张标准化城市维度表
把 `%sao%` 匹配变成等值/前缀匹配。实测数据：'Olist' 的城市名以 sao 开头的
（sao paulo 等）占绝大多数，如果业务上确认'%sao%'本意是"圣保罗都会区"，
应推动需求方改成显式城市列表——这是"改 SQL 不如改需求口径"的典型案例。

### 问题 2：列类型全是 TEXT，join 键上只能建前缀索引，且**加了反而更慢**

因为灌库方式产生 TEXT 列，ID 只能建**前缀索引**（如 `order_id(32)`）。
我在裸表上实测（见第 2 问的对比表）：

- 无索引：优化器选择 **hash join**（一次构建哈希表批量探测），原查询 **0.88s**；
- 加 TEXT 前缀索引后：优化器改走 **嵌套循环 + ref 逐行探测**，
  每次探测都要做 32 字符字符串比较，原查询变成 **30.2s，恶化 34 倍**。

这是本题最反直觉、也最有生产价值的发现：**"加索引"在数据量小、join 命中率高、
键是长字符串时可能大幅变慢**。EXPLAIN 显示加索引后 `order_items`/`order_reviews`
从 hash join 变成 `type=ref, rows=1`——单行看是"好计划"，整体是坏计划。

**根治方案不是调索引，而是修 schema**：ID 列应为 `CHAR(32)`（或 `BINARY(16)` 存 UUID），
日期应为 `DATETIME`，每表建主键。类型化之后索引才是"真索引"。

### 问题 3：`LEFT JOIN` + WHERE 右表条件 = 语义退化为 INNER JOIN

`WHERE c.customer_city LIKE '%sao%'` 会把 customers 的 LEFT JOIN 退化为 INNER JOIN
（city 为 NULL 的行被过滤）。本查询恰好"没问题"，但这是常见的口径陷阱：
写LEFT JOIN 的人以为"保留无城市客户"，实际没有。要么改写为 INNER JOIN 让意图明确，
要么把 city 条件挪到 ON 子句里保持 LEFT 语义。审核查询时这是必查项。

### 问题 4：`o.order_purchase_timestamp` 是 TEXT 列上的字符串范围比较

`>= '2017-01-01'` 对 ISO 格式 TEXT 恰好按字典序成立（侥幸正确），
但只要出现非标准格式（如 '2017/1/5'）就静默出错。类型化成 DATETIME 后，
该条件才能配合 `(order_status, order_purchase_timestamp)` 复合索引做范围扫描。

### 问题 5：`AVG(r.review_score)` 被订单的商品行数隐性加权

一单多商品时，该订单的评分在 AVG 中出现 N 次（N=商品行数）。
大订单客户对"平均分"的影响被放大 N 倍。这既是性能问题（join 产生的行数膨胀），
更是统计口径问题——见第 3 问。

---

## 第 2 问：优化方案

### 优化后的 SQL

核心思路：**先聚合、后 join，把一对多的明细/评价各自收敛成"一行一订单"**，
同时消除第 1 问问题 3 的 LEFT JOIN 语义陷阱：

```sql
WITH item_agg AS (            -- 明细按订单收敛：一行一订单
    SELECT oi.order_id, SUM(oi.price) AS revenue
    FROM order_items oi
    GROUP BY oi.order_id
),
review_agg AS (               -- 评价按订单收敛：一单一评分
    SELECT order_id, AVG(review_score) AS avg_score
    FROM order_reviews
    GROUP BY order_id
)
SELECT
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS orders,
    SUM(i.revenue)             AS revenue,     -- 不再被评价行数放大
    AVG(r.avg_score)           AS avg_score    -- 每单评分等权
FROM orders o
JOIN customers c       ON c.customer_id = o.customer_id   -- 明确 INNER 语义
JOIN item_agg i        ON i.order_id = o.order_id
LEFT JOIN review_agg r ON r.order_id = o.order_id         -- 无评价订单保留
WHERE o.order_status = 'delivered'
  AND o.order_purchase_timestamp >= '2017-01-01'
  AND LOWER(c.customer_city) LIKE '%sao%'
GROUP BY c.customer_state
ORDER BY revenue DESC;
```

### 建议添加的索引

分两层。**第一层（治本）：修正列类型与主键**（演示 DDL，本卷在临时表上验证后已清理）：

```sql
-- 以 orders 为例（customers/order_items/order_reviews 同理）
CREATE TABLE orders_typed AS
SELECT CAST(order_id AS CHAR(32))  AS order_id,
       CAST(customer_id AS CHAR(32)) AS customer_id,
       CAST(order_status AS CHAR(20)) AS order_status,
       STR_TO_DATE(order_purchase_timestamp, '%Y-%m-%d %H:%i:%s') AS purchase_ts,
       STR_TO_DATE(order_delivered_customer_date, '%Y-%m-%d %H:%i:%s') AS delivered_customer_dt,
       STR_TO_DATE(order_estimated_delivery_date, '%Y-%m-%d %H:%i:%s') AS estimated_dt
FROM orders;
ALTER TABLE orders_typed ADD PRIMARY KEY (order_id);
ALTER TABLE orders_typed ADD INDEX idx_cust (customer_id);
ALTER TABLE orders_typed ADD INDEX idx_status_ts (order_status, purchase_ts);
```

**第二层（治标，在现有 TEXT schema 上的最小改动）**——但请注意实测教训：

```sql
-- 理论上应建：
CREATE INDEX idx_items_order   ON order_items(order_id(32));
CREATE INDEX idx_reviews_order ON order_reviews(order_id(32));
-- ⚠️ 实测警告：在 TEXT 裸表上这些前缀索引把原查询从 0.88s 拖慢到 30.2s，
-- 因为优化器放弃 hash join 改走逐行 ref。在类型化修复之前，
-- "不加索引"反而是本数据量下的最优解。
```

### 优化前 vs 优化后实测对比

| 指标 | 优化前（原查询） | 优化后 |
|------|--------|--------|
| 裸表（TEXT、无索引）执行时间 | 0.88s（hash join 已是最优） | 1.95s（先聚合多付的代价，见下） |
| 裸表 + TEXT 前缀索引执行时间 | **30.20s（恶化 34x！）** | >120s 超时 |
| 类型化 schema + 主键/二级索引 | —（不成立，类型化是前提） | 6.53s（含冷缓存；该量级下瓶颈是字符串比较） |
| EXPLAIN 扫描行数（orders 表） | 98,282 全表 | 98,282（该谓词本就需全表，见下注） |
| **结果正确性** | **revenue 高估 0.49%** | 正确（2,535,313.89） |

**对这组数字的诚实解读（比数字本身更重要）**：

1. **本数据量（10 万级）不是"慢查询"问题的真实战场**。原查询在本机裸表上 <1s——
   因为 MySQL 8.0 的 hash join 对"join 后要聚合全部行"的负载非常高效。
   我先后两次观察到"41.9s / 61 分钟"的惊人耗时，排查后（`SHOW FULL PROCESSLIST`）
   发现是**实验脚本被取消后进程残留、查询间互相等表元数据锁**——根本不是查询慢。
   清理进程后一切正常。这个排查过程本身就是一个生产事故复盘的缩影：
   慢的常常不是 SQL，而是"谁挡住了谁"。
2. **线上数据放大 100 倍后（千万级 order_items）结论反转**：hash join 需要
   内存构建哈希表并落盘，嵌套循环 + 二级索引回表才能扩展；届时"先聚合后 join"
   的重写（把 join 行数从 N_items×N_reviews 压到 N_orders）才是数量级的优化。
   本卷的实测证明了"为什么"，量级推演给出"什么时候值得"。
3. **唯一在任何规模下都成立的优化是正确性优化**：revenue 高估 0.49% 与
   评分加权问题，0.88s 还是 30s 都改变不了"数字是错的"。

---

## 第 3 问：潜在的统计错误（关键题）

> `SUM(oi.price)` 的结果可能不是你以为的「订单总收入」。为什么？

**根因：order_reviews 与 order_items 的一对多 join 产生行数膨胀（fan-out）。**

- 一个订单有 M 行明细、K 条评价（本库 547 个订单有多条评价，最多一单多评）；
- 三表 join 后每个"明细行 × 评价行"组合各出现一次，订单 1003 的 14 行明细 × 3 条评价 = 42 行；
- `SUM(oi.price)` 把每行明细重复计了 K 次——**收入被高估**；
- 同时 `AVG(r.review_score)` 被商品行数加权（问题 5）。

**实测验证**（`_bench` 脚本，方法：原查询 vs 重写版逐州对账）：

| 指标 | 原查询（高估） | 重写后（真实） |
|---|---|---|
| 总收入（LIKE '%sao%' 范围） | 2,547,698.71 | 2,535,313.89 |
| 高估幅度 | **+0.49%** | — |
| 最大单州偏差 | SP +11,783.87（+0.53%） | — |

0.49% 看似小，但它的可怕之处是**系统性偏向多评价订单**（而多评价订单往往
体验两极分化），做城市/州对比或按 revenue 排序时排名可能被悄悄改变。

**修正方案**：

```sql
-- 把评价先收敛到订单粒度，明细先聚合到订单粒度，再 join（完整 SQL 见第 2 问）
WITH item_agg AS (
    SELECT oi.order_id, SUM(oi.price) AS revenue
    FROM order_items oi
    GROUP BY oi.order_id
),
review_agg AS (     -- 修正点 1：一单一评分，SUM 不再被重复计数
    SELECT order_id, AVG(review_score) AS avg_score
    FROM order_reviews
    GROUP BY order_id
)
SELECT
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS orders,
    SUM(i.revenue)  AS revenue,     -- 修正点 2：在"订单粒度"上求和
    AVG(r.avg_score) AS avg_score   -- 修正点 3：每单评分等权
FROM orders o
JOIN customers c       ON c.customer_id = o.customer_id
JOIN item_agg i        ON i.order_id = o.order_id
LEFT JOIN review_agg r ON r.order_id = o.order_id
WHERE o.order_status = 'delivered'
  AND o.order_purchase_timestamp >= '2017-01-01'
  AND LOWER(c.customer_city) LIKE '%sao%'
GROUP BY c.customer_state
ORDER BY revenue DESC;
```

顺带说明：`SUM(oi.price)` 即便没有评价 fan-out，也只是"商品金额合计"，
不含 `freight_value`——与 Q2.2 的 GMV 口径（price + freight_value）不一致。
"订单总收入"到底含不含运费，是必须和需求方确认的口径问题。

---

## 附录：EXPLAIN 证据

无索引（hash join 计划，0.88s）：

```
id  table  type  key   rows   Extra
1   o      ALL   None  98282  Using where; Using temporary; Using filesort
1   c      ALL   None  98753  Using where; Using join buffer (hash join)
1   oi     ALL   None 111517  Using where; Using join buffer (hash join)
1   r      ALL   None  98439  Using where; Using join buffer (hash join)
```

加 TEXT 前缀索引后（嵌套循环计划，30.2s）：

```
id  table  type  key                rows  Extra
1   o      ALL   None               98282 Using where; Using temporary; Using filesort
1   oi     ref   idx_items_order        1 Using where
1   r      ref   idx_reviews_order      1 Using where
1   c      ALL   None               98753 Using where; Using join buffer (hash join)
```

> 两份计划逐行看"后一份更好"（ref 比 ALL 便宜），但整体性能差 34 倍——
> 这是"只看 EXPLAIN 单表行数评估 SQL 优劣"的经典反例。
