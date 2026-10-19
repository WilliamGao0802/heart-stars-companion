-- v0121_profile_display.sql  v0.12.1（2026-10-19）
-- 目的：字号 / 小知识卡片开关与频率以前只存在本机，云端同步时会被覆盖，
--       用户「调了字号过一会儿又变回去」正是这个原因。这里把三个显示偏好落到 user_profiles。
--
-- 对应代码：
--   packages/core/src/store.js   DEFAULT_PROFILE / loadProfile / fromCloudProfile / pushProfile
--   云端列名用下划线（font_scale / kb_card / kb_freq），前端 profile 用驼峰（fontScale / kbCard / kbfreq）
--
-- 幂等：全部 IF NOT EXISTS，重复执行安全。
-- 口径：
--   font_scale numeric 默认 1（0.9 小 / 1 标准 / 1.15 大 / 1.3 特大，前端 normalizeFontScale 收敛）
--   kb_card    boolean 默认 true（心理学小知识卡片开关）
--   kb_freq    text    默认 'normal'（few 少 / normal 正常 / more 多）

ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS font_scale numeric DEFAULT 1,
  ADD COLUMN IF NOT EXISTS kb_card    boolean DEFAULT true,
  ADD COLUMN IF NOT EXISTS kb_freq    text    DEFAULT 'normal';

-- 值域兜底：哪怕前端传了脏值，也不会让界面变成 0px 字号或空频率
ALTER TABLE user_profiles DROP CONSTRAINT IF EXISTS user_profiles_font_scale_chk;
ALTER TABLE user_profiles
  ADD CONSTRAINT user_profiles_font_scale_chk
  CHECK (font_scale IS NULL OR (font_scale >= 0.8 AND font_scale <= 1.5));

ALTER TABLE user_profiles DROP CONSTRAINT IF EXISTS user_profiles_kb_freq_chk;
ALTER TABLE user_profiles
  ADD CONSTRAINT user_profiles_kb_freq_chk
  CHECK (kb_freq IS NULL OR kb_freq IN ('few', 'normal', 'more'));
