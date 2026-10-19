-- =============================================================================
-- v0.11.4 迁移：汇报要求对齐（《心与星寻_马老师汇报_初见面版》P7 / P9 / P10）
-- =============================================================================
-- 本文件是**线上变更的留痕**。执行顺序即文件顺序，可整份重跑（全部 IF NOT EXISTS /
-- CREATE OR REPLACE）。跑完后必须与 sql/admin_functions.sql 第 7、8 节逐字一致。
--
-- 变更清单：
--   ① admin_alerts 加四状态流程字段（P7「每次 L2/L3 触发都留档」+ P10「触发→响应→
--      转介→关闭 的时间线审计，老师可随时抽查」）
--   ② 新增 admin_alert_update RPC（唯一允许后台写库的入口，白名单式更新，带审计）
--   ③ admin_metrics 扩充 5 个指标口径（P9 五个指标 + P14 止损线）
--
-- 注意（MCP exec_sql 踩坑，见项目记忆）：
--   · 只能单条语句执行 → 本文件需逐条送；
--   · 字符串里不能有反斜杠转义 → 下面 LIKE 'referral_%' 不用 \_ 转义。
-- =============================================================================


-- ============ ① 警报台账：四状态流程字段 ============
-- 状态机：open（触发，系统写入即 open）→ acked（已响应）→ referred（已转介）→ closed（已关闭）
--   允许跳级（例如 L3 直接转介后关闭），但 timestamp 必须顺序不减。
-- 所有新列都可空/有默认值，老数据不用回填即可继续跑。
ALTER TABLE public.admin_alerts ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'open';
ALTER TABLE public.admin_alerts ADD COLUMN IF NOT EXISTS acked_at timestamp with time zone;
ALTER TABLE public.admin_alerts ADD COLUMN IF NOT EXISTS referred_at timestamp with time zone;
ALTER TABLE public.admin_alerts ADD COLUMN IF NOT EXISTS closed_at timestamp with time zone;
ALTER TABLE public.admin_alerts ADD COLUMN IF NOT EXISTS handler text NOT NULL DEFAULT '';
ALTER TABLE public.admin_alerts ADD COLUMN IF NOT EXISTS note text NOT NULL DEFAULT '';
ALTER TABLE public.admin_alerts ADD COLUMN IF NOT EXISTS referral_path text NOT NULL DEFAULT '';

-- 状态取值收口（用 CHECK 而不是 enum：加值不用改类型，迁移成本低）
DO $do$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'admin_alerts_status_chk'
  ) THEN
    ALTER TABLE public.admin_alerts
      ADD CONSTRAINT admin_alerts_status_chk
      CHECK (status IN ('open', 'acked', 'referred', 'closed'));
  END IF;
END $do$;

-- 看板按状态筛，建一个部分索引（open 的行永远最少、查得最多）
CREATE INDEX IF NOT EXISTS admin_alerts_status_idx
  ON public.admin_alerts (status, created_at DESC);

-- 安全边界（v0.11.2 二轮审查定下的口径，**不要再动**）：
--   · RLS 开启 + 唯一的 INSERT 策略 al_insert_any（字段长度 CHECK 收口）；
--   · anon/authenticated 只 GRANT INSERT，**不给 SELECT** —— 读只走 admin_alerts_list；
--   · 状态流转不走表直写，走下面的 admin_alert_update RPC（SECURITY DEFINER）。
-- 这里只补一句注释，不重复授权：线上已有的 al_insert_any 策略就是上报通道，
--   任何 REVOKE ALL 都必须紧跟 GRANT INSERT，否则用户侧上报会静默 401。


-- ============ ② 警报状态流转 RPC ============
-- 为什么必须走 RPC：网关不透传自定义请求头，无法用 RLS 区分"后台"与"普通用户"。
--   所以后台写库 = 带管理密钥调 SECURITY DEFINER 函数，函数内部校验 + 白名单列更新。
-- 白名单：p_status / p_handler / p_note / p_referral_path 四个字段，其它一律不接受。
-- 时间戳由服务端按状态自动落（客户端传时间会被伪造）：

