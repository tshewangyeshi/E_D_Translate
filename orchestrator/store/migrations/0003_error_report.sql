-- 0003: citizen error reports (S3.5). FR-430, FR-432, NFR-303.
--
-- A citizen reading a machine translation is the only person who can tell us
-- it is wrong, so reporting has to be one tap and require nothing of them.
-- Nothing here identifies the reporter: no address, no client hash, no
-- session. Rate limiting is enforced before the insert and its counters live
-- in memory, so the protection does not leave a record of who was protected
-- against.
--
-- Triage and turning a report into an approved translation stay in E7 (S7.2);
-- this table is an inbox, not a workflow.

CREATE TABLE error_report (
  id           BIGSERIAL PRIMARY KEY,
  segment_key  CHAR(64) NOT NULL,          -- what was wrong, from /v1/translate
  site_id      TEXT NOT NULL,              -- who should triage it
  reason       TEXT NOT NULL DEFAULT 'other'
                 CHECK (reason IN ('wrong_meaning', 'wrong_term', 'not_translated',
                                   'formatting', 'offensive', 'other')),
  comment      TEXT,                       -- optional, length-capped by the API
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  triaged_at   TIMESTAMPTZ                 -- set by S7.2, not by intake
);

-- The per-segment daily cap is a counting query on the hot path; without this
-- it degrades into a sequential scan as the table grows.
CREATE INDEX error_report_segment_recent ON error_report (segment_key, created_at DESC);

-- Triage queue: oldest untriaged first, per site.
CREATE INDEX error_report_untriaged ON error_report (site_id, created_at)
  WHERE triaged_at IS NULL;
