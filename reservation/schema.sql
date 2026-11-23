BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS dining_tables (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE COLLATE NOCASE,
    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 8)
);
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY NOT NULL,
    table_id INTEGER NOT NULL REFERENCES dining_tables(id),
    start_at INTEGER NOT NULL CHECK (start_at % 900 = 0),
    end_at INTEGER NOT NULL CHECK (end_at % 900 = 0 AND end_at - start_at BETWEEN 1800 AND 10800),
    party_size INTEGER NOT NULL CHECK (party_size BETWEEN 1 AND 8),
    guest_name TEXT NOT NULL,
    phone TEXT,
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    owner_digest TEXT UNIQUE NOT NULL,
    request_digest TEXT NOT NULL,
    UNIQUE (id, table_id)
);
CREATE INDEX IF NOT EXISTS reservations_day ON reservations(start_at, table_id);
CREATE TABLE IF NOT EXISTS occupied_slots (
    table_id INTEGER NOT NULL,
    slot_start INTEGER NOT NULL CHECK (slot_start % 900 = 0),
    reservation_id TEXT NOT NULL,
    PRIMARY KEY (table_id, slot_start),
    FOREIGN KEY (reservation_id, table_id) REFERENCES reservations(id, table_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS slots_by_reservation ON occupied_slots(reservation_id);
INSERT INTO dining_tables(label, capacity)
VALUES ('T01', 2), ('T02', 2), ('T03', 4), ('T04', 4), ('T05', 6), ('T06', 8)
ON CONFLICT(label) DO NOTHING;
COMMIT;
