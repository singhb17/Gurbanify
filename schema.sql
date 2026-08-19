-- Shabad Library schema
--
-- Three layers, deliberately separated (see CLAUDE.md §5):
--   raw     - from BaniDB. never edited by hand. re-fetchable.
--   derived - summary, embedding. disposable, regenerate at will.
--   mine    - status, rarity, notes, tags. irreplaceable. back this up.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shabads (
  id                INTEGER PRIMARY KEY,

  -- raw / identity
  banidb_shabad_id  INTEGER UNIQUE,       -- NULL for manually entered shabads
  source_line       TEXT NOT NULL,        -- the line as it appeared in my Notion export
  source_line_no    INTEGER,              -- which verse in `lines` that is, so the UI
                                          -- can highlight it. NULL if it didn't match.
                                          -- (no first_line column: it was always just
                                          -- lines.line_no=1, and that's the raag/mahalla
                                          -- header, not a tuk. Read it from `lines` if
                                          -- ever needed; show source_line in list views.)
  ang               INTEGER,
  raag_en           TEXT,
  raag_pa           TEXT,
  writer            TEXT,
  source_en         TEXT,                 -- e.g. "Sri Guru Granth Sahib Ji"
  source_pa         TEXT,

  -- mine
  rarity            TEXT,
  status            TEXT,
  notes             TEXT,
  -- (no last_done / recording_url / tune_source -- dropped as unused: a date
  --  maintained by hand never gets maintained. last_surfaced below is the
  --  replacement, and the app writes it itself.)

  -- written by the swipe deck, never by hand. Drives the recency weighting:
  -- the longer since a shabad was surfaced, the likelier it is to come up.
  -- NULL = never surfaced, which is treated as the longest gap of all.
  last_surfaced     TEXT,

  is_user_added     INTEGER NOT NULL DEFAULT 0,
  imported_at       TEXT
);

CREATE TABLE IF NOT EXISTS lines (
  id                 INTEGER PRIMARY KEY,
  shabad_id          INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
  line_no            INTEGER NOT NULL,    -- 1-based position within the shabad
  banidb_verse_id    INTEGER,             -- BaniDB's stable per-line id

  -- raw
  gurmukhi           TEXT NOT NULL,
  transliteration_en TEXT,
  translation_en     TEXT,
  teeka_pa           TEXT,
  first_letters      TEXT,                -- 'gkbvv' -- so I can find my OWN
                                          -- shabads the way I find them on STTM

  -- (no summary/embedding here: the derived layer is per MODEL and lives in
  --  line_summaries below. Single columns could only ever hold one model's
  --  output, which made swapping models a migration instead of an insert.)

  UNIQUE (shabad_id, line_no)
);

-- Multi-valued metadata. kind = 'genre' | 'speed' | later: 'occasion', 'mood'.
-- Keeping these in one table means a new tag dimension needs no migration,
-- and "filter by any tag" is a single query shape.
CREATE TABLE IF NOT EXISTS tags (
  shabad_id  INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  value      TEXT NOT NULL,
  PRIMARY KEY (shabad_id, kind, value)
);

-- Shortlists built by swiping right. Kept out of `tags` on purpose: tags
-- describe what a shabad IS (its genre, its speed) and are edited one shabad at
-- a time, whereas a shortlist is working state -- built in a burst, emptied in
-- one go once a program is picked. Mixing the two would put a throwaway list in
-- the same table as metadata that took years to build.
--
-- `list` exists now although there is only one folder, so a second one later is
-- a no-op instead of a migration.
CREATE TABLE IF NOT EXISTS shortlist (
  shabad_id  INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
  list       TEXT NOT NULL DEFAULT 'Interested',
  added_at   TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (shabad_id, list)          -- swiping right twice is not an error
);

