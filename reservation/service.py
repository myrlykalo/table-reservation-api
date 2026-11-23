from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
from uuid import uuid4

from .domain import Conflict, MissingReservation, MOSCOW, Reservation, Table, Visit
from .ports import BookingSession, BookingStore


def key_digest(key: str) -> str:
    return sha256(key.encode("ascii")).hexdigest()


class BookingService:
    def __init__(self, store: BookingStore, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def availability(self, visit: Visit) -> list[Table]:
        visit.bookable_at(self.clock())
        with self.store.session() as session:
            return session.available(visit)

    def create(self, key: str, visit: Visit, name: str, phone: str | None) -> tuple[Reservation, bool]:
        owner = key_digest(key)
        content = json.dumps([visit.start, visit.duration_minutes, visit.party_size, name, phone],
                             ensure_ascii=False, separators=(",", ":"))
        fingerprint = sha256(content.encode("utf-8")).hexdigest()
        with self.store.session(write=True) as session:
            existing = session.by_key(owner)
            if existing:
                item, original = existing
                if original != fingerprint:
                    raise Conflict("Этот ключ уже использован для другой заявки.")
                return item, False
            visit.bookable_at(self.clock())
            table = self._choose(session, visit)
            item = Reservation(str(uuid4()), table, visit.start_at.astimezone(MOSCOW),
                               datetime.fromtimestamp(visit.end, MOSCOW), visit.party_size,
                               name, phone, "confirmed")
            session.insert(item, owner, fingerprint)
            session.place(item, visit)
            return item, True

    @staticmethod
    def _choose(session: BookingSession, visit: Visit, excluding: str | None = None) -> Table:
        candidates = session.available(visit, excluding)
        if not candidates:
            raise Conflict("На это время нет свободного столика подходящей вместимости.")
        return candidates[0]

    @staticmethod
    def _owned(session: BookingSession, reservation_id: str, key: str) -> Reservation:
        found = session.by_key(key_digest(key))
        if not found or found[0].id != reservation_id:
            raise MissingReservation("Бронь не найдена.")
        return found[0]

    def find(self, reservation_id: str, key: str) -> Reservation:
        with self.store.session() as session:
            return self._owned(session, reservation_id, key)

    def reschedule(self, reservation_id: str, key: str, visit: Visit) -> Reservation:
        with self.store.session(write=True) as session:
            item = self._owned(session, reservation_id, key)
            now = self.clock()
            if item.status != "confirmed" or item.start_at <= now:
                raise Conflict("Перенос возможен только до начала подтверждённого визита.")
            visit.bookable_at(now)
            if (item.start_at == visit.start_at and item.end_at.timestamp() == visit.end
                    and item.party_size == visit.party_size):
                return item
            table = self._choose(session, visit, excluding=item.id)
            updated = replace(item, table=table, start_at=visit.start_at.astimezone(MOSCOW),
                              end_at=datetime.fromtimestamp(visit.end, MOSCOW), party_size=visit.party_size)
            session.place(updated, visit)
            return updated

    def cancel(self, reservation_id: str, key: str) -> Reservation:
        with self.store.session(write=True) as session:
            item = self._owned(session, reservation_id, key)
            if item.status == "cancelled":
                return item
            if item.start_at <= self.clock():
                raise Conflict("Начавшийся визит нельзя отменить.")
            session.cancel(item.id)
            return replace(item, status="cancelled")

    def daily_book(self, day: date) -> list[Reservation]:
        start = int((datetime.combine(day, time.min, MOSCOW) -
                     datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())
        with self.store.session() as session:
            return session.on_day(start, start + 86400)

    def add_table(self, label: str, capacity: int) -> Table:
        with self.store.session(write=True) as session:
            return session.add_table(label, capacity)
