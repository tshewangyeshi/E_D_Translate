-- 0001: translation memory (spec §2.6, ER-14). FR-410, FR-411, FR-412, FR-143, FR-153.
-- Stores MASKED text only: real entity values never enter the database (FR-143).

CREATE TABLE segment (
  segment_key    CHAR(64) PRIMARY KEY,           -- sha256 of the masked, normalised source
  masked_source  TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Immutable content: every machine output and every approval is a new row.
-- Only invalidated_at may change (glossary/model invalidation, retention).
CREATE TABLE translation_version (
  id              BIGSERIAL PRIMARY KEY,
  segment_key     CHAR(64) NOT NULL REFERENCES segment(segment_key),
  lookup_key      CHAR(64) NOT NULL,             -- machine_key, or approved_key at approval time
  origin          TEXT NOT NULL CHECK (origin IN ('mt', 'human')),
  masked_target   TEXT NOT NULL,
  gfp             CHAR(64) NOT NULL,
  model_version   TEXT,                          -- NULL for origin = 'human'
  author          TEXT,                          -- reviewer ref for human; NULL for mt
  tag_integrity   BOOLEAN,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  invalidated_at  TIMESTAMPTZ,
  CHECK ((origin = 'mt') = (model_version IS NOT NULL))
);
CREATE INDEX translation_version_live_mt
  ON translation_version (lookup_key, id DESC)
  WHERE origin = 'mt' AND invalidated_at IS NULL;
CREATE INDEX translation_version_segment ON translation_version (segment_key, id);

CREATE FUNCTION translation_version_immutable() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'translation_version rows are immutable (FR-412)';
  END IF;
  IF (NEW.id, NEW.segment_key, NEW.lookup_key, NEW.origin, NEW.masked_target, NEW.gfp,
      NEW.model_version, NEW.author, NEW.tag_integrity, NEW.created_at)
     IS DISTINCT FROM
     (OLD.id, OLD.segment_key, OLD.lookup_key, OLD.origin, OLD.masked_target, OLD.gfp,
      OLD.model_version, OLD.author, OLD.tag_integrity, OLD.created_at)
  THEN
    RAISE EXCEPTION 'translation_version content is immutable; only invalidated_at may change (FR-412)';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER translation_version_immutable
  BEFORE UPDATE OR DELETE ON translation_version
  FOR EACH ROW EXECUTE FUNCTION translation_version_immutable();

-- Mutable workflow state, one row per segment.
CREATE TABLE review_item (
  segment_key      CHAR(64) PRIMARY KEY REFERENCES segment(segment_key),
  approved_key     CHAR(64),
  state            TEXT NOT NULL CHECK (state IN
                     ('pending_review', 'approved', 'needs_recheck', 'rejected')),
  current_version  BIGINT REFERENCES translation_version(id),
  site_id          TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX review_item_approved ON review_item (approved_key) WHERE state = 'approved';

-- term_id -> (segment, glossary fingerprint): finds exactly the versions a
-- term change affects, independent of how they are currently keyed (FR-153).
CREATE TABLE glossary_hit (
  term_id      TEXT NOT NULL,
  segment_key  CHAR(64) NOT NULL REFERENCES segment(segment_key),
  gfp          CHAR(64) NOT NULL,
  PRIMARY KEY (term_id, segment_key, gfp)
);
