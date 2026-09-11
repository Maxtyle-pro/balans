"""Service-account export to a sheet whose editor proves control with a challenge."""
import os
import re
import secrets
from urllib.parse import quote
from decimal import Decimal
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from balans.report_data import export_rows

API='https://sheets.googleapis.com/v4/spreadsheets/'


def spreadsheet_id(value):
    match=re.fullmatch(r'https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,100})(?:/[^\s]*)?',value.strip())
    if not match:raise ValueError('Нужна ссылка https://docs.google.com/spreadsheets/d/…/edit')
    return match[1]


class SheetsError(Exception):pass


class Sheets:
    def __init__(self,path=None,session=None,email=None):
        path=path or os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE','').strip()
        self.session=session;self.email=email
        if path:
            credentials=Credentials.from_service_account_file(path,scopes=['https://www.googleapis.com/auth/spreadsheets'])
            # Credentials are local owner configuration, never provided through Telegram.
            self.email=credentials.service_account_email
            self.session=AuthorizedSession(credentials,max_refresh_attempts=0,refresh_timeout=15)
    @property
    def available(self):return self.session is not None
    def close(self):
        if self.session:self.session.close()
    def _request(self,method,path,**kwargs):
        try:
            response=self.session.request(method,API+path,timeout=30,**kwargs)
            if response.status_code>=400:raise SheetsError('Google rejected request')
            return response.json()
        except SheetsError:raise
        except Exception:raise SheetsError('Google unavailable') from None
    def verify(self,identity,challenge):
        # Require an explicit control tab. Do not inspect unrelated user cells.
        data=self._request('GET',identity+'/values/'+quote("'Баланс-доступ'!A1",safe=''),params={'valueRenderOption':'UNFORMATTED_VALUE'})
        values=data.get('values',[])
        return bool(values and values[0] and isinstance(values[0][0],str) and secrets.compare_digest(values[0][0],challenge))
    def export(self,identity,challenge,job_id,tab_id,snapshot):
        if not self.verify(identity,challenge):raise SheetsError('Control challenge removed')
        title='Баланс '+snapshot['start']+' '+job_id
        metadata=self._request('GET',identity,params={'fields':'sheets.properties(sheetId,title)'})
        for sheet in metadata.get('sheets',[]):
            prop=sheet['properties']
            if prop['sheetId']==tab_id:
                if prop['title']==title:return f'https://docs.google.com/spreadsheets/d/{identity}/edit#gid={tab_id}'
                raise SheetsError('Sheet id collision')
        rows=[]
        for index,row in enumerate(export_rows(snapshot)):
            cells=[]
            for col,value in enumerate(row):
                # All external text is a literal stringValue, never formulaValue.
                cell={'numberValue':float(Decimal(value))} if index and col==3 else {'stringValue':str(value)}
                cells.append({'userEnteredValue':cell})
                if index==0 and col==0:
                    cells[-1]['note']='Снимок '+snapshot['created_at']+'; период '+snapshot['start']+' — '+snapshot['end']+'; '+snapshot['timezone']
            rows.append({'values':cells})
        requests=[{'addSheet':{'properties':{'sheetId':tab_id,'title':title,'gridProperties':{'rowCount':max(100,len(rows)),'columnCount':len(rows[0]),'frozenRowCount':1}}}},
                  {'updateCells':{'start':{'sheetId':tab_id,'rowIndex':0,'columnIndex':0},'rows':rows,'fields':'userEnteredValue,note'}},
                  {'repeatCell':{'range':{'sheetId':tab_id,'startRowIndex':0,'endRowIndex':1},'cell':{'userEnteredFormat':{'backgroundColor':{'red':.88,'green':.95,'blue':.95},'textFormat':{'bold':True}}},'fields':'userEnteredFormat'}},
                  {'repeatCell':{'range':{'sheetId':tab_id,'startRowIndex':1,'startColumnIndex':3,'endColumnIndex':4},'cell':{'userEnteredFormat':{'numberFormat':{'type':'NUMBER','pattern':'0.00'}}},'fields':'userEnteredFormat.numberFormat'}},
                  {'setBasicFilter':{'filter':{'range':{'sheetId':tab_id,'endRowIndex':len(rows),'endColumnIndex':len(rows[0])}}}}]
        # One atomic batch: an uncertain result can be recovered by the same tab ID.
        self._request('POST',identity+':batchUpdate',json={'requests':requests})
        return f'https://docs.google.com/spreadsheets/d/{identity}/edit#gid={tab_id}'
