-- 心与星寻 · 管理后台数据库函数（PostgreSQL / PostgREST RPC）
-- 应用：wbapp_udsc4Pu0hZdUiMpG3JcM2i
--
-- 为什么用函数而不是「表直读 + RLS」：
--   云网关**不透传自定义请求头**（实测 current_setting('request.headers') 为空），
--   所以「x-admin-key 头 + RLS 策略」这条路读不到任何行。把密钥放进**请求体参数**、
--   由 SECURITY DEFINER 函数内部校验，才稳。
--
-- 安全设计（v1.0.1 起）：
--   · **源码里不再出现明文密钥**：密钥只存在 app_config.admin_key_hash（SHA-256 哈希），
--     浏览器和仓库都拿不到可用凭据；明文只放在 Streamlit Secrets / 环境变量；
--   · 校验用 encode(digest(p_key,'sha256'),'hex') 与库内哈希比对（需 pgcrypto）；
--   · 函数用 SECURITY DEFINER，以属主身份跑，绕过表上的行级安全，
--     因此能读到所有用户的资料 / 记忆 / 对话；
--   · 只提供 **读**，不提供任何写操作，后台不会误改线上数据；
--   · 列表类入参做了上限收敛（least(...,500)），避免被当成放大器。
--
-- 初次部署顺序（务必按序执行，逐条执行，exec_sql 单次只接受一条语句）：
--   1) CREATE EXTENSION IF NOT EXISTS pgcrypto;
--   2) 建 app_config 表并写入 admin_key_hash（见文件末尾「0. 密钥托管」）
--   3) 逐个执行下面的函数
--   4) 在 Streamlit Secrets 里把 db_key 配成同一个明文密钥
--
-- 更换密钥：只用改 app_config 里的一行哈希 + Secrets 的 db_key，函数无需改动。

