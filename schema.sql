-- Online Bus Ticket Booking - SQLite schema (safe to run repeatedly)
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (            -- admin logins
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    reg_no         TEXT NOT NULL UNIQUE,
    bus_type       TEXT NOT NULL CHECK (bus_type IN ('AC Seater', 'Non-AC Seater', 'AC Sleeper')),
    total_seats    INTEGER NOT NULL CHECK (total_seats BETWEEN 1 AND 60),
    seats_per_row  INTEGER NOT NULL DEFAULT 4 CHECK (seats_per_row BETWEEN 2 AND 6),
    aisle_after    INTEGER NOT NULL DEFAULT 2             -- aisle drawn after this many seats in a row
);

CREATE TABLE IF NOT EXISTS bus_seats (        -- the physical seats of each bus: 1A, 1B, 2A ...
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    bus_id  INTEGER NOT NULL REFERENCES buses(id) ON DELETE CASCADE,
    seat_no TEXT NOT NULL,
    row_no  INTEGER NOT NULL,
    col_no  INTEGER NOT NULL,
    UNIQUE (bus_id, seat_no)
);

CREATE TABLE IF NOT EXISTS routes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    distance_km INTEGER,
    UNIQUE (origin, destination),
    CHECK (origin <> destination)
);

-- A trip = one bus running one route at one time. Times are 'YYYY-MM-DD HH:MM' (server local time).
CREATE TABLE IF NOT EXISTS trips (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bus_id         INTEGER NOT NULL REFERENCES buses(id),
    route_id       INTEGER NOT NULL REFERENCES routes(id),
    departure      TEXT NOT NULL,
    arrival        TEXT NOT NULL,
    fare           REAL NOT NULL CHECK (fare > 0),        -- per seat
    boarding_point TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled')),
    UNIQUE (bus_id, departure)
);
CREATE INDEX IF NOT EXISTS idx_trips_route_dep ON trips(route_id, departure);

-- pending = seats held while the customer pays (expires), confirmed = paid, cancelled, expired = hold ran out
CREATE TABLE IF NOT EXISTS bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pnr             TEXT NOT NULL UNIQUE,
    trip_id         INTEGER NOT NULL REFERENCES trips(id),
    customer_name   TEXT NOT NULL,
    mobile          TEXT NOT NULL,                        -- 10 digits, the confirmation SMS goes here
    email           TEXT,
    total_amount    REAL NOT NULL CHECK (total_amount > 0),
    status          TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
    payment_ref     TEXT,
    payment_mode    TEXT,
    created_at      TEXT NOT NULL,
    hold_expires_at TEXT,
    confirmed_at    TEXT,
    cancelled_at    TEXT,
    cancel_reason   TEXT,
    refund_amount   REAL NOT NULL DEFAULT 0,
    sms_resends     INTEGER NOT NULL DEFAULT 0,
    reminder_sent   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bookings_mobile ON bookings(mobile);
CREATE INDEX IF NOT EXISTS idx_bookings_trip   ON bookings(trip_id, status);

CREATE TABLE IF NOT EXISTS booking_seats (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id     INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    trip_id        INTEGER NOT NULL REFERENCES trips(id),
    seat_no        TEXT NOT NULL,
    passenger_name TEXT NOT NULL,
    passenger_age  INTEGER NOT NULL CHECK (passenger_age BETWEEN 1 AND 120),
    gender         TEXT NOT NULL CHECK (gender IN ('M', 'F', 'O')),
    active         INTEGER NOT NULL DEFAULT 1             -- 1 while the seat is held or booked
);
-- THE double-booking guard: the database itself refuses a second active booking of the same seat
-- on the same trip, even if two customers click at the very same moment.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_booking_per_seat
    ON booking_seats(trip_id, seat_no) WHERE active = 1;

CREATE TABLE IF NOT EXISTS sms_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL,
    phone      TEXT NOT NULL,
    message    TEXT NOT NULL,
    kind       TEXT NOT NULL,       -- confirmation | cancellation | trip_cancelled | reminder
    status     TEXT NOT NULL,       -- sent | failed | simulated
    response   TEXT,
    created_at TEXT NOT NULL
);

-- Trips with route/bus details and live seat availability (held + booked seats are unavailable).
DROP VIEW IF EXISTS v_trips;
CREATE VIEW v_trips AS
SELECT t.id AS trip_id, t.departure, t.arrival, t.fare, t.boarding_point, t.status AS trip_status,
       r.id AS route_id, r.origin, r.destination,
       b.id AS bus_id, b.name AS bus_name, b.bus_type, b.total_seats,
       b.total_seats - (SELECT COUNT(*) FROM booking_seats bs
                         WHERE bs.trip_id = t.id AND bs.active = 1) AS available
FROM trips t
JOIN routes r ON r.id = t.route_id
JOIN buses  b ON b.id = t.bus_id;
