-- ============================================================
-- Q2.1 商品类别翻译完整性
-- 需求：找出 products 表中存在、但翻译表（product_category_name_translation）
--       无英文对照的类别，并统计涉及的商品数；进阶：给出 3 种处理方案及利弊。
-- 环境：MySQL 8.0（olist 库），9 张 CSV 灌入后的原始 schema（无主键/索引）
-- ============================================================

-- 主查询：按"缺失翻译的类别"聚合商品数
-- 口径说明：products.product_category_name 本身为 NULL 的商品（连类别都没有）不属于
-- "翻译缺失"，单列统计，与"有类别但翻译表缺行"区分开。
SELECT
    p.product_category_name          AS missing_translation_category,
    COUNT(*)                         AS product_cnt,
    ROUND(100 * COUNT(*) / (SELECT COUNT(*) FROM products), 2) AS pct_of_all_products
FROM products p
LEFT JOIN product_category_name_translation t
       ON p.product_category_name = t.product_category_name
WHERE p.product_category_name IS NOT NULL      -- 排除"连类别都没有"的商品
  AND t.product_category_name IS NULL          -- 类别存在但翻译表无对照
GROUP BY p.product_category_name
ORDER BY product_cnt DESC;

-- 附加核对：类别字段本身为 NULL 的商品数（数据质量问题，与翻译缺失是两回事）
SELECT COUNT(*) AS products_with_null_category
FROM products
WHERE product_category_name IS NULL;

-- ============================================================
-- 进阶：3 种处理方案及利弊（结论性说明，配套数据见上方查询）
-- ============================================================
-- 方案一：不处理，照常返回葡萄牙语原名，在展示层兜底
--   优点：零数据改动，丢失信息最少；缺点：下游报表/筛选仍会出现"看不懂"的类别，
--   每个消费方都要各自兜底，口径容易不一致。
--
-- 方案二：人工补翻 2 个类别，写回翻译表（数据量小，本查询结果仅 2 类）
--   优点：一次性根治，改动可控可审计（71 -> 73 行），推荐；
--   缺点：需要人工确认语义；若未来 Olist 上新类别，需要建立"新增类别自动报警"机制，
--   否则缺口会悄悄变大。
--
-- 方案三：ETL 层对缺失类别做标准化映射/归并（如归入 other 或最接近的现有类别）
--   优点：下游永远拿到干净的英文类别，分析口径稳定；
--   缺点：有损——若两个新品类被强行归并，商品级分析会失真；
--   且映射规则需要随业务维护，属于典型的"清洗债"。
--
-- 推荐：方案二为主（补齐 2 行），同时加方案一的展示层兜底作为防线；
-- 方案三仅当出现"长尾无法翻译"的类别时才启用。