-- What I actually opened, newest last. An append-only log, NOT a set: opening
-- the same shabad five times is five rows, because "I keep coming back to this
-- one" is the interesting signal. Trimmed to the most recent HISTORY_LIMIT.
CREATE TABLE IF NOT EXISTS history (
  id         INTEGER PRIMARY KEY,
  shabad_id  INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
  opened_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Shabads I'm working on memorising. Separate from `shortlist` on purpose: a
-- shabad can be in both, either or neither -- wanting to sing something and
-- wanting to commit it to memory are different intentions.
CREATE TABLE IF NOT EXISTS learning (
  shabad_id  INTEGER PRIMARY KEY REFERENCES shabads(id) ON DELETE CASCADE,
  added_at   TEXT NOT NULL DEFAULT (datetime('now')),
  rahao_ok   INTEGER NOT NULL DEFAULT 0    -- the meaning gate; unlocks drilling
);

-- Per-line SM-2 state. WHICH LINES HAVE A ROW HERE IS THE SCOPE -- a line
-- selector added later just inserts or deletes rows, no migration needed.
CREATE TABLE IF NOT EXISTS learning_lines (
  line_id     INTEGER PRIMARY KEY REFERENCES lines(id) ON DELETE CASCADE,
  shabad_id   INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
  level       INTEGER NOT NULL DEFAULT 0,   -- rung of the scaffold, 0..MAX_LEVEL
  ease        REAL    NOT NULL DEFAULT 2.5, -- SM-2 ease factor
  interval_d  REAL    NOT NULL DEFAULT 0,   -- days until the next review
  reps        INTEGER NOT NULL DEFAULT 0,
  lapses      INTEGER NOT NULL DEFAULT 0,
  due         TEXT,                          -- YYYY-MM-DD. NULL = never seen
  last_review TEXT
);

-- ---------------------------------------------------------------------------
-- Similarity search and the model comparison (CLAUDE.md §3, §5)
-- ---------------------------------------------------------------------------

-- Which summariser models exist, and which are currently contributing results.
-- Disabling is NOT deleting: a model that keeps losing stops appearing in the
-- merged list but keeps its summaries and every vote already cast against it,
-- so turning it back on costs nothing.
CREATE TABLE IF NOT EXISTS models (
  name        TEXT PRIMARY KEY,             -- matches the key in search/models.json
  label       TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  sort_order  INTEGER NOT NULL DEFAULT 0
);

-- DERIVED, one row per line PER MODEL. Replaced lines.summary / lines.embedding,
-- which were single-model and have been dropped.
--
-- `model` leads the primary key on purpose: the dominant read is "every vector
-- for the active model", which is then one contiguous range scan of the PK btree
-- instead of a full scan. WITHOUT ROWID keeps the row in that btree directly.
--
-- `prompt_ver` is a hash of the system prompt that wrote the summary. §6 says the
-- derived layer gets regenerated whenever the prompt improves; without this you
-- end up with a silent mix of old and new summaries and no way to tell which is
-- which. With it, staleness is a query.
CREATE TABLE IF NOT EXISTS line_summaries (
  model       TEXT NOT NULL,
  line_id     INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
  summary     TEXT NOT NULL,
  embedding   BLOB,
  prompt_ver  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (model, line_id)
) WITHOUT ROWID;

-- MINE. "Are these two lines genuinely connected" is a judgement about Gurbani,
-- not about a model, so it survives every regeneration of the derived layer and
-- must be backed up. Months of these become the ground-truth set that lets any
-- future model be evaluated for free, on the real library.
CREATE TABLE IF NOT EXISTS line_relations (
  query_line_id   INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
  result_line_id  INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
  verdict         INTEGER NOT NULL,          -- +1 connected, -1 unrelated
  judged_at       TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (query_line_id, result_line_id)
);

-- DERIVED: which model offered which result, and how prominently. Kept apart
-- from the verdict above so that regenerating summaries throws away the model's
-- output and not my judgement. `rank` is stored rather than a score so the
-- weighting can change later without re-voting anything.
CREATE TABLE IF NOT EXISTS model_results (
  query_line_id   INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
  result_line_id  INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
  model           TEXT NOT NULL,
  rank            INTEGER NOT NULL,          -- 0-based position in that model's list
  prompt_ver      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (query_line_id, result_line_id, model)
);

-- What the indexer is doing right now, written by search/index_library.py.
--
-- Without this the app cannot tell "half the lines are done because I stopped
-- the script" from "half are done because it is working on them this second" --
-- both are simply a partial count, and both rendered as the same amber badge.
-- A job row makes the difference sayable.
--
-- `updated_at` is a heartbeat: a row still marked running but not touched for
-- minutes means the process died without cleaning up, which is the normal
-- outcome of a reboot or a kill. Read staleness from it rather than trusting
-- `state` alone.
CREATE TABLE IF NOT EXISTS index_jobs (
  id          INTEGER PRIMARY KEY,
  model       TEXT NOT NULL,
  state       TEXT NOT NULL,               -- running | done | failed | stopped
  phase       TEXT,                        -- summarise | embed
  total       INTEGER NOT NULL DEFAULT 0,
  done        INTEGER NOT NULL DEFAULT 0,
  spent       REAL    NOT NULL DEFAULT 0,
  error       TEXT,
  started_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_index_jobs_state ON index_jobs(state, updated_at);

-- Small key/value bag for app-level preferences. Server-side, not localStorage:
-- the server holds the vectors and does the ranking, so the setting has to live
-- where the work happens or the two drift apart.
CREATE TABLE IF NOT EXISTS settings (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learning_due ON learning_lines(due);
CREATE INDEX IF NOT EXISTS idx_learning_shabad ON learning_lines(shabad_id);
CREATE INDEX IF NOT EXISTS idx_relations_result ON line_relations(result_line_id);
CREATE INDEX IF NOT EXISTS idx_model_results_model ON model_results(model);

CREATE INDEX IF NOT EXISTS idx_lines_shabad ON lines(shabad_id);
-- idx_lines_letters is created by migrate() instead: on an existing database the
-- column is added there, and this file runs before it.
CREATE INDEX IF NOT EXISTS idx_tags_lookup  ON tags(kind, value);
