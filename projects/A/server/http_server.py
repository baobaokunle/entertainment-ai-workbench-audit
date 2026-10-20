"""Loopback-only HTTP boundary with exact Host/Origin and a finite static tree."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import json
from .api import API, MAX_BODY
from .settings import PORT

HOSTS = {f'127.0.0.1:{PORT}',f'localhost:{PORT}'}
WEB_ORIGINS = {f'http://{host}' for host in HOSTS}
ALLOWED_HEADERS = {'authorization','content-type','x-dsh-schema'}
STATIC = {'/js/operations.js':'js/operations.js','/js/birthday_candidates.js':'js/birthday_candidates.js','/':'index.html','/index.html':'index.html','/styles.css':'styles.css',
          '/workspace.html':'workspace.html','/workspace.css':'workspace.css','/js/workspace.js':'js/workspace.js',
          '/js/api.js':'js/api.js','/js/history.js':'js/history.js','/js/main.js':'js/main.js','/js/cd.js':'js/cd.js','/js/live.js':'js/live.js','/js/groups.js':'js/groups.js','/js/ai.js':'js/ai.js','/js/candidate_review.js':'js/candidate_review.js','/js/import_binding.js':'js/import_binding.js','/js/file_import.js':'js/file_import.js','/js/activity.js':'js/activity.js','/js/deletion.js':'js/deletion.js','/js/diagnostics.js':'js/diagnostics.js'}

def make_handler(app, *, api=None, serve_ui=True):
    api=API(app) if api is None else api
    webroot=Path(__file__).resolve().parents[1]/'web'
    origins = WEB_ORIGINS | {'chrome-extension://'+x for x in app.config['extension_ids']}
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        server_version='DSH-Candidate'; sys_version=''
        # Idle request lines/headers must not hold normal shutdown forever.
        timeout=10
        def log_message(self,*args): pass
        def boundary(self):
            if len(self.headers.get_all('Host',[]))!=1 or self.headers.get('Host') not in HOSTS:
                self.respond(403,{'error':'host_rejected'}); return False
            if self.headers.get_all('Origin') and len(self.headers.get_all('Origin'))!=1:
                self.respond(403,{'error':'origin_rejected'}); return False
            if any(len(self.headers.get_all(x,[]))>1 for x in ('Authorization','Content-Type','X-DSH-Schema')):
                self.respond(400,{'error':'duplicate_header'}); return False
            origin=self.headers.get('Origin')
            if origin is not None and origin not in origins:
                self.respond(403,{'error':'origin_rejected'}); return False
            if self.headers.get('Sec-Fetch-Site')=='cross-site' and origin not in origins:
                self.respond(403,{'error':'cross_site_rejected'}); return False
            if self.client_address[0] not in ('127.0.0.1','::1'):
                self.respond(403,{'error':'loopback_required'}); return False
            return True
        def respond(self,status,value,*,raw=None,content_type='application/json; charset=utf-8'):
            body=raw if raw is not None else json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',content_type); self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            origin=self.headers.get('Origin')
            if origin in origins:
                self.send_header('Access-Control-Allow-Origin',origin); self.send_header('Vary','Origin')
            self.send_header('Connection','close'); self.end_headers(); self.wfile.write(body); self.close_connection=True
        def do_OPTIONS(self):
            if not self.boundary(): return
            method=self.headers.get('Access-Control-Request-Method')
            requested={x.strip().lower() for x in self.headers.get('Access-Control-Request-Headers','').split(',') if x.strip()}
            if method not in ('GET','POST') or not requested<=ALLOWED_HEADERS:
                self.respond(403,{'error':'preflight_rejected'}); return
            self.send_response(204); self.send_header('Content-Length','0')
            self.send_header('Access-Control-Allow-Origin',self.headers.get('Origin',''))
            self.send_header('Access-Control-Allow-Methods','GET, POST')
            self.send_header('Access-Control-Allow-Headers','Authorization, Content-Type, X-DSH-Schema')
            self.send_header('Vary','Origin'); self.end_headers()
        def do_GET(self):
            if not self.boundary(): return
            if serve_ui and self.path in STATIC:
                target=webroot/STATIC[self.path]
                if not target.is_file(): self.respond(503,{'error':'ui_unavailable'}); return
                mime='text/html; charset=utf-8' if target.suffix=='.html' else 'text/css; charset=utf-8' if target.suffix=='.css' else 'text/javascript; charset=utf-8'
                self.respond(200,None,raw=target.read_bytes(),content_type=mime); return
            status,value=api.handle('GET',self.path,self.headers); self.respond(status,value)
        def do_POST(self):
            if not self.boundary(): return
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
                self.respond(400,{'error':'length_required'}); return
            try: length=int(self.headers['Content-Length'])
            except ValueError: self.respond(400,{'error':'invalid_length'}); return
            if not 0 <= length <= MAX_BODY: self.respond(413,{'error':'request_too_large'}); return
            self.connection.settimeout(10)
            try: body=self.rfile.read(length)
            except TimeoutError: self.respond(408,{'error':'request_timeout'}); return
            if len(body)!=length: self.respond(400,{'error':'incomplete_body'}); return
            status,value=api.handle('POST',self.path,self.headers,body); self.respond(status,value)
    return Handler

def create_server(app, *, port=PORT, api=None, serve_ui=True):
    if port != PORT: raise ValueError('only fixed candidate port allowed')
    server=ThreadingHTTPServer(('127.0.0.1',PORT),make_handler(app,api=api,serve_ui=serve_ui))
    # Every request may access the app again after its first database read.
    # server_close must drain those requests before the caller closes the app.
    server.daemon_threads=False
    return server