-- ============ 0. 密钥托管（一次性）============
-- CREATE TABLE IF NOT EXISTS public.app_config (
--   k text PRIMARY KEY, v text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
-- ALTER TABLE public.app_config ENABLE ROW LEVEL SECURITY;
-- REVOKE ALL ON TABLE public.app_config FROM anon, authenticated;
-- INSERT INTO public.app_config(k, v)
-- VALUES ('admin_key_hash', encode(digest('<把新密钥写在这里>','sha256'),'hex'))
-- ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now();

-- ============ 1. 总览 KPI ============
CREATE OR REPLACE FUNCTION admin_stats(p_key text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO public AS $fn$
BEGIN
  IF encode(digest(p_key,'sha256'),'hex') <> (SELECT v FROM public.app_config WHERE k='admin_key_hash') THEN RAISE EXCEPTION 'forbidden'; END IF;
  RETURN (SELECT jsonb_build_object(
    'users',          (SELECT count(*) FROM auth.users WHERE coalesce(username,'') <> 'administrator'),
    'users_new_1d',   (SELECT count(*) FROM auth.users WHERE coalesce(username,'') <> 'administrator' AND created_at >= now() - interval '1 day'),
    'users_new_7d',   (SELECT count(*) FROM auth.users WHERE coalesce(username,'') <> 'administrator' AND created_at >= now() - interval '7 days'),
    'profiles',       (SELECT count(*) FROM user_profiles),
    'memory',         (SELECT count(*) FROM user_memory),
    'messages',       (SELECT count(*) FROM chat_messages),
    'diaries',        (SELECT count(*) FROM diary_entries),
    'lamps',          (SELECT count(*) FROM lamp_entries),
    'alerts',         (SELECT count(*) FROM admin_alerts),
    'alerts_7d',      (SELECT count(*) FROM admin_alerts WHERE created_at >= now() - interval '7 days'),
    'bindings',       (SELECT count(*) FROM user_bindings),
    'library',        (SELECT count(*) FROM chat_library),
    'turns_total',    (SELECT coalesce(sum(turn_count),0) FROM user_memory),
    'active_7d',      (SELECT count(DISTINCT owner_id) FROM chat_messages WHERE created_at >= now() - interval '7 days'),
    'last_message_at',(SELECT coalesce(max(created_at)::text,'') FROM chat_messages)
  ));
END $fn$;

-- ============ 2. 用户列表（以 auth.users 为准，左连业务表）============
-- 关键：auth.users.sub 才是业务表里的 owner_id（微信用户形如 wx:xxx），
--       不是 auth.users.id（那是库内自增 id）。搞错会一条都关联不上。
CREATE OR REPLACE FUNCTION admin_users(p_key text, p_limit int DEFAULT 200, p_offset int DEFAULT 0, p_q text DEFAULT '')
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO public AS $fn$
BEGIN
  IF encode(digest(p_key,'sha256'),'hex') <> (SELECT v FROM public.app_config WHERE k='admin_key_hash') THEN RAISE EXCEPTION 'forbidden'; END IF;
  RETURN coalesce((
    SELECT jsonb_agg(x ORDER BY x->>'created_at' DESC)
    FROM (
      SELECT jsonb_build_object(
        'uid', u.sub,
        'phone', coalesce(u.phone_number,''),
        'username', coalesce(u.username,''),
        'status', coalesce(u.status,0),
        'created_at', u.created_at::text,
        'last_login', coalesce(u.last_login::text,''),
        'provider', CASE WHEN u.sub LIKE 'wx:%' THEN 'wechat'
                         WHEN coalesce(u.phone_number,'') <> '' THEN 'phone'
                         ELSE 'unknown' END,
        'has_profile', (p.owner_id IS NOT NULL),
        'nick_name', coalesce(p.nick_name,''),
        'signature', coalesce(p.signature,''),
        'is_member', coalesce(p.is_member,false),
        'coins', coalesce(p.coins,0),
        'turns', coalesce(m.turn_count,0),
        'mood_tone', coalesce(m.mood->>'tone',''),
        'memory_updated', coalesce(m.updated_at::text,''),
        'msg_count',   (SELECT count(*) FROM chat_messages  c WHERE c.owner_id = u.sub),
        'diary_count', (SELECT count(*) FROM diary_entries  d WHERE d.owner_id = u.sub),
        'lamp_count',  (SELECT count(*) FROM lamp_entries   l WHERE l.owner_id = u.sub)
      ) AS x
      FROM auth.users u
      LEFT JOIN user_profiles p ON p.owner_id = u.sub
      LEFT JOIN user_memory   m ON m.user_id  = u.sub
      WHERE coalesce(u.username,'') <> 'administrator'
        AND (p_q = '' OR coalesce(u.phone_number,'') ILIKE '%'||p_q||'%'
             OR u.sub ILIKE '%'||p_q||'%'
             OR coalesce(p.nick_name,'') ILIKE '%'||p_q||'%')
      ORDER BY u.created_at DESC
      LIMIT p_limit OFFSET p_offset
    ) s
  ), '[]'::jsonb);
END $fn$;

-- ============ 3. 单个用户详情 ============
CREATE OR REPLACE FUNCTION admin_user_detail(p_key text, p_uid text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO public AS $fn$
BEGIN
  IF encode(digest(p_key,'sha256'),'hex') <> (SELECT v FROM public.app_config WHERE k='admin_key_hash') THEN RAISE EXCEPTION 'forbidden'; END IF;
  RETURN jsonb_build_object(
    'uid', p_uid,
    'account', (SELECT jsonb_build_object(
                  'phone', coalesce(phone_number,''),
                  'created_at', created_at::text,
                  'last_login', coalesce(last_login::text,''),
                  'status', coalesce(status,0))
                FROM auth.users WHERE sub = p_uid),
    'profile', (SELECT to_jsonb(p) FROM user_profiles p WHERE p.owner_id = p_uid),
    'memory',  (SELECT jsonb_build_object(
                  'profile', m.profile, 'mood', m.mood,
                  'turn_count', m.turn_count, 'summarized_at', m.summarized_at,
                  'updated_at', m.updated_at::text)
                FROM user_memory m WHERE m.user_id = p_uid),
    'messages', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                    'role', c.role, 'content', c.content, 'created_at', c.created_at::text)), '[]'::jsonb)
                 FROM (SELECT role, content, created_at FROM chat_messages
                       WHERE owner_id = p_uid ORDER BY created_at DESC LIMIT 40) c),
    'diaries', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'content', d.content, 'source', coalesce(d.source,''),
                   'created_at', d.created_at::text)), '[]'::jsonb)
                FROM (SELECT content, source, created_at FROM diary_entries
                      WHERE owner_id = p_uid ORDER BY created_at DESC LIMIT 30) d),
    'lamps',   (SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'content', coalesce(l.content,''), 'week_label', coalesce(l.week_label,''),
                   'week_count', coalesce(l.week_count,0),
                   'created_at', l.created_at::text)), '[]'::jsonb)
                FROM (SELECT content, week_label, week_count, created_at FROM lamp_entries
                      WHERE owner_id = p_uid ORDER BY created_at DESC LIMIT 30) l)
  );
