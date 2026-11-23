from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3

from .domain import Conflict, MOSCOW, Reservation, StoreBusy, Table, Visit


BOOKING_SELECT = """
    SELECT r.*, t.label, t.capacity
    FROM reservations AS r JOIN dining_tables AS t ON t.id = r.table_id
"""


def to_booking(row: sqlite3.Row) -> Reservation:
    return Reservation(row["id"], Table(row["table_id"], row["label"], row["capacity"]),
                       datetime.fromtimestamp(row["start_at"], MOSCOW),
                       datetime.fromtimestamp(row["end_at"], MOSCOW), row["party_size"],
                       row["guest_name"], row["phone"], row["status"])


class SQLiteStore:
    def __init__(self, path: Path, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(schema)

    @contextmanager
    def session(self, *, write: bool = False) -> Iterator["SQLiteSession"]:
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield SQLiteSession(db)
                db.commit()
            except sqlite3.OperationalError as exc:
                db.rollback()
                if getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise StoreBusy("Хранилище занято. Повторите запрос через секунду.") from exc
                raise
            except BaseException:
                db.rollback()
                raise


class SQLiteSession:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def available(self, visit: Visit, excluding: str | None = None) -> list[Table]:
        rows = self.db.execute("""
            SELECT t.id, t.label, t.capacity FROM dining_tables AS t
            WHERE t.capacity >= ? AND NOT EXISTS (
                SELECT 1 FROM occupied_slots AS s
                WHERE s.table_id = t.id AND s.slot_start >= ? AND s.slot_start < ?
                  AND (? IS NULL OR s.reservation_id <> ?)
            )
            ORDER BY t.capacity, t.id
        """, (visit.party_size, visit.start, visit.end, excluding, excluding))
        return [Table(**dict(row)) for row in rows]

    def by_key(self, digest: str) -> tuple[Reservation, str] | None:
        row = self.db.execute(BOOKING_SELECT + " WHERE r.owner_digest = ?", (digest,)).fetchone()
        return (to_booking(row), row["request_digest"]) if row else None

    def insert(self, item: Reservation, owner: str, fingerprint: str) -> None:
        self.db.execute("""
            INSERT INTO reservations
                (id, table_id, start_at, end_at, party_size, guest_name, phone, status, owner_digest, request_digest)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (item.id, item.table.id, int(item.start_at.timestamp()), int(item.end_at.timestamp()),
              item.party_size, item.guest_name, item.phone, item.status, owner, fingerprint))

    def place(self, item: Reservation, visit: Visit) -> None:
        self.db.execute("DELETE FROM occupied_slots WHERE reservation_id = ?", (item.id,))
        self.db.execute("UPDATE reservations SET table_id = ?, start_at = ?, end_at = ?, party_size = ? WHERE id = ?",
                        (item.table.id, visit.start, visit.end, visit.party_size, item.id))
        self.db.executemany("INSERT INTO occupied_slots(table_id, slot_start, reservation_id) VALUES (?, ?, ?)",
                            ((item.table.id, start, item.id) for start in visit.slots()))

    def cancel(self, reservation_id: str) -> None:
        self.db.execute("DELETE FROM occupied_slots WHERE reservation_id = ?", (reservation_id,))
        self.db.execute("UPDATE reservations SET status = 'cancelled' WHERE id = ?", (reservation_id,))

    def on_day(self, start: int, end: int) -> list[Reservation]:
        rows = self.db.execute(BOOKING_SELECT + """
            WHERE r.start_at >= ? AND r.start_at < ? ORDER BY r.start_at, r.table_id, r.id
        """, (start, end))
        return [to_booking(row) for row in rows]

    def add_table(self, label: str, capacity: int) -> Table:
        try:
            cursor = self.db.execute("INSERT INTO dining_tables(label, capacity) VALUES (?, ?)", (label, capacity))
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise Conflict("Столик с таким обозначением уже есть.") from exc
            raise
        return Table(cursor.lastrowid, label, capacity)
