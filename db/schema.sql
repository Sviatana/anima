-- db/schema.sql
-- PostgreSQL 13+

BEGIN;

-- =========================
-- USERS
-- =========================
CREATE TABLE IF NOT EXISTS user_profile (
  user_id    BIGINT PRIMARY KEY,
  username   TEXT,
  first_name TEXT,
  last_name  TEXT,
  locale     TEXT,
  facts      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================
-- PSYCHO PROFILE
-- =========================
CREATE TABLE IF NOT EXISTS psycho_profile (
  user_id     BIGINT PRIMARY KEY REFERENCES user_profile(user_id) ON DELETE CASCADE,
  ei          REAL NOT NULL DEFAULT 0.5,
  sn          REAL NOT NULL DEFAULT 0.5,
  tf          REAL NOT NULL DEFAULT 0.5,
  jp          REAL NOT NULL DEFAULT 0.5,
  confidence  REAL NOT NULL DEFAULT 0.30,
  mbti_type   TEXT,
  anchors     JSONB NOT NULL DEFAULT '[]'::jsonb,
  state       TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================
-- DIALOG EVENTS
-- =========================
CREATE TABLE IF NOT EXISTS dialog_events (
  id         BIGSERIAL PRIMARY KEY,
  user_id    BIGINT REFERENCES user_profile(user_id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
  text       TEXT,
  emotion    TEXT,
  mi_phase   TEXT,
  topic      TEXT,
  relevance  BOOLEAN NOT NULL DEFAULT TRUE,
  axes       JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================
-- DAILY TOPICS (optional)
-- =========================
CREATE TABLE IF NOT EXISTS daily_topics (
  user_id    BIGINT NOT NULL REFERENCES Canadianu_profile(user_id) ON DELETE CASCADE,
  dt         DATE   NOT NULL DEFAULT CURRENT_DATE,
  topics     JSONB  NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, dt)
);

-- =========================
-- TECH: idempotency
-- =========================
CREATE TABLE IF NOT EXISTS processed_updates (
  update_id  BIGINT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================
-- updated_at triggers
-- =========================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_user_profile_updated_at ON user_profile;
CREATE TRIGGER trg_user_profile_updated_at
BEFORE UPDATE ON user_profile
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_psycho_profile_updated_at ON psycho_profile;
CREATE TRIGGER trg_psycho_profile_updated_at
BEFORE UPDATE ON psycho_profile
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =========================
-- INDEXES
-- =========================
CREATE INDEX IF NOT EXISTS idx_dialog_user_created ON dialog_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dialog_role         ON dialog_events(role);
CREATE INDEX IF NOT EXISTS idx_dialog_phase        ON dialog_events(mi_phase);
CREATE INDEX IF NOT EXISTS idx_dialog_emotion      ON dialog_events(emotion);

CREATE INDEX IF NOT EXISTS idx_psycho_conf         ON psycho_profile(confidence DESC);

-- Optional: speed up idempotency checks
CREATE INDEX IF NOT EXISTS idx_processed_updates_created_at ON processed_updates(created_at DESC);

-- =========================
-- VIEWS FOR REPORTS
-- =========================
DROP VIEW IF EXISTS v_message_lengths CASCADE;
CREATE VIEW v_message_lengths AS
SELECT
  id,
  user_id,
  role,
  length(coalesce(text, '')) AS len,
  created_at
FROM dialog_events;

DROP VIEW IF EXISTS v_quality_flags CASCADE;
CREATE VIEW v_quality_flags AS
SELECT
  e.id,
  e.user_id,
  e.role,
  e.text,
  e.mi_phase,
  e.emotion,
  e.created_at,
  (position('?' in coalesce(e.text, '')) > 0) AS has_question,
  (length(coalesce(e.text, '')) BETWEEN 90 AND 350) AS in_target_len,
  (e.text ~* '(слышу|вижу|понимаю|рядом|важно)') AS has_empathy,
  (e.text ~* '(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)') AS has_banned
FROM dialog_events e
WHERE e.role = 'assistant';

DROP VIEW IF EXISTS v_quality_score CASCADE;
CREATE VIEW v_quality_score AS
SELECT
  user_id,
  date_trunc('day', created_at) AS day,
  avg((has_question::int + in_target_len::int + has_empathy::int) / 3.0) AS avg_quality,
  sum((NOT has_banned)::int)::float / NULLIF(count(*), 0) AS safety_rate,
  count(*) AS answers_total
FROM v_quality_flags
GROUP BY user_id, date_trunc('day', created_at);

DROP VIEW IF EXISTS v_phase_dist CASCADE;
CREATE VIEW v_phase_dist AS
SELECT
  date_trunc('day', created_at) AS day,
  mi_phase,
  count(*) AS cnt
FROM dialog_events
WHERE role = 'assistant'
GROUP BY 1,2;

DROP VIEW IF EXISTS v_len_daily CASCADE;
CREATE VIEW v_len_daily AS
SELECT
  date_trunc('day', created_at) AS day,
  avg(len) AS avg_len
FROM v_message_lengths
WHERE role = 'assistant'
GROUP BY 1;

DROP VIEW IF EXISTS v_confidence_hist CASCADE;
CREATE VIEW v_confidence_hist AS
SELECT
  width_bucket(confidence, 0, 1, 10) AS bucket,
  count(*) AS users
FROM psycho_profile
GROUP BY 1
ORDER BY 1;

DROP VIEW IF EXISTS v_retention_7d CASCADE;
CREATE VIEW v_retention_7d AS
WITH first_seen AS (
  SELECT user_id, min(created_at)::date AS first_day
  FROM dialog_events
  GROUP BY user_id
),
active_last_7 AS (
  SELECT DISTINCT user_id
  FROM dialog_events
  WHERE created_at >= NOW() - INTERVAL '7 days'
)
SELECT
  count(a.user_id)::float / NULLIF((SELECT count(*) FROM first_seen), 0) AS active_share_7d
FROM active_last_7 a;

COMMIT;
