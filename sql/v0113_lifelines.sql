-- ============================================================
-- 心与星寻 · v0.11.3 三条生死线 · 云端留痕
-- 一次性执行，可重复执行（IF NOT EXISTS / DROP IF EXISTS + CREATE）
-- 这些变更此前已通过 MCP 直接落库，本文件是把它们固化为仓库唯一真相。
-- ============================================================

-- ---------- 一、埋点表 analytics_events ----------
-- 只记计数与枚举，不记原文；owner_id 用 text（本库 auth.uid() 返回 text，匿名会话为 'anon'）
CREATE TABLE IF NOT EXISTS public.analytics_events (
  id          bigserial PRIMARY KEY,
  event       text        NOT NULL,
  owner_id    text,
  platform    text,
  props       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ae_event_chk CHECK (event IN (
    'app_open', 'chat_turn', 'diary_create',
    'crisis_l2', 'crisis_l3',
    'referral_hotline', 'referral_emergency', 'referral_school', 'referral_guardian'
  ))
);

CREATE INDEX IF NOT EXISTS ae_event_time_idx ON public.analytics_events (event, created_at DESC);
CREATE INDEX IF NOT EXISTS ae_owner_idx      ON public.analytics_events (owner_id, created_at DESC);

ALTER TABLE public.analytics_events ENABLE ROW LEVEL SECURITY;

-- 撤掉一切默认权限，只留 INSERT（只写不读）
REVOKE ALL ON public.analytics_events FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.analytics_events_id_seq FROM anon, authenticated;
GRANT  INSERT ON public.analytics_events TO anon, authenticated;
GRANT  USAGE, SELECT ON SEQUENCE public.analytics_events_id_seq TO anon, authenticated;

DROP POLICY IF EXISTS ae_insert_own ON public.analytics_events;
CREATE POLICY ae_insert_own ON public.analytics_events
  FOR INSERT TO anon, authenticated
  WITH CHECK (owner_id IS NULL OR owner_id = auth.uid());

-- 注意：不要给 SELECT 策略。读取只走 SECURITY DEFINER 的 admin_metrics。

-- ---------- 二、user_profiles 年龄分层字段 ----------
ALTER TABLE public.user_profiles
  ADD COLUMN IF NOT EXISTS birth_year        integer,
  ADD COLUMN IF NOT EXISTS guardian_consent  boolean NOT NULL DEFAULT false;

-- ---------- 三、admin_metrics：周度看板数据源 ----------
-- 密钥只存哈希：比对 encode(digest(p_key,'sha256'),'hex') 与 app_config.admin_key_hash
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
  v_ret1   numeric;
  v_ret7   numeric;
  v_l2     bigint;
  v_l3     bigint;
  v_ref    bigint;
  v_trend  jsonb;
BEGIN
  SELECT v INTO v_hash FROM public.app_config WHERE k = 'admin_key_hash';
  IF v_hash IS NULL
     OR encode(digest(coalesce(p_key, ''), 'sha256'), 'hex') <> v_hash THEN
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

  -- 危机触发与转介闭环
  SELECT count(*) INTO v_l2 FROM public.analytics_events
   WHERE event = 'crisis_l2' AND created_at >= v_now - interval '7 days';
  SELECT count(*) INTO v_l3 FROM public.analytics_events
   WHERE event = 'crisis_l3' AND created_at >= v_now - interval '7 days';
  SELECT count(*) INTO v_ref FROM public.analytics_events
   WHERE event LIKE 'referral\_%' AND created_at >= v_now - interval '7 days';

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

  RETURN jsonb_build_object(
    'dau', v_dau, 'wau', v_wau, 'mau', v_mau,
    'turns_7d', v_turns, 'avg_turns', coalesce(v_avg, 0),
    'retention_1d', coalesce(v_ret1, 0), 'retention_7d', coalesce(v_ret7, 0),
    'crisis_l2_7d', v_l2, 'crisis_l3_7d', v_l3, 'referral_7d', v_ref,
    'referral_rate', coalesce(round(100.0 * v_ref / NULLIF(v_l2 + v_l3, 0), 1), 0),
    'trend', v_trend,
    'generated_at', v_now
  );
END;
$$;

REVOKE ALL ON FUNCTION public.admin_metrics(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_metrics(text) TO anon, authenticated;
