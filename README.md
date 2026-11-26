# Серверная часть сервиса резервирования столиков

API для одного ресторана. Гость может подобрать столик, оформить бронь, перенести или отменить визит. Сотруднику доступен журнал бронирований за день. Стек проекта — Python, FastAPI и SQLite.

## Запуск с нуля

Для Windows нужны Git, Python 3.13 с командой `py` и PowerShell. При первой установке зависимостей нужен доступ к PyPI. В папке, где будет храниться проект, выполните:

```powershell
git clone https://github.com/myrlykalo/table-reservation-api.git
cd table-reservation-api
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:BOOKING_ADMIN_KEY = [guid]::NewGuid().ToString()
.\.venv\Scripts\python.exe -m uvicorn reservation.api:app --host 127.0.0.1 --port 8000
```

При первом запуске сервер создаёт файл `.data/restaurant.sqlite3` и шесть столиков. По умолчанию ресторан работает с 12:00 до 23:00 по Москве. Схема запросов и примеры доступны в [документации API](http://127.0.0.1:8000/docs).

## Проверка

Во втором окне PowerShell из папки `table-reservation-api`:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
.\.venv\Scripts\python.exe demo.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`/health` возвращает статус `ok`. На свежей базе пример бронирует столик T06 на завтра в 19:00, повторяет заявку с тем же ключом, переносит визит на 19:15 и отменяет его. В конце столик снова появляется в поиске. Автотесты работают с отдельными временными базами. Чтобы остановить сервер, нажмите Ctrl+C в первом окне.
