"""HTTP adapter and application composition."""
from contextlib import asynccontextmanager
from datetime import date, datetime
import hmac
import os
from pathlib import Path
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from .domain import Conflict, InvalidVisit, MissingReservation, StoreBusy, Visit
from .service import BookingService
from .sqlite_store import SQLiteStore


def explicit_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-5][0-9])", value
    ):
        raise ValueError("Ожидается дата и время с секундами и часовым смещением.")
    return value


Timestamp = Annotated[AwareDatetime, BeforeValidator(explicit_timestamp)]
Size = Annotated[int, Field(ge=1, le=8)]
Minutes = Annotated[int, Field(ge=30, le=180, multiple_of=15)]
JsonIntSize = Annotated[int, Field(strict=True, ge=1, le=8)]
JsonMinutes = Annotated[int, Field(strict=True, ge=30, le=180, multiple_of=15)]


class VisitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    start_at: Timestamp
    duration_minutes: JsonMinutes
    party_size: JsonIntSize

    def visit(self):
        return Visit(self.start_at, self.duration_minutes, self.party_size)


class BookingInput(VisitInput):
    guest_name: str = Field(min_length=1, max_length=60)
    phone: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")


class NewTable(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(pattern=r"^[A-Za-z0-9-]{1,12}$")
    capacity: JsonIntSize


class TableView(BaseModel):
    id: int
    label: str
    capacity: int


class BookingView(BaseModel):
    id: UUID
    table: TableView
    start_at: datetime
    end_at: datetime
    party_size: int
    guest_name: str
    phone: str | None
    status: Literal["confirmed", "cancelled"]


bearer = HTTPBearer(auto_error=False, scheme_name="GuestBookingKey",
                    description="Секретный UUIDv4. Сгенерируйте один отдельный ключ на каждую бронь.")
staff_header = APIKeyHeader(name="X-Admin-Key", auto_error=False, scheme_name="StaffKey")


def guest_key(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]):
    if credentials:
        try:
            key = UUID(credentials.credentials)
            if key.version == 4:
                return str(key)
        except ValueError:
            pass
    raise HTTPException(401, "Нужен гостевой ключ в формате UUIDv4.", headers={"WWW-Authenticate": "Bearer"})


Guest = Annotated[str, Depends(guest_key)]


def create_app(db_path: Path | None = None, admin_key: str | None = None, clock=None) -> FastAPI:
    store = SQLiteStore(db_path or Path(os.getenv("BOOKING_DB", ".data/restaurant.sqlite3")))
    service = BookingService(store, clock)
    staff_secret = admin_key if admin_key is not None else os.getenv("BOOKING_ADMIN_KEY")

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield

    app = FastAPI(title="Резервирование столиков", version="1.0.0", lifespan=lifespan,
                  description="Один ресторан, часы работы 12:00–23:00 МСК. Шаг бронирования 15 минут.")

    def require_staff(key: Annotated[str | None, Security(staff_header)]):
        if not staff_secret:
            raise HTTPException(503, "Администратор не настроил служебный ключ.")
        if not key or not hmac.compare_digest(key.encode(), staff_secret.encode()):
            raise HTTPException(403, "Требуется действующий ключ сотрудника.")

    @app.middleware("http")
    async def cache_policy(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def application_error(request, exc):
        codes = {InvalidVisit: 422, Conflict: 409, MissingReservation: 404, StoreBusy: 503}
        headers = {"Retry-After": "1"} if isinstance(exc, StoreBusy) else {}
        return JSONResponse({"detail": str(exc)}, status_code=codes[type(exc)], headers=headers)

    for error in (InvalidVisit, Conflict, MissingReservation, StoreBusy):
        app.add_exception_handler(error, application_error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        issues = [{"loc": item["loc"], "msg": item["msg"]} for item in exc.errors()]
        return JSONResponse({"detail": issues}, status_code=422)

    @app.get("/health", tags=["Сервис"])
    def health():
        return {"status": "ok"}

    @app.get("/api/availability", response_model=list[TableView], tags=["Гость"],
             summary="Проверить свободные столики")
    def availability(start_at: Annotated[Timestamp, Query()], duration_minutes: Minutes, party_size: Size):
        return service.availability(Visit(start_at, duration_minutes, party_size))

    @app.post("/api/reservations", response_model=BookingView, status_code=201, tags=["Гость"],
              responses={200: {"model": BookingView, "description": "Повтор исходной заявки"},
                         409: {"description": "Нет места или ключ использован с другими данными"}},
              summary="Подтвердить бронирование")
    def create(data: BookingInput, key: Guest, response: Response):
        item, created = service.create(key, data.visit(), data.guest_name, data.phone)
        response.status_code = 201 if created else 200
        response.headers["Location"] = f"/api/reservations/{item.id}"
        return item

    @app.get("/api/reservations/{reservation_id}", response_model=BookingView, tags=["Гость"],
             summary="Открыть свою бронь")
    def get(reservation_id: UUID, key: Guest):
        return service.find(str(reservation_id), key)

    @app.put("/api/reservations/{reservation_id}/visit", response_model=BookingView, tags=["Гость"],
             summary="Перенести визит или изменить размер группы")
    def move(reservation_id: UUID, data: VisitInput, key: Guest):
        return service.reschedule(str(reservation_id), key, data.visit())

    @app.delete("/api/reservations/{reservation_id}", response_model=BookingView, tags=["Гость"],
                summary="Отменить бронь")
    def cancel(reservation_id: UUID, key: Guest):
        return service.cancel(str(reservation_id), key)

    @app.get("/api/admin/reservations", response_model=list[BookingView], tags=["Сотрудник"],
             dependencies=[Depends(require_staff)], summary="Получить журнал за местную дату")
    def daily(day: date):
        return service.daily_book(day)

    @app.post("/api/admin/tables", response_model=TableView, status_code=201, tags=["Сотрудник"],
              dependencies=[Depends(require_staff)], summary="Добавить столик")
    def add_table(data: NewTable):
        return service.add_table(data.label.upper(), data.capacity)

    return app


app = create_app()
