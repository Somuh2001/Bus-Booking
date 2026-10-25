"""Create the database with buses, routes and upcoming trips.

    python seed.py             demo data: 4 buses, 6 routes, trips for the next 7 days
    python seed.py --no-demo   only the admin login (add buses/routes/trips in the admin panel)
    python seed.py --reset     delete the existing bus.db first (DESTROYS ALL DATA)

Admin login: ADMIN_USER / ADMIN_PASSWORD environment variables, default admin / admin123.
Run it again any time with --reset to get fresh trips dated from today.
"""
import os
import sys
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import db as dbm

BUSES = [
    ("Swift Express", "KA01AB1234", "AC Seater", 40, 4, 2),
    ("Night Rider", "KA05CD5678", "AC Sleeper", 30, 3, 1),
    ("City Link", "KA09EF9012", "Non-AC Seater", 44, 4, 2),
    ("Coastal Queen", "KA19GH3456", "AC Seater", 36, 4, 2),
]
# origin, destination, km, hours, fare, boarding point
ROUTES = [
    ("Bengaluru", "Mysuru", 150, 3.5, 450, "Majestic Bus Stand, Gate 4"),
    ("Mysuru", "Bengaluru", 150, 3.5, 450, "Mysuru Central Bus Stand"),
    ("Bengaluru", "Mangaluru", 350, 8, 900, "Majestic Bus Stand, Gate 7"),
    ("Mangaluru", "Bengaluru", 350, 8, 900, "KSRTC Bus Stand, Mangaluru"),
    ("Bengaluru", "Hyderabad", 570, 10, 1400, "Madiwala Boarding Point"),
    ("Hyderabad", "Bengaluru", 570, 10, 1400, "Gachibowli Boarding Point"),
]
DEPARTURES = {  # route index -> list of (hour, minute, bus index)
    0: [(6, 30, 0), (14, 0, 2), (21, 30, 0)], 1: [(7, 0, 2), (16, 30, 0)],
    2: [(22, 0, 1), (21, 0, 3)], 3: [(21, 30, 1), (22, 30, 3)],
    4: [(21, 0, 1)], 5: [(20, 30, 1)],
}
DAYS = 7


def main():
    args = set(sys.argv[1:])
    path = dbm.db_path()
    if "--reset" in args and os.path.exists(path):
        os.remove(path)
        for ext in ("-wal", "-shm"):
            if os.path.exists(path + ext):
                os.remove(path + ext)
        print(f"Deleted {path}")
    dbm.init_db()
    conn = dbm.connect()
    try:
        if conn.execute("SELECT COUNT(*) AS c FROM buses").fetchone()["c"]:
            sys.exit("Database already has data. Run `python seed.py --reset` to start over (deletes everything).")
        user = os.environ.get("ADMIN_USER", "admin")
        pw = os.environ.get("ADMIN_PASSWORD", "admin123")
        conn.execute("INSERT OR IGNORE INTO users(username, password_hash) VALUES (?, ?)",
                     (user, generate_password_hash(pw)))
        trips = 0
        if "--no-demo" not in args:
            bus_ids = []
            for name, reg, btype, total, per_row, aisle in BUSES:
                cur = conn.execute("INSERT INTO buses(name, reg_no, bus_type, total_seats, seats_per_row, aisle_after) "
                                   "VALUES (?,?,?,?,?,?)", (name, reg, btype, total, per_row, aisle))
                conn.executemany("INSERT INTO bus_seats(bus_id, seat_no, row_no, col_no) VALUES (?,?,?,?)",
                                 [(cur.lastrowid, *s) for s in dbm.generate_seats(total, per_row)])
                bus_ids.append(cur.lastrowid)
            route_ids = []
            for o, d, km, _, _, _ in ROUTES:
                route_ids.append(conn.execute("INSERT INTO routes(origin, destination, distance_km) VALUES (?,?,?)",
                                              (o, d, km)).lastrowid)
            today = datetime.now().replace(second=0, microsecond=0)
            for day in range(DAYS + 1):
                for ri, slots in DEPARTURES.items():
                    _, _, _, hours, fare, boarding = ROUTES[ri]
                    for hh, mm, bi in slots:
                        dep = (today + timedelta(days=day)).replace(hour=hh, minute=mm)
                        if dep <= today + timedelta(hours=1):
                            continue  # don't create trips that are already (almost) gone
                        arr = dep + timedelta(hours=hours)
                        conn.execute("INSERT INTO trips(bus_id, route_id, departure, arrival, fare, boarding_point) "
                                     "VALUES (?,?,?,?,?,?)",
                                     (bus_ids[bi], route_ids[ri], dep.strftime("%Y-%m-%d %H:%M"),
                                      arr.strftime("%Y-%m-%d %H:%M"), fare, boarding))
                        trips += 1
        conn.commit()
    finally:
        conn.close()
    print(f"Database ready at {path}")
    print(f"  demo trips : {trips}")
    print(f"  admin login: {user} / {pw}   (admin panel: /admin)")


if __name__ == "__main__":
    main()
