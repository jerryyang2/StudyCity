"""StudyCity 多人領域服務；SQLite 交易保證審核與扣款防重。"""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def identifier():
    return str(uuid.uuid4())


def text(value, limit=80):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise APIError(400, "文字欄位長度不符，請確認後重試。")
    return value.strip()


def number(value, maximum=1000):
    if type(value) is not int or not 1 <= value <= maximum:
        raise APIError(400, "積分數值超出可用範圍。")
    return value


def note_text(value, limit=1000):
    if not isinstance(value,str) or len(value.strip())>limit or any(ord(c)<32 and c not in '\n\t' for c in value):
        raise APIError(400,"說明與回饋請填寫 1000 個字元以內的文字。")
    return value.strip()


class Service:
    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, alias TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('student','teacher','parent')),
              salt BLOB NOT NULL, password BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS groups (
              id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
              kind TEXT NOT NULL, invite TEXT UNIQUE NOT NULL, leaderboard INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS members (
              group_id TEXT NOT NULL REFERENCES groups(id), user_id TEXT NOT NULL REFERENCES users(id),
              ranking INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(group_id,user_id));
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), title TEXT NOT NULL,
              points INTEGER NOT NULL CHECK(points>0), due_at REAL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS submissions (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
              user_id TEXT NOT NULL REFERENCES users(id), status TEXT NOT NULL,
              updated_at REAL NOT NULL, UNIQUE(task_id,user_id));
            CREATE TABLE IF NOT EXISTS products (
              id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), title TEXT NOT NULL,
              price INTEGER NOT NULL CHECK(price>0), active INTEGER NOT NULL DEFAULT 1,
              description TEXT NOT NULL DEFAULT '', stock INTEGER);
            CREATE TABLE IF NOT EXISTS ledger (
              id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id),
              user_id TEXT NOT NULL REFERENCES users(id), amount INTEGER NOT NULL, label TEXT NOT NULL,
              source TEXT UNIQUE NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS redemptions (
              id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id),
              user_id TEXT NOT NULL REFERENCES users(id), product_id TEXT NOT NULL REFERENCES products(id),
              title TEXT NOT NULL, price INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              created_at REAL NOT NULL, request_id TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', UNIQUE(user_id,request_id));
            CREATE INDEX IF NOT EXISTS ledger_wallet ON ledger(group_id,user_id);
            CREATE INDEX IF NOT EXISTS tasks_group ON tasks(group_id);
            CREATE TABLE IF NOT EXISTS submission_events (
              id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id),
              revision INTEGER NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS submission_events_parent ON submission_events(submission_id);
            """)
            for table,column,definition in [("products","description","TEXT NOT NULL DEFAULT ''"),("products","stock","INTEGER"),("redemptions","description","TEXT NOT NULL DEFAULT ''"),
                                            ("submissions","note","TEXT NOT NULL DEFAULT ''"),("submissions","feedback","TEXT NOT NULL DEFAULT ''"),
                                            ("submissions","revision","INTEGER NOT NULL DEFAULT 1")]:
                columns={row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                if column not in columns: db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.database.chmod(0o600)

    @contextmanager
    def connection(self, transaction=False):
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if transaction: db.execute("BEGIN IMMEDIATE")
            yield db
            if transaction: db.commit()
        except Exception:
            if transaction: db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def public_user(row):
        return {key: row[key] for key in ["id", "username", "alias", "role"]}

    @staticmethod
    def password_hash(password, salt):
        # 明確使用同一演算法，避免不同電腦的 OpenSSL 編譯選項改變驗證結果。
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000, dklen=32)

    def register(self, data):
        return self._register(data,allow_teacher=False)

    def provision_teacher(self,data):
        """只供伺服器管理者 CLI 使用；HTTP 不提供此入口。"""
        return self._register({**data,"role":"teacher"},allow_teacher=True)

    def _register(self, data, allow_teacher):
        username = text(data.get("username"), 32).lower()
        if not re.fullmatch(r"[a-z0-9_]{3,32}", username): raise APIError(400, "帳號需為 3 到 32 個小寫英文字母、數字或底線。")
        alias = text(data.get("alias"), 30)
        role = data.get("role")
        if role not in ["student", "teacher", "parent"]: raise APIError(400, "請選擇有效的身分。")
        if role=="teacher" and not allow_teacher: raise APIError(403,"老師帳號需先開啟申請 ticket，由管理者確認並開通後再登入。")
        password = data.get("password")
        if not isinstance(password, str) or not 10 <= len(password) <= 128: raise APIError(400, "密碼需為 10 到 128 個字元。")
        salt = secrets.token_bytes(16)
        hashed = self.password_hash(password, salt)
        try:
            with self.connection(True) as db:
                db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (identifier(), username, alias, role, salt, hashed))
        except sqlite3.IntegrityError:
            raise APIError(409, "這個帳號已被使用，請換一個。") from None
        return self.login({"username": username, "password": password})

    def login(self, data):
        username = data.get("username", "")
        password = data.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
            raise APIError(401, "帳號或密碼不正確。")
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username.lower(),)).fetchone()
            salt = row["salt"] if row else b"\0" * 16
            candidate = self.password_hash(password, salt)
            if not row or not hmac.compare_digest(candidate, row["password"]): raise APIError(401, "帳號或密碼不正確。")
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), row["id"], time.time()+7*86400))
            return {"token": token, "user": self.public_user(row)}

    def authenticate(self, token):
        if not isinstance(token, str) or len(token) > 256: raise APIError(401, "請重新登入。")
        with self.connection() as db:
            row = db.execute("SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            if not row: raise APIError(401, "登入已失效，請重新登入。")
            return self.public_user(row)

    def logout(self, token):
        with self.connection(True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        return {"ok": True}

    @staticmethod
    def access(db, user, group_id, owner=False):
        group = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not group: raise APIError(404, "找不到群組。")
        if group["owner_id"] == user["id"]: return group
        if owner or not db.execute("SELECT 1 FROM members WHERE group_id=? AND user_id=?", (group_id,user["id"])).fetchone():
            raise APIError(403, "你無法操作這個群組。")
        return group

    def overview(self, user):
        with self.connection() as db:
            rows = db.execute("SELECT DISTINCT g.id,g.name,g.kind,g.owner_id,(SELECT COUNT(*) FROM members x WHERE x.group_id=g.id) AS member_count,(SELECT COUNT(*) FROM tasks t WHERE t.group_id=g.id AND t.active=1) AS task_count FROM groups g LEFT JOIN members m ON m.group_id=g.id WHERE g.owner_id=? OR m.user_id=? ORDER BY g.name", (user["id"],user["id"])).fetchall()
            return {"user": user, "groups": [dict(row) for row in rows]}

    def create_group(self, user, data):
        if user["role"] not in ["teacher","parent"]: raise APIError(403, "學生透過邀請碼加入群組。")
        name = text(data.get("name"), 50); group_id = identifier()
        with self.connection(True) as db:
            db.execute("INSERT INTO groups(id,owner_id,name,kind,invite) VALUES (?,?,?,?,?)",
                       (group_id,user["id"],name,"classroom" if user["role"] == "teacher" else "family",secrets.token_urlsafe(12)))
        return {"id": group_id}

    def join(self, user, data):
        if user["role"] != "student": raise APIError(403, "只有學生帳號可使用邀請碼加入。")
        invite = text(data.get("invite"), 100)
        with self.connection(True) as db:
            group = db.execute("SELECT * FROM groups WHERE invite=?", (invite,)).fetchone()
            if not group: raise APIError(404, "邀請碼無效，請向老師或家長確認。")
            db.execute("INSERT OR IGNORE INTO members(group_id,user_id) VALUES (?,?)", (group["id"],user["id"]))
            return {"id": group["id"]}

    @staticmethod
    def balance(db, group_id, user_id):
        return db.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE group_id=? AND user_id=?", (group_id,user_id)).fetchone()[0]

    def detail(self, user, group_id):
        with self.connection() as db:
            group = self.access(db,user,group_id); owner = group["owner_id"] == user["id"]
            public_group = {key: group[key] for key in ["id","name","kind","owner_id"]}
            public_group["leaderboard"] = bool(group["leaderboard"])
            public_group["invite"] = group["invite"] if owner else None
            members = db.execute("SELECT u.id,u.alias,m.ranking FROM members m JOIN users u ON u.id=m.user_id WHERE m.group_id=?" + ("" if owner else " AND m.user_id=?"),
                                 (group_id,) if owner else (group_id,user["id"])).fetchall()
            tasks = db.execute("SELECT t.id,t.title,t.points,t.due_at,COALESCE(s.status,'open') AS status,s.id AS submission_id,s.note,s.feedback,s.revision FROM tasks t LEFT JOIN submissions s ON s.task_id=t.id AND s.user_id=? WHERE t.group_id=? AND t.active=1 ORDER BY t.rowid DESC", (user["id"],group_id)).fetchall()
            submissions = db.execute("SELECT s.id,s.task_id,s.status,s.updated_at,s.note,s.feedback,s.revision,t.title,t.points,u.alias FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN users u ON u.id=s.user_id WHERE t.group_id=?" + ("" if owner else " AND s.user_id=?") + " ORDER BY s.updated_at DESC", (group_id,) if owner else (group_id,user["id"])).fetchall()
            products = db.execute("SELECT id,title,price,description,stock,active FROM products WHERE group_id=?" + ("" if owner else " AND active=1") + " ORDER BY active DESC,price,title", (group_id,)).fetchall()
            ledger = db.execute("SELECT id,amount,label,created_at FROM ledger WHERE group_id=? AND user_id=? ORDER BY created_at DESC LIMIT 200", (group_id,user["id"])).fetchall()
            redemptions = db.execute("SELECT r.id,r.title,r.price,r.description,r.status,r.created_at,u.alias FROM redemptions r JOIN users u ON u.id=r.user_id WHERE r.group_id=?" + ("" if owner else " AND r.user_id=?") + " ORDER BY r.created_at DESC LIMIT 200", (group_id,) if owner else (group_id,user["id"])).fetchall()
            ranking = []
            if group["leaderboard"]:
                ranking = [dict(row) for row in db.execute("SELECT u.alias,COALESCE(SUM(CASE WHEN l.amount>0 THEN l.amount ELSE 0 END),0) AS points FROM members m JOIN users u ON u.id=m.user_id LEFT JOIN ledger l ON l.group_id=m.group_id AND l.user_id=m.user_id WHERE m.group_id=? AND m.ranking=1 GROUP BY u.id ORDER BY points DESC,u.alias LIMIT 100", (group_id,)).fetchall()]
            return {"capabilities":["submission_feedback_v1"],"group": public_group, "members": [dict(r) for r in members], "tasks": [dict(r) for r in tasks],
                    "submissions": [dict(r) for r in submissions], "products": [dict(r) for r in products],
                    "wallet": self.balance(db,group_id,user["id"]), "ledger": [dict(r) for r in ledger],
                    "redemptions": [dict(r) for r in redemptions], "ranking": ranking}

    def create_task(self, user, group_id, data):
        title, points = text(data.get("title")), number(data.get("points"))
        due = data.get("due_at")
        if due is not None and (type(due) not in [int,float] or not 0 < due < 253402300799): raise APIError(400,"截止時間格式不正確。")
        with self.connection(True) as db:
            self.access(db,user,group_id,True)
            task_id=identifier(); db.execute("INSERT INTO tasks(id,group_id,title,points,due_at) VALUES (?,?,?,?,?)", (task_id,group_id,title,points,due))
            return {"id": task_id}

    def submit(self, user, task_id, data=None):
        if user["role"] != "student": raise APIError(403,"只有學生可以提交任務。")
        data={} if data is None else data
        note=note_text(data.get("note","")); expected=data.get("revision")
        if expected is not None and (type(expected) is not int or expected<0): raise APIError(400,"提交版本格式不正確。")
        with self.connection(True) as db:
            task=db.execute("SELECT * FROM tasks WHERE id=? AND active=1", (task_id,)).fetchone()
            if not task: raise APIError(404,"找不到可提交的任務。")
            self.access(db,user,task["group_id"])
            existing=db.execute("SELECT * FROM submissions WHERE task_id=? AND user_id=?", (task_id,user["id"])).fetchone()
            if existing:
                if expected is None and (existing["note"] or existing["revision"]>1):
                    raise APIError(409,"這份提交含新版說明或已調整過，請更新 App／網頁並重新整理後再提交。")
                if existing["status"] != "rejected":
                    if existing["note"]!=note and expected is not None: raise APIError(409,"這份提交已送出，請重新整理確認目前狀態。")
                    return {"id":existing["id"]}
                if expected is not None and expected!=existing["revision"]: raise APIError(409,"提交內容已有更新，請重新整理後再調整。")
                revision=existing["revision"]+1; now=time.time()
                db.execute("UPDATE submissions SET status='pending',note=?,revision=?,updated_at=? WHERE id=?",(note,revision,now,existing["id"]))
                db.execute("INSERT INTO submission_events VALUES (?,?,?,?,?,?)",(identifier(),existing["id"],revision,"submitted",note,now))
                return {"id":existing["id"]}
            if expected not in [None,0]: raise APIError(409,"找不到這個版本的提交，請重新整理。")
            submission_id=identifier()
            now=time.time()
            db.execute("INSERT INTO submissions(id,task_id,user_id,status,updated_at,note) VALUES (?,?,?,?,?,?)", (submission_id,task_id,user["id"],"pending",now,note))
            db.execute("INSERT INTO submission_events VALUES (?,?,?,?,?,?)",(identifier(),submission_id,1,"submitted",note,now))
            return {"id":submission_id}

    def review(self, user, submission_id, approved, feedback=None, revision=None):
        if type(approved) is not bool: raise APIError(400,"請選擇審核結果。")
        message=note_text("" if feedback is None else feedback)
        if feedback is not None and not approved and not message: raise APIError(400,"請留下具體的調整建議。")
        if revision is not None and (type(revision) is not int or revision<1): raise APIError(400,"審核版本格式不正確。")
        with self.connection(True) as db:
            row=db.execute("SELECT s.*,t.group_id,t.points FROM submissions s JOIN tasks t ON s.task_id=t.id WHERE s.id=?", (submission_id,)).fetchone()
            if not row: raise APIError(404,"找不到提交紀錄。")
            self.access(db,user,row["group_id"],True)
            if revision is None and (row["note"] or row["revision"]>1):
                raise APIError(409,"這份提交含新版說明或已調整過，請更新 App／網頁並查看內容後再確認。")
            if revision is not None and revision!=row["revision"]: raise APIError(409,"學生已更新提交，請重新整理並查看新內容後再確認。")
            if row["status"] == "approved": return {"ok":True}
            if row["status"] == "rejected" and not approved and row["feedback"]==message: return {"ok":True}
            if row["status"] != "pending": raise APIError(409,"請等待學生重新提交。")
            kind="approved" if approved else "rejected"; now=time.time()
            db.execute("UPDATE submissions SET status=?,feedback=?,updated_at=? WHERE id=?", (kind,message,now,submission_id))
            db.execute("INSERT INTO submission_events VALUES (?,?,?,?,?,?)",(identifier(),submission_id,row["revision"],kind,message,now))
            if approved:
                db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)", (identifier(),row["group_id"],row["user_id"],row["points"],"任務審核通過","submission:"+submission_id,time.time()))
        return {"ok":True}

    def history(self,user,submission_id):
        with self.connection() as db:
            row=db.execute("SELECT s.user_id,t.group_id,g.owner_id FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN groups g ON g.id=t.group_id WHERE s.id=?",(submission_id,)).fetchone()
            if not row: raise APIError(404,"找不到提交紀錄。")
            self.access(db,user,row["group_id"])
            if user["id"] not in [row["user_id"],row["owner_id"]]: raise APIError(403,"無法查看其他人的提交與回饋。")
            total=db.execute("SELECT COUNT(*) FROM submission_events WHERE submission_id=?",(submission_id,)).fetchone()[0]
            events=db.execute("SELECT id,revision,kind,note,created_at FROM submission_events WHERE submission_id=? ORDER BY rowid DESC LIMIT 50",(submission_id,)).fetchall()
            return {"submission_id":submission_id,"total":total,"events":[dict(e) for e in reversed(events)]}

    def create_product(self,user,group_id,data):
        title,price,description,stock,active=self.product_values(data)
        with self.connection(True) as db:
            self.access(db,user,group_id,True)
            product_id=identifier(); db.execute("INSERT INTO products(id,group_id,title,price,description,stock,active) VALUES (?,?,?,?,?,?,?)", (product_id,group_id,title,price,description,stock,int(active)))
        return {"id":product_id}

    @staticmethod
    def product_values(data):
        title,price=text(data.get("title")),number(data.get("price"),10000)
        description=data.get("description","")
        if not isinstance(description,str) or len(description)>1000: raise APIError(400,"商品說明請控制在 1000 字以內。")
        stock=data.get("stock")
        if stock is not None and (type(stock) is not int or not 0<=stock<=1_000_000): raise APIError(400,"庫存請填 0 到 100 萬，或留空表示不限量。")
        active=data.get("active",True)
        if type(active) is not bool: raise APIError(400,"上架設定格式不正確。")
        return title,price,description.strip(),stock,active

    def update_product(self,user,product_id,data):
        title,price,description,stock,active=self.product_values(data)
        with self.connection(True) as db:
            product=db.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
            if not product: raise APIError(404,"找不到商品。")
            self.access(db,user,product["group_id"],True)
            db.execute("UPDATE products SET title=?,price=?,description=?,stock=?,active=? WHERE id=?",(title,price,description,stock,int(active),product_id))
        return {"ok":True}

    def redeem(self,user,product_id,data):
        if user["role"] != "student": raise APIError(403,"只有學生可以兌換獎勵。")
        request_id=text(data.get("request_id"),80)
        with self.connection(True) as db:
            product=db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if not product: raise APIError(404,"找不到可兌換的獎勵。")
            self.access(db,user,product["group_id"])
            existing=db.execute("SELECT id,product_id FROM redemptions WHERE user_id=? AND request_id=?", (user["id"],request_id)).fetchone()
            if existing:
                if existing["product_id"] != product_id: raise APIError(409,"請重新發起這次兌換。")
                return {"id":existing["id"]}
            if not product["active"]: raise APIError(404,"這項獎勵已下架。")
            if product["stock"] is not None and product["stock"]<=0: raise APIError(409,"這項獎勵已兌換完畢，請等老師補充庫存。")
            if self.balance(db,product["group_id"],user["id"]) < product["price"]: raise APIError(409,"群組積分不足。")
            redemption_id=identifier()
            db.execute("INSERT INTO redemptions(id,group_id,user_id,product_id,title,price,description,created_at,request_id) VALUES (?,?,?,?,?,?,?,?,?)",
                       (redemption_id,product["group_id"],user["id"],product_id,product["title"],product["price"],product["description"],time.time(),request_id))
            db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)", (identifier(),product["group_id"],user["id"],-product["price"],"兌換群組獎勵","redemption:"+redemption_id,time.time()))
            if product["stock"] is not None: db.execute("UPDATE products SET stock=stock-1 WHERE id=?",(product_id,))
        return {"id":redemption_id}

    def fulfill(self,user,redemption_id):
        with self.connection(True) as db:
            row=db.execute("SELECT * FROM redemptions WHERE id=?", (redemption_id,)).fetchone()
            if not row: raise APIError(404,"找不到兌換紀錄。")
            self.access(db,user,row["group_id"],True)
            db.execute("UPDATE redemptions SET status='fulfilled' WHERE id=?", (redemption_id,))
        return {"ok":True}

    def preferences(self,user,group_id,data):
        with self.connection(True) as db:
            group=self.access(db,user,group_id)
            if group["owner_id"] == user["id"]:
                if "leaderboard" in data:
                    if type(data["leaderboard"]) is not bool: raise APIError(400,"設定格式不正確。")
                    db.execute("UPDATE groups SET leaderboard=? WHERE id=?", (int(data["leaderboard"]),group_id))
                if data.get("rotate_invite") is True:
                    db.execute("UPDATE groups SET invite=? WHERE id=?", (secrets.token_urlsafe(12),group_id))
            else:
                if type(data.get("ranking")) is not bool: raise APIError(400,"請選擇是否加入排行。")
                db.execute("UPDATE members SET ranking=? WHERE group_id=? AND user_id=?", (int(data["ranking"]),group_id,user["id"]))
        return {"ok":True}

    def archive(self,user,kind,item_id):
        table={"task":"tasks","product":"products"}.get(kind)
        if not table: raise APIError(400,"無效項目。")
        with self.connection(True) as db:
            row=db.execute(f"SELECT group_id FROM {table} WHERE id=?", (item_id,)).fetchone()
            if not row: raise APIError(404,"找不到項目。")
            self.access(db,user,row["group_id"],True)
            db.execute(f"UPDATE {table} SET active=0 WHERE id=?", (item_id,))
        return {"ok":True}

    def leave(self,user,group_id):
        with self.connection(True) as db:
            group=self.access(db,user,group_id)
            if group["owner_id"] == user["id"]: raise APIError(409,"群組建立者不能以成員方式退出。")
            db.execute("DELETE FROM members WHERE group_id=? AND user_id=?", (group_id,user["id"]))
        return {"ok":True}

    def delete_account(self,user,data):
        password=data.get("password")
        if not isinstance(password,str) or len(password)>128: raise APIError(401,"請輸入目前密碼。")
        with self.connection(True) as db:
            row=db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if not row or not hmac.compare_digest(self.password_hash(password,row["salt"]),row["password"]):
                raise APIError(401,"密碼不正確，尚未刪除帳號。")
            for group in db.execute("SELECT id FROM groups WHERE owner_id=?", (user["id"],)).fetchall():
                gid=group["id"]
                db.execute("DELETE FROM ledger WHERE group_id=?", (gid,))
                db.execute("DELETE FROM redemptions WHERE group_id=?", (gid,))
                db.execute("DELETE FROM submission_events WHERE submission_id IN (SELECT s.id FROM submissions s JOIN tasks t ON t.id=s.task_id WHERE t.group_id=?)",(gid,))
                db.execute("DELETE FROM submissions WHERE task_id IN (SELECT id FROM tasks WHERE group_id=?)", (gid,))
                for table in ["tasks","products","members"]: db.execute(f"DELETE FROM {table} WHERE group_id=?", (gid,))
                db.execute("DELETE FROM groups WHERE id=?", (gid,))
            db.execute("DELETE FROM submission_events WHERE submission_id IN (SELECT id FROM submissions WHERE user_id=?)",(user["id"],))
            for table in ["ledger","redemptions","submissions","members","sessions"]:
                db.execute(f"DELETE FROM {table} WHERE user_id=?", (user["id"],))
            db.execute("DELETE FROM users WHERE id=?", (user["id"],))
        return {"ok":True}
