CREATE TABLE IF NOT EXISTS user_profile (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_name TEXT,
  locale TEXT,
  facts JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS psycho_profile (
  user_id BIGINT PRIMARY KEY REFERENCES user_profile(user_id) ON DELETE CASCADE,
  ei FLOAT DEFAULT 0.5,
  sn FLOAT DEFAULT 0.5,
  tf FLOAT DEFAULT 0.5,
  jp FLOAT DEFAULT 0.5,
  confidence FLOAT DEFAULT 0.3,
  mbti_type TEXT,
  anchors JSONB DEFAULT '[]'::jsonb,
  state TEXT,
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dialog_events (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES user_profile(user_id) ON DELETE CASCADE,
  role TEXT CHECK (role IN ('user','assistant','system')),
  text TEXT,
  emotion TEXT,
  mi_phase TEXT,
  topic TEXT,
  relevance BOOLEAN,
  axes JSONB,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_topics (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES user_profile(user_id) ON DELETE CASCADE,
  date DATE DEFAULT CURRENT_DATE,
  topics JSONB NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reports (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES user_profile(user_id) ON DELETE CASCADE,
  kind TEXT,
  content JSONB,
  created_at TIMESTAMP DEFAULT NOW()
);

