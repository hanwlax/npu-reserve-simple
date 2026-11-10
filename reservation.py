"""SQLite storage for the self-service reservation site."""

import hashlib
import ipaddress
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SLOT = 1800
DAY = 48 * SLOT
BEIJING = timezone(timedelta(hours=8))
SESSION_SECONDS = 12 * 3600
USERNAME = re.compile(r"[a-zA-Z][0-9]{8}")
HISTORY_PAGE_SIZE = 100
MUTATIONS = {"book": "book", "release": "release"}

ACCOUNT_FIELDS = """
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS accounts ({ACCOUNT_FIELDS});
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY,
    config_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    ip TEXT NOT NULL,
    spec TEXT NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS active_machine_ip ON machines(ip) WHERE retired=0;
CREATE TABLE IF NOT EXISTS intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL REFERENCES machines(id),
    start INTEGER NOT NULL CHECK(start % 1800 = 0),
    end INTEGER NOT NULL CHECK(end % 1800 = 0 AND end > start),
    owner_id INTEGER NOT NULL REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS interval_machine_time ON intervals(machine_id, start, end);
CREATE INDEX IF NOT EXISTS interval_end ON intervals(end);
CREATE INDEX IF NOT EXISTS interval_history ON intervals(start DESC,id DESC);
CREATE TRIGGER IF NOT EXISTS interval_no_overlap
BEFORE INSERT ON intervals WHEN EXISTS (
    SELECT 1 FROM intervals
    WHERE machine_id=NEW.machine_id AND start<NEW.end AND end>NEW.start
)
BEGIN SELECT RAISE(ABORT,'interval_overlap'); END;
CREATE TRIGGER IF NOT EXISTS interval_update_no_overlap
BEFORE UPDATE OF machine_id,start,end ON intervals WHEN EXISTS (
    SELECT 1 FROM intervals
    WHERE id<>OLD.id AND machine_id=NEW.machine_id AND start<NEW.end AND end>NEW.start
)
BEGIN SELECT RAISE(ABORT,'interval_overlap'); END;
CREATE TABLE IF NOT EXISTS metadata (
    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO metadata VALUES (1,0);
"""


class Error(Exception):
    def __init__(self, status, message, code="invalid"):
        super().__init__(message)
        self.status = status
        self.code = code


def text(data, key, maximum, *, required=True):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise Error(422, f"{key} 必须为文本。")
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise Error(422, f"{key} 需要填写 1–{maximum} 个字符。" if required else f"{key} 最多 {maximum} 个字符。")
    return value


def integer(data, key, *, minimum=0):
    value = data.get(key)
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise Error(422, f"{key} 必须为有效整数。")
    return value


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def clock_window(now):
    local = datetime.fromtimestamp(now, BEIJING)
    day = local.replace(hour=8, minute=0, second=0, microsecond=0)
    start = int(day.timestamp())
    cutoff = -(-now // SLOT) * SLOT
    return start, cutoff


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("machines"), list):
        raise ValueError("config.json 必须包含 machines 数组。")
    machines, used_ips = [], set()
    for number, item in enumerate(data["machines"], 1):
        if not isinstance(item, dict):
            raise ValueError(f"machines 第 {number} 项必须为对象。")
        try:
            name = text(item, "name", 48)
            raw_ip = text(item, "ip", 45)
            if "%" in raw_ip:
                raise ValueError("IP 地址不能带网卡区域标识")
            ip = str(ipaddress.ip_address(raw_ip))
            spec = text(item, "spec", 80, required=False)
        except (Error, ValueError) as error:
            raise ValueError(f"machines 第 {number} 项无效：{error}") from None
        if ip in used_ips:
            raise ValueError(f"machines 第 {number} 项的 IP 重复。")
        used_ips.add(ip)
        machines.append((ip, name, spec))
    return machines


