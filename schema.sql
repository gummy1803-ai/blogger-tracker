-- ════════════════════════════════════════════
-- 博主建联管理系统 · Supabase 数据库建表语句
-- 在 Supabase 控制台 → SQL Editor 中执行
-- ════════════════════════════════════════════

-- 1. 博主记录表（核心表）
CREATE TABLE IF NOT EXISTS bloggers (
  id           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  link         TEXT,                          -- 博主主页链接（唯一标识）
  name         TEXT,                          -- 博主名
  status       TEXT,                          -- 回复情况：没回 / 抖音私信建联 / 引导到私域了
  followers    FLOAT DEFAULT 0,               -- 粉丝量（万人）
  owner        TEXT,                          -- 跟进人
  record_date  DATE,                          -- 录入日期
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- 2. 以 link + record_date 为唯一约束，防止重复写入
ALTER TABLE bloggers
  DROP CONSTRAINT IF EXISTS bloggers_link_date_unique;
ALTER TABLE bloggers
  ADD CONSTRAINT bloggers_link_date_unique UNIQUE (link, record_date);

-- 3. 常用查询索引
CREATE INDEX IF NOT EXISTS idx_bloggers_date  ON bloggers (record_date DESC);
CREATE INDEX IF NOT EXISTS idx_bloggers_owner ON bloggers (owner);
CREATE INDEX IF NOT EXISTS idx_bloggers_status ON bloggers (status);

-- 4. 自动更新 updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bloggers_updated_at ON bloggers;
CREATE TRIGGER trg_bloggers_updated_at
  BEFORE UPDATE ON bloggers
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- 5. 日报快照表（每天运行结果存档，方便趋势分析）
CREATE TABLE IF NOT EXISTS daily_reports (
  id           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  report_date  DATE UNIQUE,
  total        INT,
  contacted    INT,
  success      INT,
  rate         FLOAT,
  w_total      INT,
  w_contacted  INT,
  w_success    INT,
  w_rate       FLOAT,
  report_text  TEXT,                          -- 完整播报文本
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- 6. 开放前端只读权限（anon key 只能 SELECT）
ALTER TABLE bloggers      ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_reports ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public_read_bloggers" ON bloggers
  FOR SELECT USING (true);

CREATE POLICY "public_read_reports" ON daily_reports
  FOR SELECT USING (true);

-- 写入只能通过 service_role key（Python 脚本用）
CREATE POLICY "service_write_bloggers" ON bloggers
  FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_write_reports" ON daily_reports
  FOR ALL USING (auth.role() = 'service_role');
