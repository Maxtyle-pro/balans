-- Media intake no longer requires a separate opt-in. Preserve recorded opt-outs.
SET search_path TO balans, public;
ALTER TABLE user_settings ALTER COLUMN voice_enabled SET DEFAULT true,
                          ALTER COLUMN receipts_enabled SET DEFAULT true;
ALTER TABLE user_settings DISABLE ROW LEVEL SECURITY;
UPDATE user_settings SET voice_enabled=true WHERE NOT voice_enabled AND voice_consented_at IS NULL;
UPDATE user_settings SET receipts_enabled=true WHERE NOT receipts_enabled AND receipts_consented_at IS NULL;
ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY;
