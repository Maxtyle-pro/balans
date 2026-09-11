"""UI preview with synthetic metadata only. No database, bot token or real authentication."""
from aiohttp import web
from balans.admin_web import create_app
from balans.admin_auth import AdminConfig
class Preview:
    admin_config=AdminConfig(True,'http://127.0.0.1:8091',{})
    def admin_session(self,token):return {'telegram_user_id':123456789,'role':'owner','csrf':'preview-only'}
    def admin_data(self,actor,section,search=''):
        if section=='dashboard':return [{'registered':240,'active_30d':182,'paid_users':64,'payments':97,'net_stars':9400,'refunded_stars':300,'refunds':3,'jobs':2841,'failed_jobs':12,'input_tokens':912300,'output_tokens':60500,'conversion_percent':26.7,'renewals':33,'expired_payers':5,'estimated_ai_usd':'14.20'}]
        return []
if __name__=='__main__':web.run_app(create_app(Preview()),host='127.0.0.1',port=8091,access_log=None)
