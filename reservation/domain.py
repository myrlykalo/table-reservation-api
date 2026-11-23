"""Restaurant policy. Times from the API are converted to Moscow time."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3), "MSK")
STEP = 15
OPEN_HOUR = 12
CLOSE_HOUR = 23
HORIZON_DAYS = 30


class InvalidVisit(Exception):
    pass


class Conflict(Exception):
    pass


class MissingReservation(Exception):
    pass


class StoreBusy(Exception):
    pass


@dataclass(frozen=True)
class Visit:
    start_at: datetime
    duration_minutes: int
    party_size: int

    def __post_init__(self):
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise InvalidVisit("Укажите время с часовым смещением.")
        try:
            local = self.start_at.astimezone(MOSCOW)
        except (OverflowError, ValueError):
            raise InvalidVisit("Дата за пределами рабочего календаря.") from None
        if not 1 <= self.party_size <= 8:
            raise InvalidVisit("За один столик можно посадить от 1 до 8 гостей.")
        if not 30 <= self.duration_minutes <= 180 or self.duration_minutes % STEP:
            raise InvalidVisit("Длительность от 30 до 180 минут с шагом 15 минут.")
        if local.second or local.microsecond or local.minute % STEP:
            raise InvalidVisit("Время начала должно совпадать с четвертью часа.")
        minute_of_day = local.hour * 60 + local.minute
        if minute_of_day < OPEN_HOUR * 60 or minute_of_day + self.duration_minutes > CLOSE_HOUR * 60:
            raise InvalidVisit("Визит должен целиком входить в часы работы 12:00–23:00 МСК.")

    def bookable_at(self, now: datetime):
        if self.start_at <= now:
            raise InvalidVisit("Начало визита уже прошло.")
        if (self.start_at.astimezone(MOSCOW).date() - now.astimezone(MOSCOW).date()).days > HORIZON_DAYS:
            raise InvalidVisit("Доступны даты на 30 дней вперёд включительно.")

    @property
    def start(self) -> int:
        return int(self.start_at.timestamp())

    @property
    def end(self) -> int:
        return self.start + self.duration_minutes * 60

    def slots(self) -> range:
        return range(self.start, self.end, STEP * 60)


@dataclass(frozen=True)
class Table:
    id: int
    label: str
    capacity: int


@dataclass(frozen=True)
class Reservation:
    id: str
    table: Table
    start_at: datetime
    end_at: datetime
    party_size: int
    guest_name: str
    phone: str | None
    status: str
