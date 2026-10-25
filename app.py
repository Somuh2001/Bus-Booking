"""Online Bus Ticket Booking - customers book seats, a confirmation SMS goes to their mobile.

Run:  python app.py      (then open http://127.0.0.1:5000)
"""
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db as dbm
import sms as smsm

BRAND = os.environ.get("BRAND_NAME", "SH BUS")
HOLD_MINUTES = int(os.environ.get("HOLD_MINUTES", "10"))   # seats are held this long while paying
MAX_SEATS = 6                                               # per booking
CUTOFF_MINUTES = 30                                         # no booking this close to departure
MAX_SMS_RESENDS = 3                                         # customer "resend SMS" clicks per booking
MAX_PENDING_PER_MOBILE = 2                                  # stops one number from hogging seats
PAYMENT_MODE = os.environ.get("PAYMENT_MODE", "demo")       # 'demo' = simulated payment page

# (at least this many hours before departure, refund %). Closer than the last row: cannot cancel.
CANCEL_POLICY = [(24, 90), (6, 50), (2, 25)]

DT_FMT = "%Y-%m-%d %H:%M"
TS_FMT = "%Y-%m-%d %H:%M:%S"

# Keep these short and match your DLT-approved templates if you send SMS in India.
SMS_TEMPLATES = {
    "confirmation": ("Dear {name}, your bus ticket is CONFIRMED. PNR {pnr}: {origin} to {destination}, "
                     "{date} {time}, {bus}. Seats {seats}. Paid Rs.{amount}. Boarding: {boarding}. -{brand}"),
    "cancellation": ("Dear {name}, booking {pnr} ({origin} to {destination}, {date}) is CANCELLED. "
                     "Refund Rs.{refund} will reach you in 5-7 working days. -{brand}"),
    "trip_cancelled": ("Dear {name}, your trip {origin} to {destination} on {date} (PNR {pnr}) was cancelled "
                       "by the operator. Full refund Rs.{refund} initiated. Sorry for the trouble. -{brand}"),
    "reminder": ("Reminder: bus {origin} to {destination} departs {date} {time} from {boarding}. "
                 "PNR {pnr}, seats {seats}. Please reach 15 min early. -{brand}"),
}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
app.teardown_appcontext(dbm.close_db)


class SeatTaken(Exception):
    pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def q(sql, args=(), one=False):
    rows = dbm.get_db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def ex(sql, args=()):
    db = dbm.get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur


@contextmanager
def transaction():
    """BEGIN IMMEDIATE takes the write lock up front, so two bookings can't interleave."""
    db = dbm.get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise


def now():
    return datetime.now()


def now_str():
    return now().strftime(TS_FMT)


def parse_dt(s):
    return datetime.strptime(s[:16], DT_FMT)


def fmt_date(s):
    return parse_dt(s).strftime("%d-%m-%Y")


def fmt_time(s):
    return parse_dt(s).strftime("%I:%M %p").lstrip("0")


