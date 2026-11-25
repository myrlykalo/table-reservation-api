from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from reservation.api import create_app
from reservation.domain import MOSCOW, StoreBusy
from reservation.sqlite_store import SQLiteStore


class BookingTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite3"
        self.now = datetime(2026, 11, 24, 11, 0, tzinfo=MOSCOW)
        self.app = create_app(self.path, "staff-test-only", lambda: self.now)
        self.client = self.enterContext(TestClient(self.app))
        self.visit = {"start_at": "2026-11-25T19:00:00+03:00", "duration_minutes": 90, "party_size": 8}
        self.payload = {**self.visit, "guest_name": "Гость"}
        self.key = self.headers()
        self.staff = {"X-Admin-Key": "staff-test-only"}

    @staticmethod
    def headers():
        return {"Authorization": f"Bearer {uuid4()}"}

    def post(self, body=None, key=None):
        return self.client.post("/api/reservations", json=body or self.payload, headers=key or self.key)

    def test_best_fit_and_tie_break(self):
        first = self.post({**self.payload, "party_size": 3}).json()
        second = self.post({**self.payload, "party_size": 3}, self.headers()).json()
        self.assertEqual([first["table"]["label"], second["table"]["label"]], ["T03", "T04"])

    def test_public_search_excludes_guest_information(self):
        rows = self.client.get("/api/availability", params=self.visit).json()
        self.assertEqual(rows, [{"id": 6, "label": "T06", "capacity": 8}])
        self.post()
        self.assertEqual(self.client.get("/api/availability", params=self.visit).json(), [])

    def test_same_key_and_equivalent_timezone_replay(self):
        first = self.post()
        again = self.post({**self.payload, "start_at": "2026-11-25T16:00:00Z"})
        self.assertEqual([first.status_code, again.status_code], [201, 200])
        self.assertEqual(first.json(), again.json())
        self.assertTrue(first.headers["Location"].endswith(first.json()["id"]))

    def test_key_reuse_with_new_content_conflicts(self):
        self.post()
        self.assertEqual(self.post({**self.payload, "guest_name": "Другой"}).status_code, 409)

    def test_concurrent_distinct_guests_get_one_table(self):
        barrier = Barrier(8)
        def attempt(i):
            key = self.headers()
            barrier.wait(timeout=10)
            return self.post(key=key).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = sorted(pool.map(attempt, range(8)))
        self.assertEqual(results, [201] + [409] * 7)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM occupied_slots").fetchone()[0], 6)

    def test_simultaneous_retries_return_one_id(self):
        barrier = Barrier(4)
        def attempt(i):
            barrier.wait(timeout=10)
            r = self.post()
            return r.status_code, r.json()["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(sorted(code for code, _ in results), [200, 200, 200, 201])
        self.assertEqual(len({identifier for _, identifier in results}), 1)

    def test_end_is_exclusive(self):
        self.post()
        overlap = {**self.payload, "start_at": "2026-11-25T20:15:00+03:00"}
        adjacent = {**self.payload, "start_at": "2026-11-25T20:30:00+03:00"}
        self.assertEqual(self.post(overlap, self.headers()).status_code, 409)
        self.assertEqual(self.post(adjacent, self.headers()).status_code, 201)

    def test_move_reuses_own_overlapping_slots(self):
        loc = self.post().headers["Location"]
        new = {**self.visit, "start_at": "2026-11-25T19:15:00+03:00"}
        response = self.client.put(loc + "/visit", json=new, headers=self.key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["start_at"], new["start_at"])
        self.assertEqual(self.client.put(loc + "/visit", json=new, headers=self.key).json(), response.json())
        self.assertEqual(self.post().json(), response.json())

    def test_failed_move_preserves_original_slots(self):
        loc = self.post().headers["Location"]
        new = {**self.visit, "start_at": "2026-11-25T21:00:00+03:00"}
        self.post({**self.payload, **new}, self.headers())
        self.assertEqual(self.client.put(loc + "/visit", json=new, headers=self.key).status_code, 409)
        self.assertEqual(self.client.get(loc, headers=self.key).json()["start_at"], self.visit["start_at"])
        self.assertEqual(self.post(key=self.headers()).status_code, 409)

    def test_cancel_releases_capacity_and_keeps_history(self):
        loc = self.post().headers["Location"]
        for _ in range(2):
            self.assertEqual(self.client.delete(loc, headers=self.key).json()["status"], "cancelled")
        self.assertEqual(self.post().json()["status"], "cancelled")
        self.assertEqual(self.client.put(loc + "/visit", json=self.visit, headers=self.key).status_code, 409)
        self.assertEqual(self.post(key=self.headers()).status_code, 201)
        book = self.client.get("/api/admin/reservations", params={"day": "2026-11-25"}, headers=self.staff).json()
        self.assertEqual(sorted(r["status"] for r in book), ["cancelled", "confirmed"])

    def test_other_guest_cannot_read_change_or_cancel(self):
        loc = self.post().headers["Location"]
        self.assertEqual(self.client.get(loc, headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.put(loc + "/visit", json=self.visit, headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.delete(loc, headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.get(loc).status_code, 401)

    def test_service_day_boundaries(self):
        for time, duration, expected in [("11:45", 30, 422), ("12:00", 30, 201),
                                         ("22:30", 30, 201), ("22:45", 30, 422),
                                         ("19:10", 60, 422), ("18:00", 35, 422)]:
            with self.subTest(time=time, duration=duration):
                r = self.post({**self.payload, "start_at": f"2026-11-25T{time}:00+03:00",
                               "duration_minutes": duration}, self.headers())
                self.assertEqual(r.status_code, expected)

    def test_horizon_and_past(self):
        for day, hour, expected in [(0, 10, 422), (0, 12, 201), (30, 14, 201), (31, 14, 422)]:
            start = (self.now + timedelta(days=day)).replace(hour=hour)
            with self.subTest(day=day, hour=hour):
                self.assertEqual(self.post({**self.payload, "start_at": start.isoformat()},
                                           self.headers()).status_code, expected)

    def test_invalid_input_and_clock_offset(self):
        for patch in [{"guest_name": " "}, {"phone": "abc"}, {"party_size": 9}, {"duration_minutes": True},
                      {"start_at": "2026-11-25T19:00:00"}, {"start_at": 1795636800}, {"unknown": 1}]:
            with self.subTest(patch=patch):
                self.assertEqual(self.post({**self.payload, **patch}).status_code, 422)

    def test_started_visit_immutable_but_post_can_be_replayed(self):
        loc = self.post().headers["Location"]
        self.now += timedelta(days=2)
        self.assertEqual(self.client.delete(loc, headers=self.key).status_code, 409)
        self.assertEqual(self.client.put(loc + "/visit", json=self.visit, headers=self.key).status_code, 409)
        self.assertEqual(self.post().status_code, 200)

    def test_staff_journal_and_extra_table(self):
        url = "/api/admin/reservations"
        self.assertEqual(self.client.get(url, params={"day": "2026-11-25"}, headers=self.key).status_code, 403)
        self.post()
        data = {"label": "W01", "capacity": 8}
        self.assertEqual(self.client.post("/api/admin/tables", json=data, headers=self.staff).status_code, 201)
        self.assertEqual(self.client.post("/api/admin/tables", json={**data, "label": "w01"},
                                          headers=self.staff).status_code, 409)
        self.assertEqual(self.post(key=self.headers()).json()["table"]["label"], "W01")
        self.assertEqual(self.client.get(url, params={"day": "2026-11-26"}, headers=self.staff).json(), [])

    def test_restart_preserves_bookings_and_does_not_duplicate_tables(self):
        loc = self.post().headers["Location"]
        with TestClient(create_app(self.path, "staff-test-only", lambda: self.now)) as another:
            self.assertEqual(another.get(loc, headers=self.key).status_code, 200)
            self.assertEqual(another.get("/api/availability", params=self.visit).json(), [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dining_tables").fetchone()[0], 6)

    def test_database_key_prevents_duplicate_slot_and_rolls_back(self):
        loc = self.post().headers["Location"]
        store = SQLiteStore(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            with store.session(write=True) as session:
                row = session.db.execute("SELECT * FROM occupied_slots LIMIT 1").fetchone()
                session.db.execute("UPDATE reservations SET guest_name='rollback check'")
                session.db.execute("INSERT INTO occupied_slots VALUES (?, ?, ?)", tuple(row))
        self.assertEqual(self.client.get(loc, headers=self.key).json()["guest_name"], "Гость")

    def test_lock_timeout_is_distinct_from_sold_out(self):
        with SQLiteStore(self.path).session(write=True):
            with self.assertRaises(StoreBusy):
                with SQLiteStore(self.path, timeout=0.01).session(write=True):
                    self.fail("A second writer should wait for the first.")

    def test_live_api_contract_lists_auth_schemes(self):
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(set(schema["components"]["securitySchemes"]), {"GuestBookingKey", "StaffKey"})
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
