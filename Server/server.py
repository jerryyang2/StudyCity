#!/usr/bin/env python3
"""可執行的開發 API；預設僅監聽本機，部署需 TLS 與主機設定。"""
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
import argparse
import json
import re
import ssl
import threading
import time
from urllib.parse import urlsplit

from service import APIError, Service


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self,address,service,secure_cookie=False,allowed_hosts=None,allowed_origins=None):
        self.service=service; self.attempts=defaultdict(deque); self.limiter_lock=threading.Lock()
        self.secure_cookie=secure_cookie
        self.allowed_hosts=set(allowed_hosts or [])|{"localhost","127.0.0.1","::1"}
        self.allowed_origins=set(allowed_origins or [])
        super().__init__(address,Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "StudyCity"
    sys_version = ""
    def setup(self):
        super().setup(); self.connection.settimeout(20)

    def log_message(self,*args):
        pass  # 不輸出帳號、路徑、內容、權杖或原始錯誤。

    def do_GET(self): self.handle_api()
    def do_POST(self): self.handle_api()
    def do_OPTIONS(self):
        if self.origin_allowed(): self.reply(204,{})
        else: self.reply(403,{"message":"這個網站尚未獲服務允許。"})

    def own_origin(self):
        return ("https" if self.server.secure_cookie else "http")+"://"+self.headers.get("Host","")

    def origin_allowed(self):
        origin=self.headers.get("Origin")
        return origin==self.own_origin() or origin in self.server.allowed_origins

    def cookie(self,token,remember=False,clear=False):
        value="studycity_session="+token+"; HttpOnly; SameSite=Strict; Path=/"
        if clear: value+="; Max-Age=0"
        elif remember: value+="; Max-Age=604800"
        if self.server.secure_cookie: value+="; Secure"
        return value

    def static(self,path):
        allowed={"/":"index.html","/index.html":"index.html","/app.js":"app.js","/config.js":"config.js",
                 "/styles.css":"styles.css","/favicon.svg":"favicon.svg","/favicon.ico":"favicon.svg"}
        if path not in allowed: return False
        root=Path(__file__).resolve().parents[1]
        web=root/"Web" if (root/"Web/index.html").is_file() else root/"docs"
        file=web/allowed[path]
        if not file.is_file(): raise APIError(404,"網站資源尚未準備完成。")
        data=file.read_bytes()
        types={".html":"text/html; charset=utf-8",".js":"text/javascript; charset=utf-8",".css":"text/css; charset=utf-8",".svg":"image/svg+xml"}
        self.send_response(200); self.send_header("Content-Type",types[file.suffix])
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-src https://docs.google.com; connect-src 'self' https: http://localhost:* http://127.0.0.1:*; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
        return True

    def handle_api(self):
        try:
            path=self.path.split("?",1)[0]
            host=urlsplit("//"+self.headers.get("Host","")).hostname
            if host not in self.server.allowed_hosts: raise APIError(400,"這個主機名稱尚未設定。")
            if len(path)>300: raise APIError(414,"請求路徑過長。")
            if self.command=="GET" and path=="/health": return self.reply(200,{"ok":True,"version":1,"service":"StudyCity"})
            if self.command=="GET" and self.static(path): return
            web_mode=self.headers.get("X-StudyCity-Web","")
            if self.headers.get("Origin") and not self.origin_allowed(): raise APIError(403,"這個網站尚未獲服務允許。")
            if web_mode and self.command=="POST" and not self.origin_allowed(): raise APIError(403,"網頁來源驗證未通過，請重新開啟教室。")
            data={}
            if self.command=="POST":
                if self.headers.get("Transfer-Encoding"): raise APIError(400,"不支援的請求格式。")
                try: length=int(self.headers.get("Content-Length","0"))
                except ValueError: raise APIError(400,"請求長度格式不正確。") from None
                if not 0<length<=32768: raise APIError(413,"請求內容大小不符。")
                try: data=json.loads(self.rfile.read(length))
                except (ValueError,UnicodeError): raise APIError(400,"JSON 格式不正確。") from None
                if not isinstance(data,dict): raise APIError(400,"請使用 JSON 物件。")
            service=self.server.service
            if path in ["/auth/register","/auth/login"] and self.command=="POST":
                now=time.monotonic()
                with self.server.limiter_lock:
                    attempts=self.server.attempts[self.client_address[0]]
                    while attempts and attempts[0]<now-60: attempts.popleft()
                    if len(attempts)>=20: raise APIError(429,"嘗試次數較多，請一分鐘後再試。")
                    attempts.append(now)
                result=service.register(data) if path.endswith("register") else service.login(data)
                if web_mode and web_mode!="token":
                    token=result.pop("token")
                    return self.reply(200,result,[("Set-Cookie",self.cookie(token,remember=data.get("remember") is True))])
                return self.reply(200,result)
            authorization=self.headers.get("Authorization","")
            if authorization.startswith("Bearer "): token=authorization[7:]
            else:
                cookies=SimpleCookie()
                try: cookies.load(self.headers.get("Cookie",""))
                except Exception: pass
                item=cookies.get("studycity_session")
                if not item: raise APIError(401,"請先登入。")
                if self.command=="POST" and self.headers.get("Origin")!=self.own_origin():
                    raise APIError(403,"網頁來源驗證未通過，請重新開啟教室。")
                token=item.value
            user=service.authenticate(token)
            if self.command=="GET":
                if path=="/me": return self.reply(200,service.overview(user))
                if match:=re.fullmatch(r"/groups/([\w-]+)",path): return self.reply(200,service.detail(user,match[1]))
            if self.command=="POST":
                if path=="/auth/logout": return self.reply(200,service.logout(token),[("Set-Cookie",self.cookie("",clear=True))])
                if path=="/auth/delete": return self.reply(200,service.delete_account(user,data),[("Set-Cookie",self.cookie("",clear=True))])
                if path=="/groups": return self.reply(200,service.create_group(user,data))
                if path=="/groups/join": return self.reply(200,service.join(user,data))
                if match:=re.fullmatch(r"/groups/([\w-]+)/(tasks|products|preferences|leave)",path):
                    action={"tasks":service.create_task,"products":service.create_product,"preferences":service.preferences}
                    return self.reply(200,service.leave(user,match[1]) if match[2]=="leave" else action[match[2]](user,match[1],data))
                if match:=re.fullmatch(r"/tasks/([\w-]+)/submit",path): return self.reply(200,service.submit(user,match[1]))
                if match:=re.fullmatch(r"/submissions/([\w-]+)/review",path): return self.reply(200,service.review(user,match[1],data.get("approved")))
                if match:=re.fullmatch(r"/products/([\w-]+)/redeem",path): return self.reply(200,service.redeem(user,match[1],data))
                if match:=re.fullmatch(r"/products/([\w-]+)/update",path): return self.reply(200,service.update_product(user,match[1],data))
                if match:=re.fullmatch(r"/redemptions/([\w-]+)/fulfill",path): return self.reply(200,service.fulfill(user,match[1]))
                if match:=re.fullmatch(r"/(tasks|products)/([\w-]+)/archive",path): return self.reply(200,service.archive(user,"task" if match[1]=="tasks" else "product",match[2]))
            raise APIError(404,"找不到這個功能。")
        except APIError as error: self.reply(error.status,{"message":error.message})
        except Exception: self.reply(500,{"message":"服務暫時未能完成操作，請重新整理確認狀態後再試。"})

    def reply(self,status,data,headers=None):
        encoded=b"" if status==204 else json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(encoded)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Connection","close")
        origin=self.headers.get("Origin")
        if origin and self.origin_allowed():
            self.send_header("Access-Control-Allow-Origin",origin)
            self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers","Content-Type, Authorization, X-StudyCity-Web")
            self.send_header("Vary","Origin")
        for name,value in headers or []: self.send_header(name,value)
        self.end_headers()
        try: self.wfile.write(encoded)
        except (BrokenPipeError,ConnectionResetError): pass


if __name__=="__main__":
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8787)
    parser.add_argument("--database",type=Path,default=root/"LocalOnly/server.sqlite")
    parser.add_argument("--cert",type=Path); parser.add_argument("--key",type=Path)
    parser.add_argument("--external-https",action="store_true",help="僅供已由自己的 HTTPS 反向代理保護的服務")
    parser.add_argument("--allowed-host",action="append",default=[])
    parser.add_argument("--allowed-origin",action="append",default=[])
    args=parser.parse_args()
    if bool(args.cert)!=bool(args.key): parser.error("--cert 與 --key 必須一起提供")
    server=Server((args.host,args.port),Service(args.database),secure_cookie=bool(args.cert) or args.external_https,
                  allowed_hosts=args.allowed_host,allowed_origins=args.allowed_origin)
    if args.cert:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert,args.key)
        server.socket=context.wrap_socket(server.socket,server_side=True)
    print("StudyCity API ready; request logging disabled.")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
