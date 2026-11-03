-- T45: the claimant header and button color, one per insurer. NULL until a
-- supervisor sets it; the app falls back to #1673e6 (and GET /api/admin/settings
-- returns the effective color: the org's, else #1673e6).
ALTER TABLE orgs ADD COLUMN brand_color TEXT;
