-- 0005: Tier 2 machine output is flagged for review (S3.1). FR-511.
--
-- Every machine translation a Tier 2 page shows opens a review item, so
-- reviewers see what citizens are being shown. Two facts about each item are
-- needed to cap how fast a site's queue can grow:
--
--   created_at  when the item was opened (updated_at moves on every change, so
--               it cannot answer "how many were opened today")
--   raised_by   'request'  a page view caused it; counted against the cap
--               'operator' pre-warm, re-warm, approval or seed import; never
--                          capped, because the operator chose to do it
--
-- The cap exists because /v1/translate is keyless: without it, anyone who can
-- get text past the distinct-clients rule could bury the reviewers in forged
-- segments. Past the cap an item is still opened, as 'owed': the translation
-- is served, nothing is lost, and an operator releases owed items into the
-- queue when there is room (python -m orchestrator.ops.review_owed).
--
-- Fail fast rather than queue: while this file runs it holds an exclusive
-- lock on review_item, which every page view reads. If it has to wait behind
-- a long transaction, every reader would wait behind it.
SET LOCAL lock_timeout = '5s';

ALTER TABLE review_item
  ADD COLUMN created_at TIMESTAMPTZ,
  ADD COLUMN raised_by  TEXT NOT NULL DEFAULT 'operator'
    CHECK (raised_by IN ('request', 'operator'));

-- Existing rows were all opened by an approval: date them by their first
-- human version, not by when this migration happened to run.
UPDATE review_item r
   SET created_at = COALESCE(
         (SELECT min(v.created_at) FROM translation_version v
           WHERE v.segment_key = r.segment_key AND v.origin = 'human'),
         r.updated_at);

ALTER TABLE review_item
  ALTER COLUMN created_at SET DEFAULT now(),
  ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE review_item DROP CONSTRAINT review_item_state_check;
ALTER TABLE review_item ADD CONSTRAINT review_item_state_check CHECK (state IN
  ('pending_review', 'owed', 'approved', 'needs_recheck', 'rejected'));

-- The worker stores on behalf of a request it never saw, so the job has to
-- carry whether the result must be flagged. The tier itself is not stored:
-- it belongs to the request, not to the content.
ALTER TABLE translation_job
  ADD COLUMN review BOOLEAN NOT NULL DEFAULT FALSE;
