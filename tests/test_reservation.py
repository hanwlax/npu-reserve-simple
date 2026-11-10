import errno
import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from threading import Barrier, Thread

from reservation import BEIJING, DAY, SLOT, Error, Store, load_config
from server import Server

ALICE = {"username": "h00893113"}
BOB = {"username": "a12345678"}
POOL = [{"name": "机器一", "ip": "192.0.2.11"}]


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config = self.state_dir / "config.json"
        self.db = self.state_dir / "reservations.sqlite3"
        self.now = int(datetime(2026, 11, 9, 8, tzinfo=BEIJING).timestamp())
        self.store = Store(self.db, clock=lambda: self.now)
        self.configure(POOL)
        self.alice, state = self.store.login(ALICE)
        self.bob, _ = self.store.login(BOB)
        self.machine = state["machines"][0]["id"]
        self.window = state["window"]

    def configure(self, machines):
        self.config.write_text(json.dumps({"machines": machines}), encoding="utf-8")
        self.store.initialize(load_config(self.config))

    def payload(self, start, end):
        return {"m": self.machine, "window": self.window, "s": start, "e": end}

    def book(self, token, start, end):
        return self.store.mutate(token, "book", self.payload(start, end))["state"]


class ReservationTests(Fixture):
    def test_employee_id_login_creates_or_reuses_the_account(self):
        for username in ("abcdefghi", "hh00893113", "h1234567", "h１２３４５６７８", " h12345678"):
            with self.assertRaises(Error) as invalid:
                self.store.login({"username": username})
            self.assertEqual(invalid.exception.status, 422)

        token, state = self.store.login({"username": "K87654321"})
        self.assertEqual(state["user"]["username"], "k87654321")
        _, again = self.store.login({"username": "k87654321"}, previous=token)
        self.assertEqual(again["user"], state["user"])
        self.assertEqual(again["machines"], self.store.state(self.alice)["machines"])
        with self.assertRaises(Error):
            self.store.state(token)

    def test_upgrade_removes_legacy_password_and_keeps_ownership(self):
        booking = self.book(self.alice, 1, 4)["records"][0]
        user_id = self.store.state(self.alice)["user"]["id"]
        with self.store.transaction(write=True) as db:
            db.execute("ALTER TABLE accounts ADD COLUMN password TEXT NOT NULL DEFAULT 'old-hash'")
            db.execute("UPDATE accounts SET name=? WHERE id=?", ("原显示名", user_id))

        self.store.initialize(load_config(self.config))
        self.store.initialize(load_config(self.config))
        with self.store.transaction() as db:
            self.assertNotIn("password", {row["name"] for row in db.execute("PRAGMA table_info(accounts)")})
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.now += 2 * SLOT
        token, state = self.store.login({"username": "H00893113"})
        self.assertEqual(state["user"]["id"], user_id)
        self.assertEqual(state["user"]["name"], "原显示名")
        self.assertEqual(state["upcoming"][0]["id"], booking["id"])
        self.assertEqual(self.store.history(token, 1)["records"][0]["name"], "原显示名")
        self.store.mutate(token, "release", {**self.payload(2, 4), "id": booking["id"]})

    def test_concurrent_booking_allows_one_winner_and_adjacent_slots(self):
        barrier = Barrier(8)

        def reserve(token):
            barrier.wait(timeout=10)
            try:
                self.book(token, 6, 10)
                return 200
            except Error as error:
                self.assertEqual(error.code, "occupied")
                return error.status

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(reserve, [self.alice, self.bob] * 4))
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 7)
        self.book(self.bob, 10, 12)

    def test_only_owner_can_release_and_split_booking(self):
        booking = self.book(self.alice, 4, 12)["records"][0]
        payload = {**self.payload(6, 8), "id": booking["id"]}
        with self.assertRaises(Error):
            self.store.mutate(self.bob, "release", payload)
        state = self.store.mutate(self.alice, "release", payload)["state"]
        self.assertEqual([(r["s"], r["e"]) for r in state["records"]], [(4, 6), (8, 12)])
        self.book(self.bob, 6, 8)
        self.assertNotIn("owner", self.store.state(self.bob)["records"][0])

    def test_other_viewers_can_distinguish_adjacent_owners(self):
        self.book(self.alice, 4, 5)
        self.book(self.bob, 5, 6)
        viewer, state = self.store.login({"username": "c12345678"})
        records = self.store.state(viewer)["records"]
        self.assertEqual([r["operatorUsername"] for r in records], [ALICE["username"], BOB["username"]])
        self.assertEqual(len({r["operatorId"] for r in records}), 2)
        self.assertTrue(all("owner" not in record for record in records))
        self.assertEqual(state["upcoming"], [])

    def test_history_includes_older_records_and_retired_machine(self):
        alice_id = self.store.state(self.alice)["user"]["id"]
        with self.store.transaction(write=True) as db:
            for slot in range(-7 * 48 - 1, -7 * 48 + 102):
                start = self.now + slot * SLOT
                self.store.insert_interval(db, self.machine, start, start + SLOT, alice_id)
        self.book(self.alice, 6, 10)
        history = self.store.history(self.bob, 1)
        self.assertEqual(len(history["records"]), 100)
        self.assertTrue(history["hasMore"])
        remainder = self.store.history(self.bob, 2)
        self.assertEqual(len(remainder["records"]), 3)
        self.assertFalse(remainder["hasMore"])
        self.assertEqual(remainder["records"][-1]["start"], self.now - 7 * DAY - SLOT)
        self.assertNotIn("username", history["records"][0])
        self.assertTrue(all(record["start"] < self.now for record in history["records"]))
        self.configure([])
        self.assertEqual(self.store.state(self.alice)["machines"], [])
        self.assertEqual(self.store.state(self.alice)["upcoming"], [])
        old = self.store.history(self.alice, 1)["records"][0]
        self.assertEqual(old["machine"], "机器一")
        self.assertEqual(old["retired"], 1)
        with self.assertRaises(Error):
            self.store.history(None, 1)

    def test_config_matches_active_ip_and_ignores_legacy_id(self):
        self.book(self.alice, 6, 8)
        self.configure([{**POOL[0], "id": "legacy", "name": "新名称"}])
        state = self.store.state(self.alice)
        self.assertEqual(state["machines"][0]["id"], self.machine)
        self.assertEqual(state["machines"][0]["name"], "新名称")
        self.assertEqual(state["records"][0]["m"], self.machine)
        for machines in (
            [POOL[0], POOL[0]], [{**POOL[0], "ip": "bad"}],
            [{**POOL[0], "ip": "2001:db8::1"}, {**POOL[0], "ip": "2001:0db8:0:0:0:0:0:1"}],
        ):
            with self.assertRaises(ValueError):
                self.configure(machines)
        self.assertEqual(self.store.state(self.alice)["machines"][0]["name"], "新名称")

    def test_replacement_with_reused_id_or_ip_preserves_old_history(self):
        self.configure([{**POOL[0], "id": "legacy"}])
        self.book(self.alice, 0, 4)
        self.now += 2 * SLOT
        self.configure([{**POOL[0], "id": "legacy", "name": "替换机", "ip": "192.0.2.22"}])
        replacement = self.store.state(self.alice)
        self.assertNotEqual(replacement["machines"][0]["id"], self.machine)
        self.assertEqual(replacement["records"], [])
        self.configure([{**POOL[0], "name": "新机器复用 IP"}])
        active, = self.store.state(self.alice)["machines"]
        self.assertNotEqual(active["id"], self.machine)
        old, = self.store.history(self.alice, 1)["records"]
        self.assertEqual((old["machine"], old["ip"], old["retired"]), ("机器一", "192.0.2.11", 1))
        self.assertEqual(old["end"], self.now)

    def test_migration_with_config_and_database(self):
        self.book(self.alice, 1, 3)
        self.now += 2 * SLOT
        new_dir = self.root / "migrated"
        shutil.copytree(self.state_dir, new_dir)
        target = Store(new_dir / self.db.name, clock=lambda: self.now)
        target.initialize(load_config(new_dir / self.config.name))
        token, state = target.login(ALICE)
        self.assertEqual(state["records"][0]["operator"], ALICE["username"])
        self.assertEqual(state["upcoming"][0]["owner"], ALICE["username"])
        self.assertEqual(target.history(token, 1)["records"][0]["machine"], "机器一")


