SET search_path TO balans, public;
ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
ALTER TABLE user_settings NO FORCE ROW LEVEL SECURITY;
UPDATE user_settings s SET ai_enabled=true,voice_enabled=true,receipts_enabled=true
FROM users u WHERE u.id=s.user_id AND u.status='active';
ALTER TABLE users FORCE ROW LEVEL SECURITY;
ALTER TABLE user_settings FORCE ROW LEVEL SECURITY;
