-- ============================================================
-- Q2.3 卖家维度分析：2017 年 GMV TOP 20 卖家
-- 输出：seller_id / seller_state / gmv_2017 / order_count / unique_customers /
--       avg_review_score / late_delivery_rate / cancel_rate / state_rank
-- 口径决策（重要）：
--   1. 评价去重：一单可有多条评价（本库 547 个订单有多条），
--      用 ROW_NUMBER 按 review_answer_timestamp 取最新一条，每单只贡献一次评分；
--   2. 反 fan-out：order_items(多行/单) 不能直接 LEFT JOIN 评价表再 SUM(price)，
--      否则 GMV 被评价行数放大——所以把"商品级聚合"和"订单级聚合"分开算再拼；
--   3. late_delivery_rate = 实际送达晚于承诺送达的订单数 / 已送达订单数
--      （只统计有实际送达时间的 delivered 订单）；
--   4. cancel_rate = canceled 订单数 / 该卖家 2017 全部订单数；
--   5. unique_customers 用 customers.customer_unique_id（同一自然人多次下单算一人），
--      而不是 customer_id（那是一次会话级标识，会低估真实客户数）；
--   6. state_rank：同州内按 gmv_2017 降序 RANK。
-- ============================================================
WITH review_latest AS (
    -- 每个订单只保留最新一条评价
    SELECT order_id, review_score
    FROM (
        SELECT
            r.order_id,
            r.review_score,
            ROW_NUMBER() OVER (
                PARTITION BY r.order_id
                ORDER BY STR_TO_DATE(r.review_answer_timestamp, '%Y-%m-%d %H:%i:%s') DESC
            ) AS rn
        FROM order_reviews r
    ) ranked
    WHERE rn = 1
),

orders_2017 AS (
    -- 2017 年订单级视图：一行一订单，预计算订单级标记
    SELECT
        o.order_id,
        o.order_status,
        c.customer_unique_id,
        STR_TO_DATE(o.order_purchase_timestamp,      '%Y-%m-%d %H:%i:%s') AS purchased_at,
        CASE
            WHEN o.order_status = 'delivered'
                 AND o.order_delivered_customer_date IS NOT NULL
                 AND o.order_estimated_delivery_date IS NOT NULL
                 AND STR_TO_DATE(o.order_delivered_customer_date, '%Y-%m-%d %H:%i:%s')
                   > STR_TO_DATE(o.order_estimated_delivery_date,   '%Y-%m-%d %H:%i:%s')
            THEN 1 ELSE 0
        END AS is_late,
        CASE
            WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
            THEN 1 ELSE 0
        END AS is_delivered_with_date
    FROM orders o
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE YEAR(STR_TO_DATE(o.order_purchase_timestamp, '%Y-%m-%d %H:%i:%s')) = 2017
),

seller_gmv AS (
    -- 商品级聚合：GMV 与商品行数（不受评价表 fan-out 影响）
    SELECT
        oi.seller_id,
        SUM(oi.price)        AS gmv_2017,
        COUNT(DISTINCT oi.order_id) AS has_item_orders
    FROM order_items oi
    JOIN orders_2017 od ON od.order_id = oi.order_id
    GROUP BY oi.seller_id
),

seller_orders AS (
    -- 订单级聚合：订单数、客户数、评分、延迟率、取消率
    -- 先按 (seller, order) 去重：一个订单可能含同一卖家的多行明细
    SELECT
        oi.seller_id,
        od.order_id,
        MAX(od.customer_unique_id) AS customer_unique_id,
        MAX(od.order_status)       AS order_status,
        MAX(od.is_late)            AS is_late,
        MAX(od.is_delivered_with_date) AS is_delivered_with_date,
        MAX(rl.review_score)       AS review_score
    FROM order_items oi
    JOIN orders_2017 od       ON od.order_id = oi.order_id
    LEFT JOIN review_latest rl ON rl.order_id = oi.order_id
    GROUP BY oi.seller_id, od.order_id
),

seller_agg AS (
    SELECT
        seller_id,
        COUNT(*)                                        AS order_count,
        COUNT(DISTINCT customer_unique_id)              AS unique_customers,
        AVG(review_score)                               AS avg_review_score,
        ROUND(100 * SUM(is_late) / NULLIF(SUM(is_delivered_with_date), 0), 2) AS late_delivery_rate_pct,
        ROUND(100 * SUM(order_status = 'canceled') / COUNT(*), 2)             AS cancel_rate_pct
    FROM seller_orders
    GROUP BY seller_id
)

SELECT
    g.seller_id,
    s.seller_state,
    ROUND(g.gmv_2017, 2)          AS gmv_2017,
    a.order_count,
    a.unique_customers,
    ROUND(a.avg_review_score, 2)  AS avg_review_score,
    a.late_delivery_rate_pct      AS late_delivery_rate,
    a.cancel_rate_pct             AS cancel_rate,
    RANK() OVER (PARTITION BY s.seller_state ORDER BY g.gmv_2017 DESC) AS state_rank
FROM seller_gmv g
JOIN seller_agg a USING (seller_id)
JOIN sellers s    USING (seller_id)
ORDER BY g.gmv_2017 DESC
LIMIT 20;
