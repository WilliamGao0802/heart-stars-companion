-- ============================================================
-- v0120_share_distribution.sql  · 数量占比页新增两个聚合 RPC
--
-- 背景：v0.12.0 管理后台新增「数量占比」页（回答两问：四端是不是同一个系统、
-- 用户用到了什么深度）。其中两块需要比 admin_metrics 更细的聚合：
--   1. admin_turn_distribution  对话轮次档位分布（1–5 / 6–20 / 21–50 / 50+）
--   2. admin_hour_distribution  近 7 天活跃的时段分布（0–23 时）
--
-- 安全约定与 admin_metrics 完全一致（这批函数照抄它的骨架，勿改）：
--   · SECURITY DEFINER + search_path = public, pg_temp
--   · 密钥只从 app_config.admin_key_hash 校验（SHA-256），错误码统一 'forbidden'
--   · 只读，无任何写路径；REVOKE 后按最小授权 GRANT
--   · 函数体内零反斜杠、零 `<>`（MCP 网关会转义），一律标准单引号
--
-- 部署：与 admin_functions.sql 保持逐字一致后，在云控制台 SQL 窗口单条执行
-- （MCP exec_sql 只收单条语句）。
-- ============================================================

-- ============ 1. 对话轮次档位分布 ============
-- 口径：按 owner_id 聚合 chat_messages 里 role='user' 的条数（一个用户轮数 = 他发过多少条），
--      档位 [1-5, 6-20, 21-50, 50+]。不含 administrator。
CREATE OR REPLACE FUNCTION public.admin_turn_distribution(p_key text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_hash text;
BEGIN
  SELECT v INTO v_hash FROM public.app_config WHERE k = 'admin_key_hash';
  IF v_hash IS NULL
     OR encode(digest(coalesce(p_key, ''), 'sha256'), 'hex') != v_hash THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE = 'P0001';
  END IF;

  RETURN jsonb_build_object(
    'buckets', (
      SELECT coalesce(jsonb_agg(x ORDER BY x.idx), '[]'::jsonb) FROM (
        SELECT
          CASE
            WHEN n BETWEEN 1  AND 5  THEN 0
            WHEN n BETWEEN 6  AND 20 THEN 1
            WHEN n BETWEEN 21 AND 50 THEN 2
            ELSE 3
          END AS idx,
          CASE
            WHEN n BETWEEN 1  AND 5  THEN '1-5 轮'
            WHEN n BETWEEN 6  AND 20 THEN '6-20 轮'
            WHEN n BETWEEN 21 AND 50 THEN '21-50 轮'
            ELSE '50 轮以上'
          END AS label,
          count(*) AS users
        FROM (
          SELECT owner_id, count(*) AS n
          FROM public.chat_messages
          WHERE role = 'user' AND owner_id IS NOT NULL
          GROUP BY owner_id
        ) t
        GROUP BY 1, 2
      ) x
    ),
    'generated_at', now()
  );
END;
$$;

REVOKE ALL ON FUNCTION public.admin_turn_distribution(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_turn_distribution(text) TO anon, authenticated;


-- ============ 2. 时段分布（近 7 天，按北京时间） ============
-- 口径：analytics_events 里 event='app_open'（打开即活跃），
--      按 created_at 转 Asia/Shanghai 后的小时聚合。0–6 时合计即「凌晨占比」分子。
CREATE OR REPLACE FUNCTION public.admin_hour_distribution(p_key text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_hash text;
BEGIN
  SELECT v INTO v_hash FROM public.app_config WHERE k = 'admin_key_hash';
  IF v_hash IS NULL
     OR encode(digest(coalesce(p_key, ''), 'sha256'), 'hex') != v_hash THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE = 'P0001';
  END IF;

  RETURN jsonb_build_object(
    'hours', (
      SELECT coalesce(jsonb_agg(x ORDER BY x.h), '[]'::jsonb) FROM (
        SELECT
          extract(hour FROM (created_at AT TIME ZONE 'Asia/Shanghai'))::int AS h,
          count(*) AS n
        FROM public.analytics_events
        WHERE event = 'app_open'
          AND created_at >= now() - interval '7 days'
        GROUP BY 1
      ) x
    ),
    'generated_at', now()
  );
END;
$$;

REVOKE ALL ON FUNCTION public.admin_hour_distribution(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_hour_distribution(text) TO anon, authenticated;