END $fn$;

-- ============ 4. 警报列表（含台账四状态与时间线）============
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

-- ============ 5. 回应库（小程序端 116 条）============
CREATE OR REPLACE FUNCTION admin_library(p_key text, p_q text DEFAULT '', p_limit int DEFAULT 500)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO public AS $fn$
BEGIN
  IF encode(digest(p_key,'sha256'),'hex') <> (SELECT v FROM public.app_config WHERE k='admin_key_hash') THEN RAISE EXCEPTION 'forbidden'; END IF;
  RETURN coalesce((
    SELECT jsonb_agg(to_jsonb(c))
    FROM (SELECT id, mood, topic, text, weight, enabled FROM chat_library
          WHERE (p_q = '' OR text ILIKE '%'||p_q||'%'
                 OR mood ILIKE '%'||p_q||'%' OR topic ILIKE '%'||p_q||'%')
          ORDER BY id LIMIT p_limit) c
  ), '[]'::jsonb);
END $fn$;

-- ============ 授权（PostgREST 的 anon 角色要能调用）============
GRANT EXECUTE ON FUNCTION admin_stats(text)                        TO anon, authenticated;
GRANT EXECUTE ON FUNCTION admin_users(text, int, int, text)        TO anon, authenticated;
GRANT EXECUTE ON FUNCTION admin_user_detail(text, text)            TO anon, authenticated;
GRANT EXECUTE ON FUNCTION admin_alerts_list(text, int)             TO anon, authenticated;
GRANT EXECUTE ON FUNCTION admin_library(text, text, int)           TO anon, authenticated;

-- ============ 6. 健康检查（SQL 语言版，注意它是用 `=` 而不是 `<>`）============
-- 已按哈希校验重写；2026-10-17 加固时同步迁移。
CREATE OR REPLACE FUNCTION public.admin_ping(p_key text) RETURNS text
LANGUAGE sql SECURITY DEFINER SET search_path TO 'public' AS $fn$
  SELECT CASE WHEN encode(digest(p_key,'sha256'),'hex')
                = (SELECT v FROM public.app_config WHERE k = 'admin_key_hash')
              THEN 'ok' ELSE 'denied' END
$fn$;
GRANT EXECUTE ON FUNCTION public.admin_ping(text) TO anon, authenticated;


-- ============ 7. 周度看板（埋点聚合，P9 五个指标 + P14 止损线）============
-- 数据源：analytics_events（只 INSERT 的埋点表，RLS 开启，anon/authenticated 不能读）。
-- 表结构与 RLS 见 sql/v0113_lifelines.sql；本函数的迁移留痕见 sql/v0114_metrics_alerts.sql。
-- 口径说明写在函数体注释里，改字段名必须同步改 views/admin.py 的 _page_metrics。

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


-- ============ 8. 警报台账状态流转（P7 / P10：触发→响应→转介→关闭）============
-- 台账字段由 sql/v0114_metrics_alerts.sql 迁移加上：
--   status（open/acked/referred/closed）+ acked_at / referred_at / closed_at
--   + handler / note / referral_path
-- 后台唯一允许写库的入口：白名单四字段，时间戳由服务端落（客户端传时间可被伪造）。
-- 已关闭的记录不许再改（归档不可抹改）。

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


-- ============================================================
-- v0.12.0 追加：数量占比页两个聚合函数（与 v0120_share_distribution.sql 逐字一致）
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
