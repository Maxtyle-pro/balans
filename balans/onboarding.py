"""Short entry screens and explicit, idempotent trial activation."""
import hashlib
import json
from html import escape
from datetime import datetime
from zoneinfo import ZoneInfo
from psycopg.types.json import Jsonb
from balans.domain import Reply

WELCOME = ('👋 <b>С чего начнём?</b>\n'
           'Выберите действие или просто отправьте сообщение, голосовое, фото или документ.')
DEMO_BADGE='Тестовая оплата · Stars не списываются.\n\n'
ENTRY_MENU = [[('➖ Записать расход', 'add')],
              [('➕ Записать доход', 'ui:go:income')],
              [('☰ Меню', 'ui:menu')]]


def paid_quotas(cfg):
    return {'text':cfg['text_quota'], 'voice':cfg['voice_seconds'],
            'image':cfg['image_quota'], 'analysis':cfg['analysis_quota']}


def trial_quotas(cfg):
    return {'text':cfg['trial_text_quota'], 'voice':cfg['trial_voice_seconds'],
            'image':cfg['trial_image_quota'], 'analysis':cfg['trial_analysis_quota']}


def quota_text(quotas):
    return (f"{quotas['text']} ИИ-распознаваний текста, {quotas['image']} файлов чеков / изображений, "
            f"{quotas['voice'] / 60:g} минут голоса, {quotas['analysis']} ИИ-анализов отчётов")


