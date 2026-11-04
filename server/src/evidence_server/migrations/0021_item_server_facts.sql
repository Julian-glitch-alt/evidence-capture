-- Capture facts the server records itself for each item (received time, the
-- client-reported capture time labelled untrusted, nonce latency, liveness).
ALTER TABLE items ADD COLUMN server_facts_json TEXT NOT NULL DEFAULT '{}';
