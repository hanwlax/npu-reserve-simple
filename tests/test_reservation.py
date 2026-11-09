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

PASSWORD = "test-password"
ALICE = {"username": "h00893113", "name": "用户甲", "password": PASSWORD}
BOB = {"username": "a12345678", "name": "用户乙", "password": PASSWORD}
POOL = [{"id": "npu-01", "name": "机器一", "ip": "192.0.2.11"}]


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
        self.alice, result = self.store.register(ALICE)
        self.bob, _ = self.store.register(BOB)
        self.machine = result["state"]["machines"][0]["id"]
        self.window = result["state"]["window"]

    def configure(self, machines):
        self.config.write_text(json.dumps({"machines": machines}), encoding="utf-8")
        self.store.initialize(load_config(self.config))

    def payload(self, start, end):
        return {"m": self.machine, "window": self.window, "s": start, "e": end}

    def book(self, token, start, end):
        return self.store.mutate(token, "book", self.payload(start, end))["state"]


class ReservationTests(Fixture):
    def test_registration_requires_employee_id_and_logs_in_immediately(self):
        for username in ("abcdefghi", "hh00893113", "h1234567", "h１２３４５６７８", " h12345678"):
            with self.assertRaises(Error) as invalid:
                self.store.register({"username": username, "password": PASSWORD})
            self.assertEqual(invalid.exception.status, 422)
        with self.assertRaises(Error) as duplicate:
            self.store.register({"username": "H00893113", "password": PASSWORD})
        self.assertEqual(duplicate.exception.status, 409)
        with self.assertRaises(Error) as invalid_login:
            self.store.login({"username": "admin", "password": PASSWORD})
        self.assertEqual(invalid_login.exception.status, 401)

        token, result = self.store.register({"username": "K87654321", "generate": True})
        self.assertRegex(result["generatedPassword"], "^[A-Za-z0-9]{8}$")
        self.assertEqual(self.store.state(token)["user"]["username"], "k87654321")
        _, logged_in = self.store.login({"username": "K87654321", "password": result["generatedPassword"]})
        self.assertEqual(logged_in["machines"], self.store.state(self.alice)["machines"])
        self.assertNotIn("role", logged_in["user"])

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

    def test_history_bounds_pagination_and_retired_machine(self):
        alice_id = self.store.state(self.alice)["user"]["id"]
        with self.store.transaction(write=True) as db:
            for slot in range(-7 * 48 - 1, -7 * 48 + 102):
                start = self.now + slot * SLOT
                self.store.insert_interval(db, self.machine, start, start + SLOT, alice_id)
        self.book(self.alice, 6, 10)
        history = self.store.history(self.bob, 1)
        self.assertEqual(history["from"], self.now - 7 * DAY)
        self.assertEqual(len(history["records"]), 100)
        self.assertTrue(history["hasMore"])
        remainder = self.store.history(self.bob, 2)
        self.assertEqual(len(remainder["records"]), 2)
        self.assertFalse(remainder["hasMore"])
        self.assertEqual(remainder["records"][-1]["start"], self.now - 7 * DAY)
        self.assertNotIn("username", history["records"][0])
        self.assertTrue(all(record["start"] < self.now for record in history["records"]))
        self.configure([])
        self.assertEqual(self.store.state(self.alice)["machines"], [])
        self.assertEqual(self.store.state(self.alice)["upcoming"], [])
        self.assertEqual(self.store.history(self.alice, 1)["records"][0]["machine"], "机器一")
        with self.assertRaises(Error):
            self.store.history(None, 1)

    def test_config_validation_and_stable_machine_identity(self):
        self.book(self.alice, 6, 8)
        self.configure([{**POOL[0], "name": "新名称", "ip": "192.0.2.22"}])
        state = self.store.state(self.alice)
        self.assertEqual(state["machines"][0]["id"], self.machine)
        self.assertEqual(state["machines"][0]["name"], "新名称")
        self.assertEqual(state["records"][0]["m"], self.machine)
        for machines in ([POOL[0], POOL[0]], [POOL[0], {**POOL[0], "id": "another"}], [{**POOL[0], "ip": "bad"}]):
            with self.assertRaises(ValueError):
                self.configure(machines)
        self.assertEqual(self.store.state(self.alice)["machines"][0]["name"], "新名称")

    def test_migration_with_config_and_database(self):
        self.book(self.alice, 1, 3)
        self.now += 2 * SLOT
        new_dir = self.root / "migrated"
        shutil.copytree(self.state_dir, new_dir)
        target = Store(new_dir / self.db.name, clock=lambda: self.now)
        target.initialize(load_config(new_dir / self.config.name))
        token, state = target.login(ALICE)
        self.assertEqual(state["records"][0]["operator"], "用户甲")
        self.assertEqual(state["upcoming"][0]["owner"], ALICE["username"])
        self.assertEqual(target.history(token, 1)["records"][0]["machine"], "机器一")


class HTTPTests(Fixture):
    def test_http_registration_booking_and_history(self):
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
        self.assertEqual(request("/api/register", {"username": "invalid", "password": PASSWORD})[0], 422)
        status, cookie, body = request("/api/register", {"username": "b87654321", "password": PASSWORD})
        self.assertEqual(status, 201)
        self.assertIn("npu_simple_session=", cookie)
        self.assertIn("HttpOnly; SameSite=Strict", cookie)
        cookie = cookie.split(";", 1)[0]
        self.assertEqual(json.loads(body)["state"]["machines"][0]["id"], self.machine)
        self.assertEqual(request("/api/book", self.payload(1, 3), cookie)[0], 200)
        self.now += 2 * SLOT
        status, _, body = request("/api/history?page=1", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["records"][0]["name"], "b87654321")
        self.assertEqual(request("/api/history?page=0", cookie=cookie)[0], 422)
        self.assertEqual(request("/api/create-account", {}, cookie)[0], 404)