class Onboarding:
    def _local_deadline(self,c,value):
        if not value:return 'без срока'
        zone=c.execute('SELECT timezone FROM user_settings WHERE user_id=actor_user_id()').fetchone()['timezone']
        moment=datetime.fromisoformat(value) if isinstance(value,str) else value
        return moment.astimezone(ZoneInfo(zone)).strftime('%d.%m.%Y %H:%M')+f' ({zone})'

    def _welcome(self,c):
        if not c.execute('SELECT currency_selected_at FROM user_settings WHERE user_id=actor_user_id()').fetchone()['currency_selected_at']:
            reply=self._currency_picker(c)
            reply.text=WELCOME+'\n\n'+reply.text
            reply.parse_mode='HTML'
            return reply
        cfg=self._billing_config(c)
        access=c.execute('SELECT billing_access() AS data').fetchone()['data']
        status=access['status'];text=(DEMO_BADGE if cfg.get('demo') else '')+WELCOME
        if status=='not_started':
            buttons=[[(f"🎁 Начать {cfg['trial_days']} дней бесплатно",'trialinfo')],[('💡 Как пользоваться','howto')]]
        elif status in ('expired','suspended'):
            text+='\n\n'+('Доступ приостановлен.' if status=='suspended' else 'Период доступа завершён. История и экспорт доступны.')
            buttons=[[('⭐ Моя подписка','subscription')],[('💡 Как пользоваться','howto')]]
        else:
            buttons=ENTRY_MENU
            if status=='trial':text+='\n\n🎁 Бесплатный период до '+escape(self._local_deadline(c,access['until']))+'.'
        return Reply(text,buttons,parse_mode='HTML')

    def _trial_offer(self,c):
        cfg=self._billing_config(c)
        policy=c.execute('SELECT * FROM privacy_policy').fetchone()
        # Bind acceptance to all displayed tariff/consent values, even if an operator
        # changes configuration without incrementing terms_version.
        payload={k:cfg[k] for k in ('enabled','stars','trial_days','terms_version','terms_url',
                 'text_quota','voice_seconds','image_quota','analysis_quota',
                 'trial_text_quota','trial_voice_seconds','trial_image_quota','trial_analysis_quota')}
        payload['demo']=(cfg.get('demo',False),cfg.get('demo_generation'))
        payload['privacy']={k:policy[k] for k in ('required','version','terms_url','privacy_url')}
        token=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()[:20]
        return cfg,policy,token

    def _trial_card(self,c,details=False):
        cfg,policy,token=self._trial_offer(c)
        access=c.execute('SELECT billing_access() AS data').fetchone()['data']
        if access['status']!='not_started':return self._subscription_card(c)
        if not cfg['stars'] or not cfg['terms_url'] or (policy['required'] and (not policy['privacy_url'] or not policy['terms_url'])):
            return Reply('Условия тарифа ещё не опубликованы. /support — поддержка.',[[('Назад','start')]])
        if access['sponsor']!=str(c.execute('SELECT actor_user_id() AS id').fetchone()['id']):
            return Reply('Доступ к общему бюджету подключает его владелец. Выберите личный бюджет, чтобы начать свой бесплатный период.',[[('Мои бюджеты','workspaces')],[('Назад','start')]])
        if details:
            text=(f"На пробный период: {quota_text(trial_quotas(cfg))}.\n\n"
                  f"На оплаченные 30 дней: {quota_text(paid_quotas(cfg))}.\n\n"
                  'Один запрос может содержать несколько операций. Один PDF считается одним файлом; в обрабатываемом наборе допускается до 10 страниц. '
                  'Квоты общих бюджетов расходуются у владельца. Остаток — в «Моей подписке».\n'
                  'Ручной ввод и обычные отчёты — без отдельной квоты в течение доступа. '
                  'После окончания доступны история и экспорт.\n'
                  f"Условия тарифа: {cfg['terms_url']}")
            if policy['terms_url']:text+='\nУсловия сервиса: '+policy['terms_url']
            if policy['privacy_url']:text+='\nПолитика: '+policy['privacy_url']
            return Reply((DEMO_BADGE if cfg.get('demo') else '')+text,[[('Назад к началу','trialinfo')]])
        text=(f"{cfg['trial_days']} дней бесплатно\nЗатем — {cfg['stars']} Stars за 30 дней.\n\n"
              'Платную подписку вы подключаете сами. После пробного периода автоматического списания нет. '
              'После оплаты — продление каждые 30 дней, которое можно отключить.\n\n'
              'Нажимая «Понятно, начать», вы принимаете условия, включаете ИИ-распознавание текста '
              '(текст сообщения, валюта учёта и категории передаются в OpenAI) и ежемесячный отчёт в этот чат. '
              'Распознавание и ежемесячный отчёт работают автоматически.\n'
              f"Условия: {cfg['terms_url']}")
        if policy['terms_url'] and policy['terms_url']!=cfg['terms_url']:text+='\nУсловия сервиса: '+policy['terms_url']
        if policy['privacy_url']:text+='\nПолитика: '+policy['privacy_url']
        return Reply((DEMO_BADGE if cfg.get('demo') else '')+text,[[('Понятно, начать',f'trialaccept:{token}')],
                           [('Лимиты и условия','trialdetails'),('Назад','start')]])

    def _onboarding_callback(self,c,user,callback,sent):
        if callback=='trialinfo':return self._trial_card(c)
        if callback=='trialdetails':return self._trial_card(c,details=True)
        if not callback.startswith('trialaccept:'):return None
        cfg,policy,token=self._trial_offer(c)
        if callback.split(':',1)[1]!=token:
            reply=self._trial_card(c);reply.text='Условия изменились. Ознакомьтесь с ними ещё раз.\n\n'+reply.text;return reply
        access=c.execute('SELECT billing_access() AS data').fetchone()['data']
        if access['status']!='not_started':return self._subscription_card(c)
        if access['sponsor']!=str(user) or not cfg['enabled'] or not cfg['stars'] or not cfg['terms_url']:
            return self._trial_card(c)
        if policy['required'] and (not policy['privacy_url'] or not policy['terms_url']):return self._trial_card(c)
        if cfg.get('demo'):
            row=c.execute('UPDATE billing_demo SET trial_started_at=now(),trial_until=now()+make_interval(days=>%s),trial_quotas=%s WHERE user_id=%s AND allowed AND enabled AND trial_started_at IS NULL RETURNING trial_until',
                          (cfg['trial_days'],Jsonb(trial_quotas(cfg)),user)).fetchone()
        else:
            row=c.execute('UPDATE billing_accounts SET trial_started_at=now(),trial_until=now()+make_interval(days=>%s),trial_quotas=%s,trial_terms_version=%s WHERE user_id=%s AND trial_started_at IS NULL RETURNING trial_until',
                          (cfg['trial_days'],Jsonb(trial_quotas(cfg)),cfg['terms_version'],user)).fetchone()
        if not row:return self._subscription_card(c)
        if not cfg.get('demo'):
            c.execute("INSERT INTO billing_audit(user_id,event_key,action,details) VALUES(%s,%s,'trial_started',%s) ON CONFLICT DO NOTHING",
                      (user,f'trial:{user}',Jsonb({'offer':token,'terms_version':cfg['terms_version'],'terms_url':cfg['terms_url'],'quotas':trial_quotas(cfg),'days':cfg['trial_days']})))
        c.execute("UPDATE user_settings SET ai_enabled=true,ai_consent_version='category-ai-v1',ai_consented_at=now() WHERE user_id=%s",(user,))
        if policy['terms_url'] and policy['privacy_url']:
            c.execute('UPDATE user_settings SET service_consent_version=%s,service_consented_at=now() WHERE user_id=%s',(policy['version'],user))
        self._preference(c)
        c.execute('UPDATE notification_preferences SET enabled=true,monthly=true,monthly_enabled_at=now(),enabled_at=CASE WHEN enabled THEN enabled_at ELSE now() END,next_check_at=now() WHERE workspace_id=current_workspace() AND user_id=%s',(user,))
        return Reply((DEMO_BADGE if cfg.get('demo') else '')+'Бесплатный период до '+self._local_deadline(c,row['trial_until'])+'.\n\n'
                     'Начнём с первой записи: напишите «Кофе 250», отправьте голосовое или фото чека.\n'
                     'Ежемесячный отчёт включён. /notify — настройки.',ENTRY_MENU)
