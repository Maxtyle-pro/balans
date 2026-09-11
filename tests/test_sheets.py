from balans.sheets import Sheets,SheetsError,spreadsheet_id
from scripts.check_report_pdf import fixture
import pytest


class Response:
    status_code=200
    def __init__(self,data):self.data=data
    def json(self):return self.data


class Session:
    def __init__(self):self.calls=[];self.created=None;self.challenge='challenge';self.fail_after_write=False
    def close(self):pass
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        if '/values/' in url:return Response({'values':[[self.challenge]]})
        if method=='GET':return Response({'sheets':[{'properties':self.created}] if self.created else []})
        requests=kwargs['json']['requests'];self.created=requests[0]['addSheet']['properties']
        if self.fail_after_write:raise TimeoutError()
        return Response({})


def test_atomic_export_typed_literals_and_recover_uncertain_write():
    session=Session();sheets=Sheets(session=session,email='bot@test');snapshot=fixture()
    snapshot['rows'][0]['description']='=IMPORTXML("evil")';session.fail_after_write=True
    with pytest.raises(SheetsError):sheets.export('a'*30,'challenge','job-1',12,snapshot)
    requests=session.calls[-1][2]['json']['requests']
    cells=requests[1]['updateCells']['rows'][1]['values']
    assert cells[7]['userEnteredValue']=={'stringValue':'=IMPORTXML("evil")'}
    assert cells[3]['userEnteredValue']=={'numberValue':137.25}
    session.fail_after_write=False
    url=sheets.export('a'*30,'challenge','job-1',12,snapshot)
    assert url.endswith('gid=12')
    assert len([x for x in session.calls if x[0]=='POST'])==1


def test_control_proof_and_collision():
    session=Session();sheets=Sheets(session=session,email='bot@test')
    with pytest.raises(SheetsError):sheets.export('a'*30,'wrong','job',12,fixture())
    assert all(x[0]!='POST' for x in session.calls)
    session.created={'sheetId':12,'title':'someone else'}
    with pytest.raises(SheetsError):sheets.export('a'*30,'challenge','job',12,fixture())


def test_strict_google_url():
    assert spreadsheet_id('https://docs.google.com/spreadsheets/d/'+'a'*30+'/edit')=='a'*30
    for value in ('file:///etc/passwd','https://evil.test/sheet','https://docs.google.com.evil.test/spreadsheets/d/'+'a'*30):
        with pytest.raises(ValueError):spreadsheet_id(value)
