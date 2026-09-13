SET search_path=balans,pg_catalog;
INSERT INTO admin_roles(telegram_user_id,role,active) VALUES(294966057,'owner',true)
ON CONFLICT(telegram_user_id) DO UPDATE SET role='owner',active=true;
