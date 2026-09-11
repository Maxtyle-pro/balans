"""Apply operator-provided published policy metadata. Does not generate legal terms."""
import os
from urllib.parse import urlparse
import psycopg
from dotenv import load_dotenv

def main():
    load_dotenv();required=os.getenv('PRIVACY_REQUIRED','false').lower()=='true'
    values={k:os.getenv(env,'').strip() for k,env in [('terms_url','TERMS_URL'),('privacy_url','PRIVACY_URL'),('operator_country','OPERATOR_COUNTRY'),('storage_country','STORAGE_COUNTRY'),('payment_retention','PAYMENT_RETENTION_POLICY')]}
    if required:
        for key in ('terms_url','privacy_url'):
            url=urlparse(values[key])
            if url.scheme!='https' or not url.netloc:raise SystemExit('Нужны опубликованные HTTPS TERMS_URL и PRIVACY_URL.')
        if not all(values.values()) or any(len(v)>1000 for v in values.values()):raise SystemExit('Заполните страну оператора, страну хранения и правило хранения платежей.')
    from psycopg import sql
    with psycopg.connect(os.environ['MIGRATION_DATABASE_URL']) as c:
        c.execute(sql.SQL('UPDATE balans.privacy_policy SET required=%s,{},version=version+1').format(sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in values)),(required,*values.values()))
    print('Версия политики обновлена. Обязательное согласие '+('включено.' if required else 'выключено.'))
if __name__=='__main__':main()