class Store:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    @contextmanager
    def transaction(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self, machines):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(machines)")}
            if columns and "config_id" not in columns:
                raise ValueError("该数据库属于完整版，请为简版指定独立数据库。")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            self.migrate_accounts(db)
        finally:
            db.close()
        with self.transaction(write=True) as db:
            before = {row["ip"]: row["id"] for row in db.execute("SELECT id,ip FROM machines WHERE retired=0")}
            configured = {machine[0] for machine in machines}
            _, cutoff = clock_window(int(self.clock()))
            for removed in before.keys() - configured:
                machine = before[removed]
                records = db.execute("SELECT * FROM intervals WHERE machine_id=? AND end>?", (machine, cutoff)).fetchall()
                for record in records:
                    self.cut(db, record, cutoff, record["end"])
                db.execute("UPDATE machines SET retired=1 WHERE id=?", (machine,))
            for ip, name, spec in machines:
                if ip in before:
                    db.execute("UPDATE machines SET name=?,spec=? WHERE id=?", (name, spec, before[ip]))
                else:
                    db.execute("INSERT INTO machines(config_id,name,ip,spec) VALUES (?,?,?,?)",
                               (secrets.token_hex(16), name, ip, spec))
            self.bump(db)

    @staticmethod
    def migrate_accounts(db):
        # This setup connection is separate from requests, which always enforce foreign keys.
        db.execute("PRAGMA foreign_keys=OFF")
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
            if "password" not in columns:
                return
            db.execute(f"CREATE TABLE accounts_new ({ACCOUNT_FIELDS})")
            db.execute("INSERT INTO accounts_new SELECT id,username,name FROM accounts")
            db.execute("DROP TABLE accounts")
            db.execute("ALTER TABLE accounts_new RENAME TO accounts")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("账户迁移后的外键校验失败，数据库未更新。")

    def login(self, data, previous=None):
        username = data.get("username")
        if not isinstance(username, str) or not USERNAME.fullmatch(username):
            raise Error(422, "工号须为一个英文字母加八位数字，例如 h00893113。")
        username = username.lower()
        with self.transaction(write=True) as db:
            db.execute("INSERT INTO accounts(username,name) VALUES (?,?) ON CONFLICT(username) DO NOTHING",
                       (username, username))
            account = db.execute("SELECT id,username,name FROM accounts WHERE username=?", (username,)).fetchone()
            now = int(self.clock())
            token = self.create_session(db, account["id"], previous, now)
            state = self.snapshot(db, account, now)
        return token, state

    @staticmethod
    def create_session(db, account, previous, now):
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires<=? OR token=?", (now, token_hash(previous or "")))
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (token_hash(token), account, now + SESSION_SECONDS))
        return token

    def logout(self, token):
        if token:
            with self.transaction(write=True) as db:
                db.execute("DELETE FROM sessions WHERE token=?", (token_hash(token),))

    @staticmethod
    def actor(db, token, now):
        row = db.execute(
            "SELECT a.id,a.username,a.name FROM sessions s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.token=? AND s.expires>?", (token_hash(token or ""), now),
        ).fetchone()
        if row is None:
            raise Error(401, "请先登录。", "login_required")
        return row

    @staticmethod
    def revision(db):
        return db.execute("SELECT revision FROM metadata WHERE id=1").fetchone()[0]

    @staticmethod
    def bump(db):
        db.execute("UPDATE metadata SET revision=revision+1 WHERE id=1")

    def state(self, token):
        with self.transaction() as db:
            now = int(self.clock())
            return self.snapshot(db, self.actor(db, token, now), now)

    def snapshot(self, db, actor, now):
        start, cutoff = clock_window(now)
        machines = [dict(row) for row in db.execute("SELECT id,name,ip,spec FROM machines WHERE retired=0 ORDER BY id")]
        intervals = db.execute(
            "SELECT i.*,a.name,a.username FROM intervals i "
            "JOIN accounts a ON a.id=i.owner_id JOIN machines m ON m.id=i.machine_id "
            "WHERE m.retired=0 AND i.start<? AND i.end>? ORDER BY i.start,i.id", (start + DAY, min(start, cutoff)),
        )
        records, upcoming = [], []
        for row in intervals:
            record = {"id": row["id"], "m": row["machine_id"], "s": (row["start"] - start) // SLOT,
                      "e": (row["end"] - start) // SLOT, "operator": row["name"],
                      "operatorId": row["owner_id"], "operatorUsername": row["username"]}
            if row["owner_id"] == actor["id"]:
                record["owner"] = row["username"]
                if row["end"] > cutoff:
                    upcoming.append(record)
            if row["end"] > start:
                records.append(record)
        return {
            "user": {key: actor[key] for key in ("id", "username", "name")},
            "machines": machines, "records": records, "upcoming": upcoming, "window": start,
            "cutoff": (cutoff - start) // SLOT, "pastEnd": max(0, (cutoff - start) // SLOT),
            "revision": self.revision(db),
        }

    def history(self, token, page):
        if not 1 <= page <= 1_000_000:
            raise Error(422, "历史页码无效。")
        with self.transaction() as db:
            now = int(self.clock())
            self.actor(db, token, now)
            rows = db.execute(
                "SELECT i.id,i.start,i.end,m.name AS machine,m.ip,m.retired,a.name "
                "FROM intervals i JOIN machines m ON m.id=i.machine_id JOIN accounts a ON a.id=i.owner_id "
                "WHERE i.start<? ORDER BY i.start DESC,i.id DESC LIMIT ? OFFSET ?",
                (now, HISTORY_PAGE_SIZE + 1, (page - 1) * HISTORY_PAGE_SIZE),
            ).fetchall()
            return {"to": now, "page": page, "hasMore": len(rows) > HISTORY_PAGE_SIZE,
                    "records": [dict(row) for row in rows[:HISTORY_PAGE_SIZE]]}

    def mutate(self, token, action, data):
        if action not in MUTATIONS:
            raise Error(404, "接口不存在。")
        with self.transaction(write=True) as db:
            now = int(self.clock())
            actor = self.actor(db, token, now)
            message = getattr(self, MUTATIONS[action])(db, actor, data, now)
            self.bump(db)
            return {"message": message, "state": self.snapshot(db, actor, now)}

    @staticmethod
    def selected_range(data, now, *, overnight=False):
        window, cutoff = clock_window(now)
        if integer(data, "window") != window:
            raise Error(409, "预约窗口已切换，请在更新时间轴后重选。", "state_changed")
        # Previous-window reservations may still be releasable after midnight.
        first = min(0, (cutoff - window) // SLOT) if overnight else 0
        s, e = integer(data, "s", minimum=first), integer(data, "e", minimum=first)
        if not first <= s < e <= 48:
            raise Error(422, "请选择时间轴内有效的起止时间。")
        start, end = window + s * SLOT, window + e * SLOT
        if start < cutoff:
            raise Error(409, "所选时段已经开始，请重新选择。", "state_changed")
        return start, end

    @staticmethod
    def overlaps(db, machine, start, end):
        return db.execute(
            "SELECT * FROM intervals WHERE machine_id=? AND start<? AND end>? ORDER BY start",
            (machine, end, start),
        ).fetchall()

    @staticmethod
    def insert_interval(db, machine, start, end, owner):
        db.execute("INSERT INTO intervals(machine_id,start,end,owner_id) VALUES (?,?,?,?)",
                   (machine, start, end, owner))

    def cut(self, db, record, start, end):
        db.execute("DELETE FROM intervals WHERE id=?", (record["id"],))
        for left, right in ((record["start"], min(start, record["end"])), (max(end, record["start"]), record["end"])):
            if left < right:
                self.insert_interval(db, record["machine_id"], left, right, record["owner_id"])

    def book(self, db, actor, data, now):
        machine = db.execute("SELECT id,name FROM machines WHERE id=? AND retired=0", (integer(data, "m"),)).fetchone()
        if machine is None:
            raise Error(404, "机器已从资源池移除。", "state_changed")
        start, end = self.selected_range(data, now)
        if self.overlaps(db, machine["id"], start, end):
            raise Error(409, "预约未成功，所选时段已被预约。请重新选择。", "occupied")
        self.insert_interval(db, machine["id"], start, end, actor["id"])
        return f"预约成功 · {machine['name']}"

    def release(self, db, actor, data, now):
        record = db.execute("SELECT * FROM intervals WHERE id=? AND owner_id=?",
                            (integer(data, "id"), actor["id"])).fetchone()
        if record is None:
            raise Error(409, "预约已变更或不可操作，请重新选择。", "state_changed")
        start, end = self.selected_range(data, now, overnight=True)
        if not record["start"] <= start < end <= record["end"]:
            raise Error(422, "只能释放本人预约范围内的机时。")
        self.cut(db, record, start, end)
        return "已释放，所选时段可重新预约。"
