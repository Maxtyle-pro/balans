"""Apply explicitly supplied billing settings with schema-owner credentials."""
import os
from urllib.parse import urlparse
import psycopg
from dotenv import load_dotenv


def configuration(env):
    enabled=env.get('BILLING_ENABLED','false').lower()=='true'
    if not enabled:return {'enabled':False}
    values={'enabled':True,'owner_telegram_id':int(env.get('OWNER_TELEGRAM_ID','0')),'stars':int(env.get('SUBSCRIPTION_STARS','0')),'trial_days':int(env.get('TRIAL_DAYS','7')),'text_quota':int(env.get('TEXT_MONTHLY_QUOTA','0')),'voice_seconds':int(env.get('VOICE_MONTHLY_SECONDS','0')),'image_quota':int(env.get('IMAGE_MONTHLY_QUOTA','0')),'terms_url':env.get('TERMS_URL','').strip(),'support_contact':env.get('SUPPORT_CONTACT','').strip()}
    if not 0<values['owner_telegram_id']<2**63 or not 1<=values['stars']<=10000 or not 0<=values['trial_days']<=90:raise ValueError('Нужны корректные OWNER_TELEGRAM_ID, SUBSCRIPTION_STARS и TRIAL_DAYS.')
    if min(values[k] for k in ('text_quota','voice_seconds','image_quota'))<=0:raise ValueError('Задайте положительные месячные квоты AI.')
    url=urlparse(values['terms_url'])
    if url.scheme!='https' or not url.netloc or not values['support_contact']:raise ValueError('Нужны опубликованные HTTPS TERMS_URL и SUPPORT_CONTACT.')
    return values


def main():
    from psycopg import sql
    load_dotenv();values=configuration(os.environ)
    with psycopg.connect(os.environ['MIGRATION_DATABASE_URL']) as c:
        if values['enabled'] and not c.execute('SELECT required FROM balans.privacy_policy').fetchone()[0]:raise ValueError('Сначала опубликуйте и настройте политику: python -m scripts.configure_privacy')
        c.execute(sql.SQL('UPDATE balans.billing_config SET {},enabled_at=CASE WHEN NOT enabled AND %s THEN now() ELSE enabled_at END,terms_version=terms_version+1').format(sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in values)),(*values.values(),values['enabled']))
    print('Настройки оплаты применены; приём платежей '+('включён.' if values['enabled'] else 'выключен.'))

if __name__=='__main__':main()
