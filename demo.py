"""Run against a local server. Only synthetic guest data are created."""
import argparse
from datetime import datetime, timedelta
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from reservation.domain import MOSCOW

BASE = "http://127.0.0.1:8000"


def request(method, route, *, body=None, key=None, params=None, staff=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if staff:
        headers["X-Admin-Key"] = staff
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    url = BASE + route + (("?" + urlencode(params)) if params else "")
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=8) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def main():
    tomorrow = (datetime.now(MOSCOW) + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    visit = {"start_at": tomorrow.isoformat(), "duration_minutes": 90, "party_size": 8}
    code, options = request("GET", "/api/availability", params=visit)
    print("Свободные столы на завтра в 19:00:", code, options)
    if code != 200 or not options:
        raise SystemExit("Для примера нужен свободный стол на восемь. Используйте отдельную демонстрационную базу.")
    key = str(uuid4())
    body = {**visit, "guest_name": "Учебный гость"}
    code, booking = request("POST", "/api/reservations", body=body, key=key)
    if code != 201:
        raise SystemExit(f"Создание не выполнено: {code} {booking}")
    route = "/api/reservations/" + booking["id"]
    print("Создание:", code, booking["table"]["label"], booking["id"])
    try:
        code, repeat = request("POST", "/api/reservations", body=body, key=key)
        print("Повтор исходной заявки:", code, "тот же ID:", repeat.get("id") == booking["id"])
        moved = {**visit, "start_at": (tomorrow + timedelta(minutes=15)).isoformat()}
        code, updated = request("PUT", route + "/visit", body=moved, key=key)
        print("Перенос:", code, updated.get("start_at", updated.get("detail")))
        if admin := os.getenv("BOOKING_ADMIN_KEY"):
            code, rows = request("GET", "/api/admin/reservations", params={"day": tomorrow.date()}, staff=admin)
            print("Журнал сотрудника:", code, "записей:", len(rows) if code == 200 else "—")
    finally:
        code, closed = request("DELETE", route, key=key)
        print("Отмена:", code, closed.get("status"))
        if code != 200:
            raise SystemExit("Отмена не выполнена.")
    code, free = request("GET", "/api/availability", params=visit)
    print("Стол снова свободен:", code == 200 and booking["table"] in free)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Демонстрация бронирования в локальном ресторане.")
    parser.add_argument("--port", type=int, default=8000, help="Порт локального сервера (по умолчанию 8000).")
    port = parser.parse_args().port
    if not 1 <= port <= 65535:
        parser.error("Номер порта должен быть в диапазоне 1–65535.")
    BASE = f"http://127.0.0.1:{port}"
    main()
