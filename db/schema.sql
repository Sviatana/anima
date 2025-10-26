-- db/schema.sql
-- Постгрес 13+

-- === USERS ===
CREATE TABLE IF NOT EXISTS user_profile (
  user_id    BIGINT PRIMARY KEY,
  username   TEXT,
  first_name TEXT,
  last_name  TEXT,
  locale     TEXT,
  facts      JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- === PSYCHO PROFILE ===
CREATE TABLE IF NOT EXISTS psycho_profile (
  user_id    BIGINT PRIMARY KEY REFERENCES user_profile(user_id) ON DELETE CASCADE,
  ei         FLOAT  DEFAULT 0.5,  -- E/I   (храним значение 0..1 для "первой" буквы)
  sn         FLOAT  DEFAULT 0.5,  -- N/S
  tf         FLOAT  DEFAULT 0.5,  -- T/F
  jp         FLOAT  DEFAULT 0.5,  -- J/P
  confidence FLOAT  DEFAULT 0.30,
  mbti_type  TEXT,
  anchors    JSONB  DEFAULT '[]'::jsonb,
  state      TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- === DIALOG EVENTS ===
CREATE TABLE IF NOT EXISTS dialog_events (
  id         BIGSERIAL PRIMARY KEY,
  user_id    BIGINT REFERENCES user_profile(user_id) ON DELETE CASCADE,
  role       TEXT CHECK (role IN ('user','assistant','system')),
  text       TEXT,
  emotion    TEXT,
  mi_phase   TEXT,
  topic      TEXT,
  relevance  BOOLEAN,
  axes       JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- === DAILY TOPICS (не обязательно в коде, но пригодится) ===
CREATE TABLE IF NOT EXISTS daily_topics (
  user_id    BIGINT NOT NULL REFERENCES user_profile(user_id) ON DELETE CASCADE,
  dt         DATE   NOT NULL DEFAULT CURRENT_DATE,
  topics     JSONB  NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, dt)
);

-- === TECH: идемпотентность апдейтов ===
CREATE TABLE IF NOT EXISTS processed_updates(
  update_id  BIGINT PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ===== ИНДЕКСЫ =====
CREATE INDEX IF NOT EXISTS idx_dialog_user_created ON dialog_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dialog_role         ON dialog_events(role);
CREATE INDEX IF NOT EXISTS idx_dialog_phase        ON dialog_events(mi_phase);
CREATE INDEX IF NOT EXISTS idx_dialog_emotion      ON dialog_events(emotion);
CREATE INDEX IF NOT EXISTS idx_psycho_conf         ON psycho_profile(confidence DESC);

-- ===== ВЬЮХИ ДЛЯ ОТЧЁТОВ =====
DROP VIEW IF EXISTS v_message_lengths;
CREATE VIEW v_message_lengths AS
SELECT id, user_id, role, length(coalesce(text,'')) AS len, created_at
FROM dialog_events;

DROP VIEW IF EXISTS v_quality_flags;
CREATE VIEW v_quality_flags AS
SELECT
  e.id,
  e.user_id,
  e.role,
  e.text,
  e.mi_phase,
  e.emotion,
  e.created_at,
  (position('?' in coalesce(e.text,'')) > 0) AS has_question,
  (length(coalesce(e.text,'')) BETWEEN 90 AND 350) AS in_target_len,
  (e.text ~* '(слышу|вижу|понимаю|рядом|важно)') AS has_empathy,
  (e.text ~* '(политик|религ|насили|медицинск|вакцин|диагноз|лекарств|суицид)') AS has_banned
FROM dialog_events e
WHERE e.role = 'assistant';

DROP VIEW IF EXISTS v_quality_score;
CREATE VIEW v_quality_score AS
SELECT
  user_id,
  date_trunc('day', created_at) AS day,
  avg( (has_question::int + in_target_len::int + has_empathy::int) / 3.0 ) AS avg_quality,
  sum((NOT has_banned)::int)::float / NULLIF(count(*),0) AS safety_rate,
  count(*) AS answers_total
FROM v_quality_flags
GROUP BY user_id, date_trunc('day', created_at);

DROP VIEW IF EXISTS v_phase_dist;
CREATE VIEW v_phase_dist AS
SELECT date_trunc('day', created_at) AS day, mi_phase, count(*) AS cnt
FROM dialog_events
WHERE role='assistant'
GROUP BY 1,2;

DROP VIEW IF EXISTS v_len_daily;
CREATE VIEW v_len_daily AS
SELECT date_trunc('day', created_at) AS day, avg(len) AS avg_len
FROM v_message_lengths
WHERE role='assistant'
GROUP BY 1;

DROP VIEW IF EXISTS v_confidence_hist;
CREATE VIEW v_confidence_hist AS
SELECT
  width_bucket(confidence, 0, 1, 10) AS bucket,
  count(*) AS users
FROM psycho_profile
GROUP BY 1
ORDER BY 1;

DROP VIEW IF EXISTS v_retention_7d;
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
  count(a.user_id)::float / NULLIF((SELECT count(*) FROM first_seen),0) AS active_share_7d
FROM active_last_7 a;
