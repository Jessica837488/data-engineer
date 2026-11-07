-- ============================================================
-- Q2.2 平台真实成交分析（按月指标）
-- 输出：month / total_orders / delivered_orders / canceled_orders /
--       cancel_rate(2位小数) / delivered_gmv(price+freight_value) / avg_delivery_days
-- 口径决策（重要）：
--   1. 日期字段在灌库后是 TEXT 类型，一律 STR_TO_DATE 转换后再计算（README 提示的方言点）；
--   2. total/delivered/canceled 订单数用 COUNT(DISTINCT order_id)——
--      因为 orders LEFT JOIN order_items 是一对多，直接 COUNT(*) 会把多商品订单算多次；
--   3. delivered_gmv 只统计 delivered 订单的明细行（price + freight_value），
--      漏掉 freight_value 会低估约 15-20% 的真实收入；
--   4. avg_delivery_days 只对 delivered 且送达时间非空的订单取平均（NULL 自动被 AVG 忽略，
--      但仍显式过滤以便阅读），按"自然日" DATEDIFF 计算；
--   5. cancel_rate 输出为百分比值并保留 2 位小数（如 0.65 表示 0.65%）。
-- ============================================================
WITH order_gmv AS (
    -- 每个订单的 GMV（仅 delivered 订单参与），避免在月度聚合时重复 join 明细
    SELECT
        oi.order_id,
        SUM(oi.price + oi.freight_value) AS gmv
    FROM order_items oi
    JOIN orders o ON o.order_id = oi.order_id
    WHERE o.order_status = 'delivered'
    GROUP BY oi.order_id
)
SELECT
    DATE_FORMAT(STR_TO_DATE(o.order_purchase_timestamp, '%Y-%m-%d %H:%i:%s'), '%Y-%m') AS `month`,
    COUNT(DISTINCT o.order_id)                                                        AS total_orders,
    COUNT(DISTINCT CASE WHEN o.order_status = 'delivered' THEN o.order_id END)         AS delivered_orders,
    COUNT(DISTINCT CASE WHEN o.order_status = 'canceled'  THEN o.order_id END)         AS canceled_orders,
    ROUND(100 * COUNT(DISTINCT CASE WHEN o.order_status = 'canceled' THEN o.order_id END)
              / COUNT(DISTINCT o.order_id), 2)                                        AS cancel_rate_pct,
    ROUND(SUM(CASE WHEN o.order_status = 'delivered' THEN g.gmv ELSE 0 END), 2)        AS delivered_gmv,
    ROUND(AVG(CASE
                  WHEN o.order_status = 'delivered'
                   AND o.order_delivered_customer_date IS NOT NULL
                  THEN DATEDIFF(
                           STR_TO_DATE(o.order_delivered_customer_date, '%Y-%m-%d %H:%i:%s'),
                           STR_TO_DATE(o.order_purchase_timestamp,     '%Y-%m-%d %H:%i:%s'))
             END), 2)                                                                 AS avg_delivery_days
FROM orders o
LEFT JOIN order_gmv g ON g.order_id = o.order_id
GROUP BY `month`
ORDER BY `month`;
