from io import BytesIO
import os
import time
from uuid import uuid4

from PIL import Image
import pytest

from balans.receipt_media import prepare,MediaError,BoundedBuffer,MAX_FILE_BYTES,ReceiptStorage
from receipt_fixtures import pdf_bytes,scan_pdf_bytes,photo_bytes


@pytest.mark.parametrize('data,mime,text',[(pdf_bytes(),'application/pdf','TOTAL RUB'),(scan_pdf_bytes(),'application/pdf',''),(photo_bytes(),'image/png',''),(photo_bytes(True),'image/png','')])
def test_real_pdf_and_image_decoding(data,mime,text):
    result=prepare(data)
    assert result.mime==mime
    assert result.pages==1
    if text:
        assert text in result.text
    with Image.open(BytesIO(result.images[0])) as im:
        assert im.format=='JPEG'
        assert max(im.size)<=2800


@pytest.mark.parametrize('data',[b'',b'not a receipt',b'%PDF-fake',pdf_bytes(encrypted=True),pdf_bytes(pages=11)])
def test_bad_inputs_rejected(data):
    with pytest.raises(MediaError):
        prepare(data)


def test_buffer_limit_and_private_storage(tmp_path):
    with BoundedBuffer() as b:
        b.seek(MAX_FILE_BYTES)
        with pytest.raises(MediaError):
            b.write(b'x')
    store=ReceiptStorage(tmp_path/'receipts')
    identity=uuid4();store.put(identity,b'original')
    assert store.read(identity)==b'original'
    assert store.path(identity).stat().st_mode&0o777==0o600
    with pytest.raises(ValueError):
        store.path('../../outside')
    old=time.time()-31*86400
    os.utime(store.path(identity),(old,old))
    # File age cannot override a retention extension recorded in the database.
    assert store.read(identity)==b'original'
    store.purge()
    assert store.path(identity).exists()
    store.remove(identity)
    assert not store.path(identity).exists()
