from __future__ import annotations
import io
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from service import DEFAULTS, FIELD_NAMES, Workspace, checked_config


@dataclass
class Reply:
    status: int = 200
    body: bytes = b''
    content_type: str = 'application/json; charset=utf-8'
    headers: dict = field(default_factory=dict)


def json_reply(data, status=200):
    return Reply(status,json.dumps(data,ensure_ascii=False).encode('utf-8'))


class Application:
    def __init__(self,root: Path,data_root: Path|None=None):
        self.root = root.resolve()
        self.data_root = (data_root if data_root is not None else root).resolve()
        self.workspace = Workspace(self.data_root)
        self.token = secrets.token_urlsafe(32)
        self.render_lock = threading.Lock()

    def route(self,method: str,target: str,headers,stream) -> Reply:
        parsed = urlsplit(target)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        host = headers.get('Host','')
        if host.split(':')[0] not in ('127.0.0.1','localhost'):
            return json_reply({'error':'请通过本机地址访问工具。'},403)
        if method == 'POST':
            origin = headers.get('Origin')
            if origin and origin not in ('http://'+host,):
                return json_reply({'error':'请求来源无效。'},403)
            if not secrets.compare_digest(headers.get('X-Tool-Token',''),self.token):
                return json_reply({'error':'页面已失效，请刷新后重试。'},403)
        try:
            if method=='GET' and path=='/api/ping':
                return json_reply({'tool':'pdf_compare_web','root':str(self.root),'data_root':str(self.data_root)})
            if method=='GET' and path=='/api/config':
                return json_reply({'defaults':DEFAULTS,'field_names':FIELD_NAMES,'token':self.token,'max_file_mb':200})
            if method=='POST' and path=='/api/upload':
                document = self.workspace.register_document(stream,int(headers.get('Content-Length','0')),query.get('name',['document.pdf'])[0])
                return json_reply(document)
            if method=='POST':
                length = int(headers.get('Content-Length','0'))
                if not 0 < length <= 512*1024:
                    raise ValueError('请求大小无效。')
                data = json.loads(stream.read(length))
                if not isinstance(data,dict):
                    raise ValueError('请求内容必须为 JSON 对象。')
                if path=='/api/extract':
                    c = checked_config(data.get('config',{}))
                    a,b = str(data.get('a_id','')),str(data.get('b_id',''))
                    self.workspace.document(a)
                    self.workspace.document(b)
                    identifier = self.workspace.submit(lambda update:self.workspace.extract(a,b,c,update))
                    return json_reply({'job_id':identifier},202)
                if path=='/api/compare':
                    run_id = str(data.get('run_id',''))
                    self.workspace.run(run_id)
                    ignore = data.get('ignore_whitespace',False)
                    if not isinstance(ignore,bool):
                        raise ValueError('忽略空白设置无效。')
                    identifier = self.workspace.submit(lambda update:self.workspace.compare(run_id,ignore,update))
                    return json_reply({'job_id':identifier},202)
                if path=='/api/open-folder':
                    run = self.workspace.run(str(data.get('run_id','')))
                    folder = str(run['path'])
                    if sys.platform=='win32':
                        os.startfile(folder)
                    elif sys.platform=='darwin':
                        subprocess.Popen(['open',folder])
                    else:
                        subprocess.Popen(['xdg-open',folder],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    return json_reply({'ok':True})
            if method=='GET' and path.startswith('/api/jobs/'):
                identifier = path.rsplit('/',1)[-1]
                with self.workspace.lock:
                    item = self.workspace.jobs.get(identifier)
                    return json_reply(dict(item)) if item else json_reply({'error':'任务不存在。'},404)
            if method=='GET' and path.startswith('/api/runs/'):
                run = self.workspace.run(path.rsplit('/',1)[-1])
                return json_reply({k:v for k,v in run.items() if k!='path'})
            if method=='GET' and path.startswith('/api/pdf/'):
                document = self.workspace.document(path.rsplit('/',1)[-1])
                return self.file(document['path'],'application/pdf')
            if method=='GET' and path.startswith('/api/preview/'):
                parts=path.strip('/').split('/')
                if len(parts)!=4:
                    raise ValueError('页码无效。')
                document = self.workspace.document(parts[2])
                number = int(parts[3])
                if not 1 <= number <= document['page_count']:
                    raise ValueError('页码超出范围。')
                return self.preview(document,number)
            if method=='GET' and path.startswith('/results/'):
                parts=path.strip('/').split('/')
                if len(parts)<3:
                    raise ValueError('结果路径无效。')
                run = self.workspace.run(parts[1])
                root = run['path'].resolve()
                f = root.joinpath(*parts[2:]).resolve()
                if not f.is_relative_to(root):
                    return json_reply({'error':'结果路径无效。'},403)
                return self.file(f,download='download' in query)
            if method=='GET' and path in ('/','/index.html','/app.js','/style.css'):
                return self.file(self.root/'static'/('index.html' if path=='/' else path.lstrip('/')))
            return json_reply({'error':'页面不存在。'},404)
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
            return json_reply({'error':str(exc)},400)
        except OSError as exc:
            return json_reply({'error':'无法读写文件：'+str(exc)},400)
        except Exception as exc:
            return json_reply({'error':'处理失败：'+str(exc)},500)

    @staticmethod
    def file(path: Path, content_type: str|None=None, download=False) -> Reply:
        if not path.is_file():
            return json_reply({'error':'文件不存在。'},404)
        content_type = content_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if content_type.startswith('text/') or content_type in ('application/javascript','application/json'):
            content_type += '; charset=utf-8'
        # Prevent browsers from treating CSV responses as HTML.
        headers = {'Content-Disposition':'attachment'} if download or path.suffix in ('.csv','.zip') else {}
        return Reply(200,path.read_bytes(),content_type,headers)

    def preview(self,document,number):
        import pypdfium2 as pdfium
        with self.render_lock:
            pdf = pdfium.PdfDocument(document['path'])
            try:
                page = pdf[number-1]
                try:
                    # PDFium renders CropBox intersected with MediaBox and
                    # applies page rotation. Extraction/registration use the
                    # same visible rectangle; margin values are relative to it.
                    width,height = page.get_size()
                    bitmap=page.render(scale=min(1.25,1100/width))
                    try:
                        image=bitmap.to_pil()
                        out=io.BytesIO()
                        image.save(out,format='PNG')
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                pdf.close()
        return Reply(200,out.getvalue(),'image/png',{'X-Page-Height':str(height),'X-Page-Width':str(width)})


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,app):
        self.app=app
        super().__init__(address,Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def do_GET(self):
        self.respond('GET')
    def do_POST(self):
        self.respond('POST')
    def respond(self,method):
        reply=self.server.app.route(method,self.path,self.headers,self.rfile)
        self.send_response(reply.status)
        self.send_header('Content-Type',reply.content_type)
        self.send_header('Content-Length',str(len(reply.body)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','same-origin')
        for k,v in reply.headers.items():
            self.send_header(k,v)
        self.end_headers()
        try:
            self.wfile.write(reply.body)
        except (BrokenPipeError,ConnectionResetError):
            pass
    def log_message(self,format,*args):
        # Avoid one console entry for each progress poll.
        pass