def clean_mobile(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if re.fullmatch(r"[6-9]\d{9}", d) else None


def mask_mobile(m):
    return "XXXXXX" + m[-4:]


PNR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I to avoid mix-ups


def new_pnr(db):
    while True:
        p = "".join(secrets.choice(PNR_ALPHABET) for _ in range(8))
        if not db.execute("SELECT 1 FROM bookings WHERE pnr = ?", (p,)).fetchone():
            return p


def expire_holds(db=None):
    """Release seats of pending bookings whose hold ran out."""
    own = db is None
    db = db or dbm.get_db()
    t = now_str()
    db.execute("UPDATE booking_seats SET active = 0 WHERE active = 1 AND booking_id IN "
               "(SELECT id FROM bookings WHERE status = 'pending' AND hold_expires_at < ?)", (t,))
    db.execute("UPDATE bookings SET status = 'expired' WHERE status = 'pending' AND hold_expires_at < ?", (t,))
    if own:
        db.commit()


def get_trip(trip_id):
    return q("SELECT * FROM v_trips WHERE trip_id = ?", (trip_id,), one=True)


def bookable(trip):
    return (trip["trip_status"] == "scheduled"
            and parse_dt(trip["departure"]) - now() > timedelta(minutes=CUTOFF_MINUTES))


BOOKING_SQL = """SELECT b.*, t.departure, t.arrival, t.boarding_point, t.status AS trip_status,
                        r.origin, r.destination, bu.name AS bus_name, bu.bus_type
                 FROM bookings b JOIN trips t ON t.id = b.trip_id
                 JOIN routes r ON r.id = t.route_id JOIN buses bu ON bu.id = t.bus_id"""


def load_booking(pnr):
    return q(BOOKING_SQL + " WHERE b.pnr = ?", ((pnr or "").upper(),), one=True)


def booking_seats(booking_id):
    return q("SELECT * FROM booking_seats WHERE booking_id = ? ORDER BY id", (booking_id,))


def can_view(pnr):
    return bool(session.get("admin_id")) or pnr in session.get("pnrs", [])


def remember_pnr(pnr):
    lst = list(session.get("pnrs", []))
    if pnr not in lst:
        lst.append(pnr)
    session["pnrs"] = lst[-20:]


# ---- SMS --------------------------------------------------------------------
def build_sms(kind, b):
    seats = ", ".join(s["seat_no"] for s in booking_seats(b["id"]))
    return SMS_TEMPLATES[kind].format(
        name=b["customer_name"].split()[0], pnr=b["pnr"], origin=b["origin"], destination=b["destination"],
        date=fmt_date(b["departure"]), time=fmt_time(b["departure"]), bus=b["bus_name"], seats=seats,
        amount=f"{b['total_amount']:,.0f}", refund=f"{b['refund_amount']:,.0f}",
        boarding=b["boarding_point"] or "bus station", brand=BRAND)


def send_booking_sms(b, kind):
    """Send + log one SMS about a booking. Returns True if it went (or was simulated)."""
    message = build_sms(kind, b)
    status, response = smsm.send_sms(b["mobile"], message)
    ex("INSERT INTO sms_log(booking_id, phone, message, kind, status, response, created_at) "
       "VALUES (?, ?, ?, ?, ?, ?, ?)", (b["id"], b["mobile"], message, kind, status, response, now_str()))
    return status in ("sent", "simulated")


# ---- cancellation -----------------------------------------------------------
def refund_quote(b):
    """(percent, amount) the customer would get now, or None when cancelling is no longer allowed."""
    if b["status"] != "confirmed" or b["trip_status"] != "scheduled":
        return None
    hours = (parse_dt(b["departure"]) - now()).total_seconds() / 3600
    for min_hours, pct in CANCEL_POLICY:
        if hours >= min_hours:
            return pct, round(b["total_amount"] * pct / 100, 2)
    return None


def do_cancel(b, refund_amount, reason, sms_kind="cancellation"):
    """Cancel a confirmed booking, free its seats, SMS the customer. Raises ValueError if not confirmed."""
    with transaction() as db:
        cur = db.execute("UPDATE bookings SET status = 'cancelled', cancelled_at = ?, cancel_reason = ?, "
                         "refund_amount = ? WHERE id = ? AND status = 'confirmed'",
                         (now_str(), reason, refund_amount, b["id"]))
        if cur.rowcount != 1:
            raise ValueError("Booking is not in a cancellable state.")
        db.execute("UPDATE booking_seats SET active = 0 WHERE booking_id = ?", (b["id"],))
    return send_booking_sms(load_booking(b["pnr"]), sms_kind)


# --------------------------------------------------------------------------
# Security: CSRF, login throttling, template helpers
# --------------------------------------------------------------------------
FAILED = {}  # key -> (count, last_time)


def blocked(key, limit=5, window=300):
    count, last = FAILED.get(key, (0, 0))
    if time.time() - last >= window:
        FAILED.pop(key, None)
        return False
    return count >= limit


def record_fail(key):
    count, _ = FAILED.get(key, (0, 0))
    FAILED[key] = (count + 1, time.time())


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Invalid or missing CSRF token. Reload the page and try again.")


app.jinja_env.globals["csrf_token"] = csrf_token


@app.context_processor
def inject_globals():
    return {"brand": BRAND, "today": date.today().isoformat(), "max_seats": MAX_SEATS,
            "hold_minutes": HOLD_MINUTES, "in_admin": request.path.startswith("/admin"),
            "is_admin": bool(session.get("admin_id")), "payment_mode": PAYMENT_MODE}


@app.template_filter("inr")
def inr_filter(v):
    v = v or 0
    return "₹{:,.2f}".format(v) if abs(v - round(v)) > 0.004 else "₹{:,.0f}".format(v)


@app.template_filter("dt")
def dt_filter(v):
    try:
        return parse_dt(v).strftime("%a, %d %b %Y, %I:%M %p")
    except (TypeError, ValueError):
        return v or ""


@app.template_filter("dshort")
def dshort_filter(v):
    try:
        return parse_dt(v).strftime("%d %b, %I:%M %p")
    except (TypeError, ValueError):
        return v or ""


@app.template_filter("tm")
def tm_filter(v):
    try:
        return fmt_time(v)
    except (TypeError, ValueError):
        return v or ""


@app.template_filter("dur")
def dur_filter(dep, arr):
    mins = int((parse_dt(arr) - parse_dt(dep)).total_seconds() // 60)
    return f"{mins // 60}h {mins % 60:02d}m"


app.jinja_env.filters["mask"] = mask_mobile


# --------------------------------------------------------------------------
# Customer: search
# --------------------------------------------------------------------------
@app.route("/")
def home():
    expire_holds()
    cities = [r["c"] for r in q("SELECT origin AS c FROM routes UNION SELECT destination FROM routes ORDER BY c")]
    popular = q("""SELECT origin, destination, MIN(fare) AS from_fare, COUNT(*) AS trips FROM v_trips
                   WHERE trip_status = 'scheduled' AND departure > ? GROUP BY route_id
                   ORDER BY trips DESC LIMIT 6""", (now().strftime(DT_FMT),))
    return render_template("home.html", cities=cities, popular=popular)


@app.route("/search")
def search():
    expire_holds()
    origin = request.args.get("origin", "").strip()
    destination = request.args.get("destination", "").strip()
    day = request.args.get("date", "").strip() or date.today().isoformat()
    cities = [r["c"] for r in q("SELECT origin AS c FROM routes UNION SELECT destination FROM routes ORDER BY c")]
    trips, error = [], None
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        d, error = None, "Choose a valid travel date."
    if not error and (not origin or not destination):
        error = "Choose where you are travelling from and to."
    elif not error and origin.lower() == destination.lower():
        error = "From and To must be different cities."
    elif not error and d < date.today():
        error = "That date has already passed."
    if not error:
        cutoff = (now() + timedelta(minutes=CUTOFF_MINUTES)).strftime(DT_FMT)
        trips = q("""SELECT * FROM v_trips WHERE lower(origin) = lower(?) AND lower(destination) = lower(?)
                     AND substr(departure, 1, 10) = ? AND trip_status = 'scheduled' AND departure > ?
                     ORDER BY departure""", (origin, destination, day, cutoff))
    else:
        flash(error, "error")
    prev_day = (d - timedelta(days=1)).isoformat() if d and d > date.today() else None
    next_day = (d + timedelta(days=1)).isoformat() if d else None
    return render_template("search.html", trips=trips, origin=origin, destination=destination, day=day,
                           searched=not error, cities=cities, prev_day=prev_day, next_day=next_day)


# --------------------------------------------------------------------------
# Customer: seat map, hold, pay
# --------------------------------------------------------------------------
def render_trip(trip, form=None, status=200):
    bus = q("SELECT * FROM buses WHERE id = ?", (trip["bus_id"],), one=True)
    seats = q("SELECT * FROM bus_seats WHERE bus_id = ? ORDER BY row_no, col_no", (trip["bus_id"],))
    taken = {r["seat_no"] for r in q("SELECT seat_no FROM booking_seats WHERE trip_id = ? AND active = 1",
                                     (trip["trip_id"],))}
    grid = {}
    for s in seats:
        grid.setdefault(s["row_no"], [None] * bus["seats_per_row"])[s["col_no"] - 1] = s
    form = form or {}
    prefill = {"seats": form.get("seats", []), "passengers": form.get("passengers", {})}
    return render_template("trip.html", trip=trip, bus=bus, grid=[grid[k] for k in sorted(grid)],
                           taken=taken, form=form, prefill=prefill, can_book=bookable(trip)), status


@app.route("/trip/<int:trip_id>")
def trip_page(trip_id):
    trip = get_trip(trip_id) or abort(404)
    expire_holds()
    trip = get_trip(trip_id)
    if not bookable(trip):
        flash("Booking is closed for this trip.", "error")
    return render_trip(trip)


@app.route("/trip/<int:trip_id>/book", methods=["POST"])
def book(trip_id):
    trip = get_trip(trip_id) or abort(404)
    f = request.form
    selected = f.getlist("seats")
    form = {"name": f.get("name", "").strip(), "mobile": f.get("mobile", "").strip(),
            "email": f.get("email", "").strip(), "seats": selected, "passengers": {}}
    if not bookable(trip):
        flash("Booking is closed for this trip.", "error")
        return render_trip(trip, form, 400)

    errors = []
    valid = {r["seat_no"] for r in q("SELECT seat_no FROM bus_seats WHERE bus_id = ?", (trip["bus_id"],))}
    if not selected:
        errors.append("Select at least one seat.")
    elif len(selected) > MAX_SEATS:
        errors.append(f"You can book at most {MAX_SEATS} seats at a time.")
    elif len(set(selected)) != len(selected) or not set(selected) <= valid:
        errors.append("Invalid seat selection.")
    if not 2 <= len(form["name"]) <= 60:
        errors.append("Enter the contact person's name (2-60 characters).")
    mobile = clean_mobile(form["mobile"])
    if not mobile:
        errors.append("Enter a valid 10-digit Indian mobile number - your ticket SMS is sent there.")
    if form["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", form["email"]):
        errors.append("Email address looks invalid.")
    if not errors:
        for seat in selected:
            p = {"name": f.get(f"pname_{seat}", "").strip(), "age": f.get(f"page_{seat}", "").strip(),
                 "gender": f.get(f"pgender_{seat}", "")}
            form["passengers"][seat] = p
            if not 1 <= len(p["name"]) <= 60:
                errors.append(f"Seat {seat}: enter the passenger's name.")
            if not (p["age"].isdigit() and 1 <= int(p["age"]) <= 120):
                errors.append(f"Seat {seat}: age must be between 1 and 120.")
            if p["gender"] not in ("M", "F", "O"):
                errors.append(f"Seat {seat}: choose a gender.")
    if not errors:
        pending = q("SELECT COUNT(*) AS c FROM bookings WHERE mobile = ? AND status = 'pending' "
                    "AND hold_expires_at >= ?", (mobile, now_str()), one=True)["c"]
        if pending >= MAX_PENDING_PER_MOBILE:
            errors.append("This mobile number already has unpaid bookings in progress. "
                          "Complete or wait for them to expire first.")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_trip(trip, form, 400)

    try:
        with transaction() as db:
            expire_holds(db)
            taken = {r["seat_no"] for r in db.execute(
                "SELECT seat_no FROM booking_seats WHERE trip_id = ? AND active = 1", (trip_id,))}
            if taken & set(selected):
                raise SeatTaken()
            pnr = new_pnr(db)
            now_s = now_str()
            cur = db.execute(
                "INSERT INTO bookings(pnr, trip_id, customer_name, mobile, email, total_amount, status, "
                "created_at, hold_expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (pnr, trip_id, form["name"], mobile, form["email"] or None, trip["fare"] * len(selected), now_s,
                 (now() + timedelta(minutes=HOLD_MINUTES)).strftime(TS_FMT)))
            for seat in selected:
                p = form["passengers"][seat]
                db.execute("INSERT INTO booking_seats(booking_id, trip_id, seat_no, passenger_name, "
                           "passenger_age, gender) VALUES (?, ?, ?, ?, ?, ?)",
                           (cur.lastrowid, trip_id, seat, p["name"], int(p["age"]), p["gender"]))
    except (SeatTaken, sqlite3.IntegrityError):
        # IntegrityError = the unique index caught a simultaneous booking of the same seat
        flash("Sorry - one or more of those seats was just taken by another customer. Please pick again.", "error")
        form["seats"] = []
        return render_trip(trip, form, 409)
    remember_pnr(pnr)
    return redirect(url_for("pay", pnr=pnr))


@app.route("/pay/<pnr>", methods=["GET", "POST"])
def pay(pnr):
    expire_holds()
    b = load_booking(pnr) or abort(404)
    if not can_view(b["pnr"]):
        abort(404)
    if b["status"] == "confirmed":
        return redirect(url_for("ticket", pnr=b["pnr"]))
    if b["status"] != "pending":
        flash("This seat hold has expired. Please select your seats again.", "error")
        return redirect(url_for("trip_page", trip_id=b["trip_id"]))
    left = int((datetime.strptime(b["hold_expires_at"], TS_FMT) - now()).total_seconds())
    if request.method == "POST":
        mode = request.form.get("mode", "UPI")
        if mode not in ("UPI", "Card", "Net Banking"):
            flash("Choose a payment method.", "error")
        elif PAYMENT_MODE == "demo" and request.form.get("demo_result") == "fail":
            flash("Payment failed (simulated). Your seats are still held - try again.", "error")
        else:
            # --- A real gateway (Razorpay, PayU, Stripe...) goes here: create an order before this page,
            # --- then verify the gateway's signature/webhook on the SERVER before confirming.
            with transaction() as db:
                expire_holds(db)
                cur = db.execute(
                    "UPDATE bookings SET status = 'confirmed', confirmed_at = ?, payment_mode = ?, "
                    "payment_ref = ? WHERE id = ? AND status = 'pending'",
                    (now_str(), mode, "DEMO-" + secrets.token_hex(5).upper(), b["id"]))
                paid_ok = cur.rowcount == 1
            if not paid_ok:
                flash("Your seat hold expired before payment completed. Please book again.", "error")
                return redirect(url_for("trip_page", trip_id=b["trip_id"]))
            sent = send_booking_sms(load_booking(b["pnr"]), "confirmation")
            flash(f"Booking confirmed! SMS sent to {mask_mobile(b['mobile'])}." if sent else
                  "Booking confirmed! We could not send the SMS right now - your ticket is below, "
                  "and you can use 'Resend SMS'.", "ok" if sent else "warn")
            return redirect(url_for("ticket", pnr=b["pnr"]))
    return render_template("pay.html", b=b, seats=booking_seats(b["id"]), left=max(left, 0))


# --------------------------------------------------------------------------
# Customer: ticket, manage, cancel
# --------------------------------------------------------------------------
@app.route("/ticket/<pnr>")
def ticket(pnr):
    b = load_booking(pnr) or abort(404)
    if not can_view(b["pnr"]):
        return redirect(url_for("manage"))
    if b["status"] in ("pending", "expired"):
        return redirect(url_for("pay", pnr=b["pnr"]))
    return render_template("ticket.html", b=b, seats=booking_seats(b["id"]), quote=refund_quote(b),
                           resends_left=MAX_SMS_RESENDS - b["sms_resends"])


@app.route("/ticket/<pnr>/resend-sms", methods=["POST"])
def resend_sms(pnr):
    b = load_booking(pnr) or abort(404)
    if not can_view(b["pnr"]):
        abort(404)
    if b["status"] != "confirmed":
        flash("Only confirmed bookings can be re-sent.", "error")
    elif b["sms_resends"] >= MAX_SMS_RESENDS:
        flash("SMS resend limit reached. Please contact support.", "error")
    else:
        ex("UPDATE bookings SET sms_resends = sms_resends + 1 WHERE id = ?", (b["id"],))
        ok = send_booking_sms(b, "confirmation")
        flash(f"SMS sent to {mask_mobile(b['mobile'])}." if ok else "SMS could not be sent right now.",
              "ok" if ok else "error")
    return redirect(url_for("ticket", pnr=b["pnr"]))


@app.route("/manage", methods=["GET", "POST"])
def manage():
    if request.method == "POST":
        key = "manage:" + (request.remote_addr or "?")
        if blocked(key):
            flash("Too many attempts. Try again in 5 minutes.", "error")
            return render_template("manage.html"), 429
        pnr = request.form.get("pnr", "").strip().upper()
        mobile = clean_mobile(request.form.get("mobile", ""))
        b = load_booking(pnr)
        if b and mobile and secrets.compare_digest(b["mobile"], mobile):
            FAILED.pop(key, None)
            remember_pnr(b["pnr"])
            return redirect(url_for("ticket", pnr=b["pnr"]))
        record_fail(key)
        flash("We could not find a booking with that PNR and mobile number.", "error")
    return render_template("manage.html")


@app.route("/cancel/<pnr>", methods=["GET", "POST"])
def cancel(pnr):
    b = load_booking(pnr) or abort(404)
    if not can_view(b["pnr"]):
        abort(404)
    quote = refund_quote(b)
    if request.method == "POST":
        if not quote:
            flash("This booking can no longer be cancelled.", "error")
        else:
            try:
                sent = do_cancel(b, quote[1], "Cancelled by customer")
                flash(f"Booking cancelled. Refund of ₹{quote[1]:,.0f} initiated."
                      + (" Confirmation SMS sent." if sent else ""), "ok")
            except ValueError as exc:
                flash(str(exc), "error")
        return redirect(url_for("ticket", pnr=b["pnr"]))
    return render_template("cancel.html", b=b, quote=quote, seats=booking_seats(b["id"]),
                           policy=CANCEL_POLICY)


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
def admin_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login"))
        return view(*a, **kw)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        key = "admin:" + (request.remote_addr or "?")
        if blocked(key):
            flash("Too many failed attempts. Try again in 5 minutes.", "error")
            return render_template("admin_login.html"), 429
        user = q("SELECT * FROM users WHERE username = ?", (request.form.get("username", "").strip(),), one=True)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            FAILED.pop(key, None)
            keep = session.get("pnrs", [])
            session.clear()
            session["pnrs"] = keep
            session["admin_id"] = user["id"]
            return redirect(url_for("admin_dashboard"))
        record_fail(key)
        flash("Wrong username or password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_id", None)
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    expire_holds()
    today = date.today().isoformat()
    stats = q("""SELECT
        COALESCE(SUM(CASE WHEN status = 'confirmed' AND substr(confirmed_at, 1, 10) = ? THEN 1 END), 0) AS today_bookings,
        COALESCE(SUM(CASE WHEN status = 'confirmed' THEN total_amount END), 0) AS gross,
        COALESCE(SUM(CASE WHEN status = 'cancelled' THEN refund_amount END), 0) AS refunds,
        COALESCE(SUM(CASE WHEN status = 'cancelled' THEN total_amount - refund_amount END), 0) AS kept,
        COALESCE(SUM(CASE WHEN status = 'confirmed' THEN 1 END), 0) AS confirmed
        FROM bookings""", (today,), one=True)
    net = stats["gross"] + stats["kept"]
    upcoming = q("""SELECT * FROM v_trips WHERE trip_status = 'scheduled' AND departure > ?
                    ORDER BY departure LIMIT 12""", (now().strftime(DT_FMT),))
    recent = q("""SELECT b.pnr, b.customer_name, b.mobile, b.total_amount, b.status, b.created_at,
                         r.origin, r.destination FROM bookings b JOIN trips t ON t.id = b.trip_id
                  JOIN routes r ON r.id = t.route_id WHERE b.status != 'pending'
                  ORDER BY b.id DESC LIMIT 8""")
    sms_failed = q("SELECT COUNT(*) AS c FROM sms_log WHERE status = 'failed' AND created_at >= ?",
                   ((now() - timedelta(days=7)).strftime(TS_FMT),), one=True)["c"]
    return render_template("admin_dashboard.html", stats=stats, net=net, upcoming=upcoming,
                           recent=recent, sms_failed=sms_failed)


@app.route("/admin/trips", methods=["GET", "POST"])
@admin_required
def admin_trips():
    if request.method == "POST":
        f = request.form
        try:
            bus_id, route_id = f.get("bus_id", type=int), f.get("route_id", type=int)
            dep = datetime.strptime(f.get("departure", ""), "%Y-%m-%dT%H:%M")
            arr = datetime.strptime(f.get("arrival", ""), "%Y-%m-%dT%H:%M")
            fare = float(f.get("fare", ""))
        except ValueError:
            flash("Fill in all fields with valid values.", "error")
            return redirect(url_for("admin_trips"))
        if not (bus_id and route_id and q("SELECT 1 FROM buses WHERE id = ?", (bus_id,), one=True)
                and q("SELECT 1 FROM routes WHERE id = ?", (route_id,), one=True)):
            flash("Choose a bus and a route.", "error")
        elif arr <= dep:
            flash("Arrival must be after departure.", "error")
        elif dep <= now():
            flash("Departure must be in the future.", "error")
        elif fare <= 0:
            flash("Fare must be above zero.", "error")
        elif q("SELECT 1 FROM trips WHERE bus_id = ? AND status = 'scheduled' AND departure < ? AND arrival > ?",
               (bus_id, arr.strftime(DT_FMT), dep.strftime(DT_FMT)), one=True):
            flash("That bus already has a trip overlapping this time.", "error")
        else:
            ex("INSERT INTO trips(bus_id, route_id, departure, arrival, fare, boarding_point) VALUES (?,?,?,?,?,?)",
               (bus_id, route_id, dep.strftime(DT_FMT), arr.strftime(DT_FMT), fare,
                f.get("boarding_point", "").strip()))
            flash("Trip added.", "ok")
        return redirect(url_for("admin_trips"))
    show = request.args.get("show", "upcoming")
    sql, args = "SELECT * FROM v_trips", []
    if show == "upcoming":
        sql += " WHERE departure > ?"
        args.append(now().strftime(DT_FMT))
    sql += " ORDER BY departure" + (" DESC LIMIT 200" if show == "all" else " LIMIT 200")
    return render_template("admin_trips.html", trips=q(sql, args), show=show,
                           buses=q("SELECT * FROM buses ORDER BY name"),
                           routes=q("SELECT * FROM routes ORDER BY origin, destination"))


@app.route("/admin/trips/<int:trip_id>")
@admin_required
def admin_trip(trip_id):
    expire_holds()
    trip = get_trip(trip_id) or abort(404)
    manifest = q("""SELECT bs.seat_no, bs.passenger_name, bs.passenger_age, bs.gender, b.pnr, b.customer_name, b.mobile
                    FROM booking_seats bs JOIN bookings b ON b.id = bs.booking_id
                    WHERE bs.trip_id = ? AND bs.active = 1 AND b.status = 'confirmed'
                    ORDER BY CAST(bs.seat_no AS INTEGER), bs.seat_no""", (trip_id,))
    held = q("SELECT COUNT(*) AS c FROM booking_seats bs JOIN bookings b ON b.id = bs.booking_id "
             "WHERE bs.trip_id = ? AND bs.active = 1 AND b.status = 'pending'", (trip_id,), one=True)["c"]
    return render_template("admin_trip.html", trip=trip, manifest=manifest, held=held,
                           future=parse_dt(trip["departure"]) > now())


@app.route("/admin/trips/<int:trip_id>/cancel", methods=["POST"])
@admin_required
def admin_cancel_trip(trip_id):
    trip = get_trip(trip_id) or abort(404)
    if trip["trip_status"] != "scheduled" or parse_dt(trip["departure"]) <= now():
        flash("Only upcoming scheduled trips can be cancelled.", "error")
        return redirect(url_for("admin_trip", trip_id=trip_id))
    with transaction() as db:
        db.execute("UPDATE trips SET status = 'cancelled' WHERE id = ?", (trip_id,))
        db.execute("UPDATE booking_seats SET active = 0 WHERE active = 1 AND booking_id IN "
                   "(SELECT id FROM bookings WHERE trip_id = ? AND status = 'pending')", (trip_id,))
        db.execute("UPDATE bookings SET status = 'expired' WHERE trip_id = ? AND status = 'pending'", (trip_id,))
    done = failed_sms = 0
    for b in q(BOOKING_SQL + " WHERE b.trip_id = ? AND b.status = 'confirmed'", (trip_id,)):
        try:
            if not do_cancel(b, b["total_amount"], "Trip cancelled by operator", "trip_cancelled"):
                failed_sms += 1
            done += 1
        except ValueError:
            pass
    flash(f"Trip cancelled. {done} booking(s) refunded in full and customers notified"
          + (f" ({failed_sms} SMS failed - retry from the SMS log)." if failed_sms else "."), "ok")
    return redirect(url_for("admin_trip", trip_id=trip_id))


@app.route("/admin/fleet", methods=["GET", "POST"])
@admin_required
def admin_fleet():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        try:
            if action == "bus":
                total, per_row = f.get("total_seats", type=int), f.get("seats_per_row", type=int)
                aisle = f.get("aisle_after", type=int)
                btype = f.get("bus_type")
                if (not f.get("name", "").strip() or not f.get("reg_no", "").strip() or not total or not per_row
                        or aisle is None or not 1 <= total <= 60 or not 2 <= per_row <= 6
                        or not 0 <= aisle <= per_row
                        or btype not in ("AC Seater", "Non-AC Seater", "AC Sleeper")):
                    flash("Fill in the bus details correctly (1-60 seats, 2-6 per row).", "error")
                else:
                    with transaction() as db:
                        cur = db.execute("INSERT INTO buses(name, reg_no, bus_type, total_seats, seats_per_row, "
                                         "aisle_after) VALUES (?,?,?,?,?,?)",
                                         (f["name"].strip(), f["reg_no"].strip().upper(), btype, total, per_row, aisle))
                        db.executemany("INSERT INTO bus_seats(bus_id, seat_no, row_no, col_no) VALUES (?,?,?,?)",
                                       [(cur.lastrowid, *s) for s in dbm.generate_seats(total, per_row)])
                    flash("Bus added with its seat layout.", "ok")
            elif action == "route":
                dist = f.get("distance_km", type=int)
                o, d = f.get("origin", "").strip(), f.get("destination", "").strip()
                if not o or not d or o.lower() == d.lower():
                    flash("Enter two different cities.", "error")
                else:
                    ex("INSERT INTO routes(origin, destination, distance_km) VALUES (?,?,?)", (o, d, dist))
                    flash("Route added.", "ok")
        except sqlite3.IntegrityError:
            flash("That bus registration or route already exists.", "error")
        return redirect(url_for("admin_fleet"))
    return render_template("admin_fleet.html", buses=q("SELECT * FROM buses ORDER BY name"),
                           routes=q("SELECT * FROM routes ORDER BY origin, destination"))


@app.route("/admin/bookings")
@admin_required
def admin_bookings():
    expire_holds()
    term = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    day = request.args.get("date", "").strip()
    sql, args = BOOKING_SQL + " WHERE 1 = 1", []
    if term:
        sql += " AND (b.pnr LIKE ? OR b.mobile LIKE ? OR b.customer_name LIKE ?)"
        args += [f"%{term.upper() if len(term) == 8 else term}%"] + [f"%{term}%"] * 2
    if status in ("pending", "confirmed", "cancelled", "expired"):
        sql += " AND b.status = ?"
        args.append(status)
    if day:
        sql += " AND substr(t.departure, 1, 10) = ?"
        args.append(day)
    sql += " ORDER BY b.id DESC LIMIT 300"
    return render_template("admin_bookings.html", rows=q(sql, args), term=term, status=status, day=day)


@app.route("/admin/bookings/<pnr>")
@admin_required
def admin_booking(pnr):
    b = load_booking(pnr) or abort(404)
    logs = q("SELECT * FROM sms_log WHERE booking_id = ? ORDER BY id DESC", (b["id"],))
    return render_template("admin_booking.html", b=b, seats=booking_seats(b["id"]), logs=logs)


@app.route("/admin/bookings/<pnr>/cancel", methods=["POST"])
@admin_required
def admin_cancel_booking(pnr):
    b = load_booking(pnr) or abort(404)
    try:
        sent = do_cancel(b, b["total_amount"], "Cancelled by admin (full refund)")
        flash("Booking cancelled with a full refund." + (" Customer notified by SMS." if sent else
              " The SMS failed - retry from the SMS log."), "ok" if sent else "warn")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin_booking", pnr=b["pnr"]))


@app.route("/admin/bookings/<pnr>/resend", methods=["POST"])
@admin_required
def admin_resend(pnr):
    b = load_booking(pnr) or abort(404)
    kind = {"confirmed": "confirmation", "cancelled": "cancellation"}.get(b["status"])
    if not kind:
        flash("Nothing to send for this booking status.", "error")
    else:
        ok = send_booking_sms(b, kind)
        flash("SMS sent." if ok else "SMS failed - see the log below.", "ok" if ok else "error")
    return redirect(url_for("admin_booking", pnr=b["pnr"]))


@app.route("/admin/sms")
@admin_required
def admin_sms():
    status = request.args.get("status", "")
    sql = ("SELECT l.*, b.pnr FROM sms_log l LEFT JOIN bookings b ON b.id = l.booking_id")
    args = []
    if status in ("sent", "failed", "simulated"):
        sql += " WHERE l.status = ?"
        args.append(status)
    sql += " ORDER BY l.id DESC LIMIT 200"
    return render_template("admin_sms.html", rows=q(sql, args), status=status,
                           provider=os.environ.get("SMS_PROVIDER", "console").lower())


@app.route("/admin/sms/<int:log_id>/retry", methods=["POST"])
@admin_required
def admin_sms_retry(log_id):
    log = q("SELECT * FROM sms_log WHERE id = ?", (log_id,), one=True) or abort(404)
    status, response = smsm.send_sms(log["phone"], log["message"])
    ex("INSERT INTO sms_log(booking_id, phone, message, kind, status, response, created_at) VALUES (?,?,?,?,?,?,?)",
       (log["booking_id"], log["phone"], log["message"], log["kind"], status, response, now_str()))
    flash("SMS re-sent." if status != "failed" else f"Still failing: {response}", "ok" if status != "failed" else "error")
    return redirect(url_for("admin_sms"))


# --------------------------------------------------------------------------
# Reminders (called by send_reminders.py, e.g. from cron every hour)
# --------------------------------------------------------------------------
def send_due_reminders(hours_ahead=24):
    """SMS a reminder once to every confirmed passenger whose bus leaves within `hours_ahead` hours."""
    expire_holds()
    lo, hi = now().strftime(DT_FMT), (now() + timedelta(hours=hours_ahead)).strftime(DT_FMT)
    sent = failed = 0
    for b in q(BOOKING_SQL + " WHERE b.status = 'confirmed' AND b.reminder_sent = 0 AND t.status = 'scheduled' "
               "AND t.departure > ? AND t.departure <= ?", (lo, hi)):
        if send_booking_sms(b, "reminder"):
            ex("UPDATE bookings SET reminder_sent = 1 WHERE id = ?", (b["id"],))
            sent += 1
        else:
            failed += 1
    return sent, failed


# --------------------------------------------------------------------------
# Start-up
# --------------------------------------------------------------------------
def bootstrap():
    dbm.init_db()
    conn = dbm.connect()
    try:
        if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            user = os.environ.get("ADMIN_USER", "admin")
            pw = os.environ.get("ADMIN_PASSWORD", "admin123")
            conn.execute("INSERT INTO users(username, password_hash) VALUES (?, ?)",
                         (user, generate_password_hash(pw)))
            conn.commit()
            print(f"* First run: created admin '{user}'. Set ADMIN_PASSWORD (or change it) before going live.")
    finally:
        conn.close()


bootstrap()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
