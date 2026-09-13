"""Bounded PDF/image decoding in a disposable subprocess; no network access."""
from dataclasses import dataclass
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import warnings
from uuid import UUID
from balans.storage_lock import storage_lock

MAX_FILE_BYTES=15*1024*1024
MAX_BATCH_BYTES=30*1024*1024
MAX_PAGES=10
MAX_PIXELS=24_000_000


class MediaError(ValueError):
    pass


@dataclass
class Prepared:
    mime: str
    pages: int
    text: str
    images: list[bytes]


class BoundedBuffer(BytesIO):
    def write(self,data):
        if self.tell()+len(data)>MAX_FILE_BYTES:
            raise MediaError('Файл больше 15 МБ. Отправьте чек меньшего размера.')
        return super().write(data)


class ReceiptStorage:
    def __init__(self,root=None):
        self.root=Path(root or os.getenv('RECEIPT_STORAGE_DIR','.local/receipts')).resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.root.chmod(0o700)

    def path(self,file_id):
        return self.root/(str(UUID(str(file_id)))+'.bin')

    def put(self,file_id,data):
        path=self.path(file_id)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:
            f.write(data)
            f.flush();os.fsync(f.fileno())

    def read(self,file_id):
        path=self.path(file_id)
        if not path.is_file() or path.is_symlink():
            raise MediaError('Срок хранения исходного чека истёк. Отправьте его заново.')
        data=path.read_bytes()
        if len(data)>MAX_FILE_BYTES:
            raise MediaError('Файл больше 15 МБ.')
        return data

    def remove(self,file_id):
        with storage_lock(self.root):self.path(file_id).unlink(missing_ok=True)

    def purge(self):
        # Database deadlines and delivered notices authorize deletion.
        # A file's mtime cannot represent purchased retention extensions.
        return None


def prepare(data: bytes,documents_only: bool=False) -> Prepared:
    if not data or len(data)>MAX_FILE_BYTES:
        raise MediaError('Пустой файл или размер больше 15 МБ.')
    with tempfile.TemporaryDirectory(prefix='balans-receipt-') as directory:
        root=Path(directory)
        (root/'input').write_bytes(data)
        try:
            result=subprocess.run([sys.executable,'-m','balans.receipt_media',str(root)]+(['attachment'] if documents_only else []),capture_output=True,timeout=25,check=False)
        except subprocess.TimeoutExpired:
            raise MediaError('Файл слишком сложный для обработки. Отправьте фото нужной страницы.') from None
        if result.returncode:
            raise MediaError('Не удалось открыть файл. Нужны JPEG/PNG или безопасный PDF без пароля, до '+('500' if documents_only else '10')+' страниц.')
        info=json.loads((root/'info.json').read_text())
        return Prepared(info['mime'],info['pages'],info['text'],[(root/f'{i}.jpg').read_bytes() for i in range(0 if documents_only else info['pages'])])


def _check_pdf_actions(root):
    from pypdf.generic import IndirectObject,DictionaryObject,ArrayObject
    visited=set();remaining=50000
    forbidden={'/JavaScript','/JS','/OpenAction','/AA','/Launch','/EmbeddedFiles','/EmbeddedFile','/RichMedia','/XFA','/SubmitForm','/ImportData'}
    def visit(obj,depth=0):
        nonlocal remaining
        remaining-=1
        if remaining<0 or depth>80:raise ValueError('PDF object limit')
        if isinstance(obj,IndirectObject):
            key=(obj.idnum,obj.generation)
            if key in visited:return
            visited.add(key);obj=obj.get_object()
        if isinstance(obj,DictionaryObject):
            if any(str(k) in forbidden or (k in ('/S','/Type','/Subtype') and str(v) in forbidden) for k,v in obj.items()):raise ValueError('Active PDF content')
            for value in obj.values():visit(value,depth+1)
        elif isinstance(obj,ArrayObject):
            for value in obj:visit(value,depth+1)
    visit(root)


def _decode(root,documents_only=False):
    # Bound parser CPU and address space on platforms supporting these limits.
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(20,20))
    if sys.platform=='linux':
        resource.setrlimit(resource.RLIMIT_AS,(768*1024*1024,768*1024*1024))
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS=MAX_PIXELS
    warnings.simplefilter('error',Image.DecompressionBombWarning)
    raw=(root/'input').read_bytes()
    images=[];text=''
    if raw.startswith(b'%PDF-'):
        from pypdf import PdfReader
        import pypdfium2 as pdfium
        reader=PdfReader(BytesIO(raw),strict=True)
        if reader.is_encrypted or not 1<=len(reader.pages)<=(500 if documents_only else MAX_PAGES):
            raise ValueError('PDF limit')
        _check_pdf_actions(reader.trailer)
        if documents_only:
            (root/'info.json').write_text(json.dumps({'mime':'application/pdf','pages':len(reader.pages),'text':''}))
            return
        for page in reader.pages:
            text+=(page.extract_text() or '')+'\n'
            if len(text)>30000:
                raise ValueError('PDF text limit')
        with pdfium.PdfDocument(raw) as document:
            if len(document)!=len(reader.pages):
                raise ValueError('Page count mismatch')
            for page in document:
                width,height=page.get_size()
                if not all(math.isfinite(n) and 1<=n<=20000 for n in (width,height)):
                    raise ValueError('Page geometry')
                bitmap=page.render(scale=min(2.5,2800/max(width,height)))
                images.append(bitmap.to_pil().convert('RGB'))
                bitmap.close();page.close()
        mime='application/pdf'
    else:
        with Image.open(BytesIO(raw)) as im:
            if im.format not in ('PNG','JPEG') or getattr(im,'n_frames',1)!=1 or im.width*im.height>MAX_PIXELS:
                raise ValueError('Image type or pixel limit')
            mime='image/png' if im.format=='PNG' else 'image/jpeg'
            im.load()
            image=ImageOps.exif_transpose(im).convert('RGBA')
            white=Image.new('RGBA',image.size,'white')
            white.alpha_composite(image)
            images=[white.convert('RGB')]
    for i,image in enumerate(images):
        image.thumbnail((2800,2800))
        image.save(root/f'{i}.jpg',format='JPEG',quality=90)
        image.close()
    (root/'info.json').write_text(json.dumps({'mime':mime,'pages':len(images),'text':text},ensure_ascii=False))


if __name__=='__main__':
    _decode(Path(sys.argv[1]),len(sys.argv)>2 and sys.argv[2]=='attachment')