class HTTPTests(Fixture):
    def test_http_login_booking_and_history(self):
        try:
            server = Server(("127.0.0.1", 0), self.store)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EPERM):
                self.skipTest("本地网络监听受限")
            raise
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

        def request(path, payload=None, cookie=None):
            headers = {"Content-Type": "application/json", "X-NPU-Request": "1"}
            if cookie:
                headers["Cookie"] = cookie
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as conn:
                conn.request("GET" if payload is None else "POST", path,
                             json.dumps(payload) if payload is not None else None, headers)
                response = conn.getresponse()
                return response.status, response.getheader("Set-Cookie"), response.read()

        self.assertEqual(request("/")[0], 200)
        self.assertEqual(request("/api/history")[0], 401)
        self.assertEqual(request("/api/login", {"username": "invalid"})[0], 422)
        status, cookie, body = request("/api/login", {"username": "b87654321"})
        self.assertEqual(status, 200)
        self.assertIn("npu_simple_session=", cookie)
        self.assertIn("HttpOnly; SameSite=Strict", cookie)
        cookie = cookie.split(";", 1)[0]
        self.assertEqual(json.loads(body)["machines"][0]["id"], self.machine)
        self.assertEqual(request("/api/book", self.payload(1, 3), cookie)[0], 200)
        self.now += 2 * SLOT
        status, _, body = request("/api/history?page=1", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["records"][0]["name"], "b87654321")
        self.assertEqual(request("/api/history?page=0", cookie=cookie)[0], 422)
        for path in ("/api/register", "/api/change-password", "/api/create-account"):
            self.assertEqual(request(path, {}, cookie)[0], 404)
