-- 0011_send_back_rounds (track 3): Send back reopens the claimant link for the
-- requested photos only. The resubmission is round 2 (3, ...): sealed, analysed,
-- reported and decided again. Earlier rounds stay as history, never overwritten.
ALTER TABLE claims ADD COLUMN round INTEGER NOT NULL DEFAULT 1;
ALTER TABLE claims ADD COLUMN reopen_slots_json TEXT;    -- slots the claimant must retake; NULL = not reopened
ALTER TABLE capture_sessions ADD COLUMN slots_json TEXT;  -- slots this session may capture; NULL = every slot
ALTER TABLE seals ADD COLUMN round INTEGER NOT NULL DEFAULT 1;
ALTER TABLE seals DROP CONSTRAINT seals_pkey;
ALTER TABLE seals ADD PRIMARY KEY (claim_id, round);
ALTER TABLE decisions ADD COLUMN round INTEGER NOT NULL DEFAULT 1;
ALTER TABLE decisions DROP CONSTRAINT decisions_pkey;
ALTER TABLE decisions ADD PRIMARY KEY (claim_id, round);
ALTER TABLE reviews ADD COLUMN round INTEGER NOT NULL DEFAULT 1;
ALTER TABLE reports ADD COLUMN round INTEGER NOT NULL DEFAULT 1;  -- one row per claim: the latest round's report, same secret link
ALTER TABLE outbox ADD COLUMN round INTEGER NOT NULL DEFAULT 1;
ALTER TABLE outbox DROP CONSTRAINT outbox_claim_id_event_key;
ALTER TABLE outbox ADD CONSTRAINT outbox_claim_event_round UNIQUE (claim_id, event, round);
