SET search_path TO balans, public;
ALTER TABLE user_settings ALTER COLUMN ai_enabled SET DEFAULT true;
ALTER TABLE user_settings DISABLE ROW LEVEL SECURITY;
UPDATE user_settings s SET ai_enabled=true,voice_enabled=true,receipts_enabled=true
FROM users u WHERE u.id=s.user_id AND u.status='active';
ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_preferences ALTER COLUMN enabled SET DEFAULT true,
 ALTER COLUMN budget_alerts SET DEFAULT false,
 ALTER COLUMN monthly SET DEFAULT true, ALTER COLUMN monthly_enabled_at SET DEFAULT now();
ALTER TABLE notification_preferences DISABLE ROW LEVEL SECURITY;
UPDATE notification_preferences SET enabled=true,monthly=true,
 monthly_enabled_at=CASE WHEN enabled AND monthly THEN coalesce(monthly_enabled_at,enabled_at) ELSE now() END,
 enabled_at=CASE WHEN enabled THEN enabled_at ELSE now() END,
 reminder=false,weekly=false,budget_alerts=false,shared_mode='off',next_check_at=now();
ALTER TABLE notification_preferences ENABLE ROW LEVEL SECURITY;