CREATE OR REPLACE FUNCTION public.admin_alert_update(
  p_key    text,
  p_id     bigint,
  p_status text,
  p_handler text DEFAULT '',
  p_note    text DEFAULT '',
  p_referral_path text DEFAULT ''
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_hash  text;
  v_row   public.admin_alerts%ROWTYPE;
  v_now   timestamptz := now();
BEGIN
  SELECT v INTO v_hash FROM public.app_config WHERE k = 'admin_key_hash';
  IF v_hash IS NULL
     OR encode(digest(coalesce(p_key, ''), 'sha256'), 'hex') != v_hash THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE = 'P0001';
  END IF;

  IF p_status IS NULL OR p_status NOT IN ('open', 'acked', 'referred', 'closed') THEN
    RAISE EXCEPTION 'bad_status' USING ERRCODE = 'P0001';
  END IF;

  SELECT * INTO v_row FROM public.admin_alerts WHERE id = p_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'not_found' USING ERRCODE = 'P0001';
  END IF;

  -- 已关闭的不许再改（台账一旦关闭就是归档，避免事后抹改）
  IF v_row.status = 'closed' AND p_status != 'closed' THEN
    RAISE EXCEPTION 'already_closed' USING ERRCODE = 'P0001';
  END IF;

  UPDATE public.admin_alerts SET
    status        = p_status,
    handler       = left(coalesce(nullif(p_handler, ''), handler, ''), 64),
    note          = left(coalesce(nullif(p_note, ''), note, ''), 500),
    referral_path = left(coalesce(nullif(p_referral_path, ''), referral_path, ''), 32),
    acked_at      = CASE WHEN p_status IN ('acked', 'referred', 'closed')
                         THEN coalesce(acked_at, v_now) ELSE acked_at END,
    referred_at   = CASE WHEN p_status IN ('referred', 'closed')
                         THEN coalesce(referred_at, v_now) ELSE referred_at END,
    closed_at     = CASE WHEN p_status = 'closed'
                         THEN coalesce(closed_at, v_now) ELSE closed_at END
  WHERE id = p_id;

  RETURN jsonb_build_object('ok', true, 'id', p_id, 'status', p_status);
END;
$$;

REVOKE ALL ON FUNCTION public.admin_alert_update(text, bigint, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_alert_update(text, bigint, text, text, text, text) TO anon, authenticated;


-- ============ ③ 警报列表：带状态与时间线 ============
DROP FUNCTION IF EXISTS public.admin_alerts_list(text, int);

CREATE OR REPLACE FUNCTION public.admin_alerts_list(p_key text, p_limit int DEFAULT 200)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO public, pg_temp AS $fn$
BEGIN
  IF encode(digest(coalesce(p_key,''),'sha256'),'hex')
     != (SELECT v FROM public.app_config WHERE k='admin_key_hash') THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE = 'P0001';
  END IF;
  RETURN coalesce((
    SELECT jsonb_agg(jsonb_build_object(
      'id', a.id, 'user_id', a.user_id, 'source', a.source,
      'content', a.content, 'words', a.words, 'created_at', a.created_at::text,
      'status', a.status,
      'acked_at', a.acked_at::text,
      'referred_at', a.referred_at::text,
      'closed_at', a.closed_at::text,
      'handler', a.handler,
      'note', a.note,
      'referral_path', a.referral_path))
    FROM (SELECT * FROM admin_alerts ORDER BY created_at DESC LIMIT p_limit) a
  ), '[]'::jsonb);
END $fn$;
GRANT EXECUTE ON FUNCTION public.admin_alerts_list(text, int) TO anon, authenticated;


-- ============ ④ 周度看板：补 P9 五个指标 + P14 止损线 ============
-- 口径（改字段名必须同步改 views/admin.py 的 _page_metrics，两边一一对应）：
--
--   指标① 主动周留存 week_retention（主指标，阈值 ≥25%）
--     分子：本周（最近 7 天）内 app_open 落在 ≥2 个不同日期的用户数
--     分母：上周（7~14 天前）有过 app_open 的用户数
--     这是「主动回来」的口径 —— 一天里反复打开不算，必须跨天。
--
--   指标② 首周激活率 activation_7d（止损线 <30%）
--     分母：首次 app_open 发生在 7~14 天前的人（cohort 已满 7 天，可判成败）
--     分子：其中在注册后 7 天内有过至少一次 diary_create 的人
--     注：写日记不要求连接账号，匿名期就写的人不会进分子 —— 这是**保守**口径。
--
--   指标③ 日均对话轮次 turns_per_day（= 周内 chat_turn / 7，另给人均口径 avg_turns）
--
--   指标④ 情绪记录连续性 diary_days（人均每周记录天数，0~7）
--     分母：本周写过日记的人；分子：日记去重天数之和 / 人数
--
--   指标⑤ 安全事件台账 crisis_l2/l3_7d + referral_7d + referral_rate
--     台账四状态的数量另给 alert_open / alert_closed / alert_median_minutes
--     （响应时长 = acked_at - created_at 的中位数分钟数，P9「响应时长」）
CREATE OR REPLACE FUNCTION public.admin_metrics(p_key text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_hash   text;
  v_now    timestamptz := now();
  v_today  date        := (now() AT TIME ZONE 'Asia/Shanghai')::date;
  v_dau    bigint;
  v_wau    bigint;
  v_mau    bigint;
  v_turns  bigint;
  v_avg    numeric;
  v_tpd    numeric;
  v_ret1   numeric;
  v_ret7   numeric;
  v_wret   numeric;
  v_wret_c bigint;
  v_wret_k bigint;
  v_act    numeric;
  v_act_c  bigint;
  v_act_k  bigint;
  v_ddays  numeric;
  v_l2     bigint;
  v_l3     bigint;
  v_ref    bigint;
  v_open   bigint;
  v_closed bigint;
  v_resp   numeric;
  v_trend  jsonb;
  v_plat   jsonb;
BEGIN
  SELECT v INTO v_hash FROM public.app_config WHERE k = 'admin_key_hash';
  IF v_hash IS NULL
     OR encode(digest(coalesce(p_key, ''), 'sha256'), 'hex') != v_hash THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE = 'P0001';
  END IF;

  -- 日活 / 周活 / 月活（app_open 已按天去重，distinct owner_id 即人数）
  SELECT count(DISTINCT owner_id) INTO v_dau FROM public.analytics_events
   WHERE event = 'app_open' AND owner_id IS NOT NULL
     AND created_at >= v_now - interval '1 day';
  SELECT count(DISTINCT owner_id) INTO v_wau FROM public.analytics_events
   WHERE event = 'app_open' AND owner_id IS NOT NULL
     AND created_at >= v_now - interval '7 days';
  SELECT count(DISTINCT owner_id) INTO v_mau FROM public.analytics_events
   WHERE event = 'app_open' AND owner_id IS NOT NULL
     AND created_at >= v_now - interval '30 days';

  -- 人均对话轮次 = 本周 chat_turn / 本周活跃人数（除零必须 NULLIF）
  SELECT count(*) INTO v_turns FROM public.analytics_events
   WHERE event = 'chat_turn' AND created_at >= v_now - interval '7 days';
  v_avg := round(v_turns::numeric / NULLIF(v_wau, 0), 1);
  v_tpd := round(v_turns::numeric / 7.0, 1);

  -- 次日 / 7 日留存：以「首次 app_open 的日期」为 cohort
  WITH first_seen AS (
    SELECT owner_id, min((created_at AT TIME ZONE 'Asia/Shanghai')::date) AS d0
      FROM public.analytics_events
     WHERE event = 'app_open' AND owner_id IS NOT NULL
     GROUP BY owner_id
  ),
  days AS (
    SELECT DISTINCT owner_id, (created_at AT TIME ZONE 'Asia/Shanghai')::date AS d
      FROM public.analytics_events
     WHERE event = 'app_open' AND owner_id IS NOT NULL
  ),
  c1 AS (
    SELECT count(*) FILTER (WHERE fs.d0 = v_today - 1) AS cohort,
           count(*) FILTER (WHERE fs.d0 = v_today - 1 AND days.d = fs.d0 + 1) AS kept
      FROM first_seen fs LEFT JOIN days ON days.owner_id = fs.owner_id AND days.d = fs.d0 + 1
  ),
  c7 AS (
    SELECT count(*) FILTER (WHERE fs.d0 = v_today - 7) AS cohort,
           count(*) FILTER (WHERE fs.d0 = v_today - 7 AND days.d = fs.d0 + 7) AS kept
      FROM first_seen fs LEFT JOIN days ON days.owner_id = fs.owner_id AND days.d = fs.d0 + 7
  )
  SELECT round(100.0 * c1.kept / NULLIF(c1.cohort, 0), 1),
         round(100.0 * c7.kept / NULLIF(c7.cohort, 0), 1)
    INTO v_ret1, v_ret7
    FROM c1, c7;

  -- 指标①：主动周留存（本周跨 ≥2 天打开 / 上周活跃）
  WITH wk AS (
    SELECT owner_id,
           count(DISTINCT (created_at AT TIME ZONE 'Asia/Shanghai')::date) AS days_cnt,
           count(*) FILTER (WHERE created_at >= v_now - interval '7 days') AS recent_cnt
      FROM public.analytics_events
     WHERE event = 'app_open' AND owner_id IS NOT NULL
       AND created_at >= v_now - interval '14 days'
     GROUP BY owner_id
  )
  SELECT count(*) FILTER (WHERE recent_cnt > 0),
         count(*) FILTER (WHERE recent_cnt > 0 AND days_cnt >= 2)
    INTO v_wret_c, v_wret_k
    FROM wk;
  v_wret := round(100.0 * v_wret_k / NULLIF(v_wret_c, 0), 1);

  -- 指标②：首周激活率（首次出现在 7~14 天前的人，7 天内是否写过日记）
  WITH first_seen AS (
    SELECT owner_id, min((created_at AT TIME ZONE 'Asia/Shanghai')::date) AS d0
      FROM public.analytics_events
     WHERE event = 'app_open' AND owner_id IS NOT NULL
     GROUP BY owner_id
  ),
  act AS (
    SELECT DISTINCT owner_id, (created_at AT TIME ZONE 'Asia/Shanghai')::date AS d
      FROM public.analytics_events
     WHERE event = 'diary_create' AND owner_id IS NOT NULL
  )
  SELECT count(*),
         count(*) FILTER (WHERE act.owner_id IS NOT NULL)
    INTO v_act_c, v_act_k
    FROM first_seen fs
    LEFT JOIN act ON act.owner_id = fs.owner_id
                 AND act.d >= fs.d0 AND act.d <= fs.d0 + 7
   WHERE fs.d0 BETWEEN v_today - 14 AND v_today - 7;
  v_act := round(100.0 * v_act_k / NULLIF(v_act_c, 0), 1);

  -- 指标④：情绪记录连续性（人均每周记录天数）
  WITH dd AS (
    SELECT owner_id, count(DISTINCT (created_at AT TIME ZONE 'Asia/Shanghai')::date) AS n
      FROM public.analytics_events
     WHERE event = 'diary_create' AND owner_id IS NOT NULL
       AND created_at >= v_now - interval '7 days'
     GROUP BY owner_id
  )
  SELECT round(avg(n)::numeric, 1) INTO v_ddays FROM dd;

  -- 指标⑤：危机触发与转介闭环
  SELECT count(*) INTO v_l2 FROM public.analytics_events
   WHERE event = 'crisis_l2' AND created_at >= v_now - interval '7 days';
  SELECT count(*) INTO v_l3 FROM public.analytics_events
   WHERE event = 'crisis_l3' AND created_at >= v_now - interval '7 days';
  SELECT count(*) INTO v_ref FROM public.analytics_events
   WHERE event LIKE 'referral_%' AND created_at >= v_now - interval '7 days';

  -- 台账四状态（全量，不限 7 天：这是要抽查的档案，不是指标）
  SELECT count(*) FILTER (WHERE status != 'closed') INTO v_open FROM public.admin_alerts;
  SELECT count(*) FILTER (WHERE status = 'closed')  INTO v_closed FROM public.admin_alerts;
  SELECT round((extract(epoch FROM (
             percentile_cont(0.5) WITHIN GROUP (ORDER BY (acked_at - created_at))
           )))::numeric / 60.0, 1)
    INTO v_resp
    FROM public.admin_alerts
   WHERE acked_at IS NOT NULL AND created_at >= v_now - interval '30 days';

  -- 近 14 天日活趋势
  SELECT coalesce(jsonb_agg(jsonb_build_object('d', d, 'n', n) ORDER BY d), '[]'::jsonb)
    INTO v_trend
    FROM (
      SELECT (created_at AT TIME ZONE 'Asia/Shanghai')::date AS d,
             count(DISTINCT owner_id) AS n
        FROM public.analytics_events
       WHERE event = 'app_open' AND owner_id IS NOT NULL
         AND created_at >= v_now - interval '14 days'
       GROUP BY 1
    ) t;

  -- 近 7 天分端活跃（H5 / 小程序 / App 各自多少人，汇报要能报"三端都有量"）
  SELECT coalesce(jsonb_object_agg(platform, n), '{}'::jsonb) INTO v_plat FROM (
    SELECT coalesce(nullif(platform, ''), 'unknown') AS platform,
           count(DISTINCT owner_id) AS n
      FROM public.analytics_events
     WHERE event = 'app_open' AND owner_id IS NOT NULL
       AND created_at >= v_now - interval '7 days'
     GROUP BY 1
  ) p;

  RETURN jsonb_build_object(
    'dau', v_dau, 'wau', v_wau, 'mau', v_mau,
    'turns_7d', v_turns, 'avg_turns', coalesce(v_avg, 0), 'turns_per_day', coalesce(v_tpd, 0),
    'retention_1d', coalesce(v_ret1, 0), 'retention_7d', coalesce(v_ret7, 0),
    'week_retention', coalesce(v_wret, 0),
    'week_retention_base', coalesce(v_wret_c, 0), 'week_retention_kept', coalesce(v_wret_k, 0),
    'activation_7d', coalesce(v_act, 0),
    'activation_base', coalesce(v_act_c, 0), 'activation_kept', coalesce(v_act_k, 0),
    'diary_days', coalesce(v_ddays, 0),
    'crisis_l2_7d', v_l2, 'crisis_l3_7d', v_l3, 'referral_7d', v_ref,
    'referral_rate', coalesce(round(100.0 * v_ref / NULLIF(v_l2 + v_l3, 0), 1), 0),
    'alert_open', coalesce(v_open, 0), 'alert_closed', coalesce(v_closed, 0),
    'alert_median_minutes', coalesce(v_resp, 0),
    'trend', v_trend, 'platforms', coalesce(v_plat, '{}'::jsonb),
    'generated_at', v_now
  );
END;
$$;

REVOKE ALL ON FUNCTION public.admin_metrics(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_metrics(text) TO anon, authenticated;
