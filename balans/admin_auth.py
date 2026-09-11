"""Telegram one-time login + TOTP, opaque server sessions, CSRF, short step-up window."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from dataclasses import dataclass,field
from datetime import datetime,timezone
from urllib.parse import urlparse


def digest(value):return hashlib.sha256(value.encode()).hexdigest()

def totp(secret,counter):
    key=base64.b32decode(secret.upper()+'='*((-len(secret))%8))
    raw=hmac.new(key,struct.pack('>Q',counter),hashlib.sha1).digest();offset=raw[-1]&15
    return f'{(int.from_bytes(raw[offset:offset+4],"big")&0x7fffffff)%1000000:06}'

@dataclass
class AdminConfig:
    enabled:bool=False
    base_url:str='http://127.0.0.1:8088'
    secrets:dict=field(default_factory=dict,repr=False)

    @classmethod
    def from_env(cls):
        if os.getenv('ADMIN_ENABLED','false').lower()!='true':return cls()
        owner=int(os.getenv('OWNER_TELEGRAM_ID','0'));secret=os.getenv('ADMIN_TOTP_SECRET','').strip()
        values={int(k):v for k,v in json.loads(os.getenv('ADMIN_TOTP_SECRETS','{}')).items()}
        if owner and secret:values[owner]=secret
        url=os.getenv('ADMIN_BASE_URL','http://127.0.0.1:8088').rstrip('/');parsed=urlparse(url)
        if not values or (parsed.scheme!='https' and parsed.hostname not in ('127.0.0.1','localhost','::1')):raise ValueError('Настройте ADMIN_TOTP_SECRET и безопасный ADMIN_BASE_URL.')
        for value in values.values():
            if len(value)<32:raise ValueError('Секрет TOTP должен содержать минимум 32 символа Base32.')
            totp(value,0)
        return cls(True,url,values)

class AdminAuth:
    def issue_admin_code(self,actor):
        with self._actor_transaction(actor) as c:return self._create_admin_code(c,actor)

    def _create_admin_code(self,c,actor):
        if not self.admin_config.enabled or actor not in self.admin_config.secrets:raise ValueError('Панель администратора не настроена для этого аккаунта.')
        if not c.execute('SELECT admin_role() AS role').fetchone()['role']:raise ValueError('Недостаточно прав.')
        c.execute("SELECT set_config('balans.admin_auth','on',true)")
        c.execute('UPDATE admin_login_codes SET consumed=true WHERE telegram_user_id=%s',(actor,))
        code=secrets.token_urlsafe(24)
        c.execute('INSERT INTO admin_login_codes(token_hash,telegram_user_id) VALUES(%s,%s)',(digest(code),actor))
        return code

    def admin_login(self,code,otp):
        if not self.admin_config.enabled or len(otp)!=6 or not otp.isascii() or not otp.isdigit():return None
        with self.pool.connection() as c,c.transaction():
            c.execute('SET LOCAL search_path=balans,pg_catalog');c.execute("SELECT set_config('balans.admin_auth','on',true)")
            row=c.execute('UPDATE admin_login_codes SET attempts=attempts+1 WHERE token_hash=%s AND NOT consumed AND expires_at>now() AND attempts<5 RETURNING *',(digest(code),)).fetchone()
            if not row:return None
            actor=row['telegram_user_id'];secret=self.admin_config.secrets.get(actor)
            c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(actor),))
            if not secret or not c.execute('SELECT admin_role() AS role').fetchone()['role']:return None
            counter=next((n for n in range(int(time.time())//30-1,int(time.time())//30+2) if hmac.compare_digest(totp(secret,n),otp)),None)
            if counter is None:return None
            if not c.execute('INSERT INTO admin_otp_used VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING counter',(actor,counter)).fetchone():return None
            c.execute('UPDATE admin_login_codes SET consumed=true WHERE token_hash=%s',(digest(code),))
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
            c.execute('INSERT INTO admin_sessions(token_hash,telegram_user_id,csrf) VALUES(%s,%s,%s)',(digest(token),actor,csrf))
            c.execute("INSERT INTO admin_audit(actor_telegram_id,action) VALUES(%s,'login')",(actor,))
            return token

    def admin_session(self,token):
        if not self.admin_config.enabled:return None
        with self.pool.connection() as c,c.transaction():
            c.execute('SET LOCAL search_path=balans,pg_catalog');c.execute("SELECT set_config('balans.admin_auth','on',true)")
            row=c.execute('SELECT * FROM admin_sessions WHERE token_hash=%s AND expires_at>now()',(digest(token),)).fetchone()
            if not row or row['telegram_user_id'] not in self.admin_config.secrets:return None
            c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(row['telegram_user_id']),))
            role=c.execute('SELECT admin_role() AS role').fetchone()['role']
            return dict(row,role=role) if role else None

    def admin_step_up(self,session,otp=''):
        if (datetime.now(timezone.utc)-session['verified_at']).total_seconds()<300:return True
        if len(otp)!=6 or not otp.isascii() or not otp.isdigit():return False
        actor=session['telegram_user_id'];secret=self.admin_config.secrets.get(actor)
        if not secret:return False
        counter=next((n for n in range(int(time.time())//30-1,int(time.time())//30+2) if hmac.compare_digest(totp(secret,n),otp)),None)
        if counter is None:return False
        with self._actor_transaction(actor) as c:
            c.execute("SELECT set_config('balans.admin_auth','on',true)")
            if not c.execute('INSERT INTO admin_otp_used VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING counter',(actor,counter)).fetchone():return False
            c.execute('UPDATE admin_sessions SET verified_at=now() WHERE token_hash=%s',(session['token_hash'],));return True

    def admin_logout(self,token):
        with self.pool.connection() as c,c.transaction():
            c.execute("SELECT set_config('balans.admin_auth','on',true)")
            c.execute('UPDATE balans.admin_sessions SET expires_at=now() WHERE token_hash=%s',(digest(token),))
