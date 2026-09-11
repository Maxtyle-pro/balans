"""Bootstrap one explicitly configured owner; never infer an owner from bot traffic."""
import os
import psycopg
from dotenv import load_dotenv
from balans.admin_auth import AdminConfig

def main():
    load_dotenv();config=AdminConfig.from_env();owner=int(os.getenv('OWNER_TELEGRAM_ID','0'))
    if not config.enabled or owner not in config.secrets:raise SystemExit('Настройте ADMIN_ENABLED, OWNER_TELEGRAM_ID и ADMIN_TOTP_SECRET.')
    with psycopg.connect(os.environ['MIGRATION_DATABASE_URL']) as c:
        c.execute("INSERT INTO balans.admin_roles(telegram_user_id,role) VALUES(%s,'owner') ON CONFLICT(telegram_user_id) DO UPDATE SET role='owner',active=true",(owner,))
        c.execute('UPDATE balans.billing_config SET owner_telegram_id=%s',(owner,))
    print('Владелец панели настроен. Перезапустите бота; /admin выдаст одноразовый код входа.')
if __name__=='__main__':main()
