# Online Bus Ticket Booking — with confirmation SMS

A complete web application where customers search buses, pick seats on a seat map, pay, and receive their
**ticket confirmation by SMS** on their mobile number. Includes an admin panel for the bus operator.
Built with **Python (Flask)** and **SQL (SQLite)**.

## Booking flow

```
Search (from, to, date)  →  Choose seats + passenger details  →  Seats HELD for 10 min
        →  Pay  →  CONFIRMED  →  ticket page  +  SMS to the customer's mobile
```

| For customers | For the admin |
|---|---|
| Search by route and date, live seats-left count | Dashboard: bookings, revenue after refunds, failed-SMS alert |
| Visual seat map (available / selected / booked) | Schedule trips (clash check: a bus can't run two overlapping trips) |
| Up to 6 seats per booking, passenger name/age/gender | Passenger list per trip (printable) |
| 10-minute seat hold with countdown while paying | Add buses (seat layout is generated) and routes |
| Printable ticket with PNR | Search bookings by PNR / mobile / name |
| **Confirmation SMS** with PNR, route, time, seats, amount, boarding point | Cancel a booking (full refund) or a whole trip (everyone refunded + SMS) |
| "Resend SMS" (max 3 times) | **SMS log** with one-click retry of failed messages |
| Cancel online with a tiered refund; cancellation SMS | |
| Find booking again with PNR + mobile number | |

Also: a **reminder SMS** script (24 h before departure) you can run from cron.

---

## 1. Requirements

| Need | Details |
|---|---|
| Python | **3.9 or newer** (tested on 3.13) |
| Packages | `Flask` and `requests` — in `requirements.txt` |
| Database | **SQLite** — built into Python, nothing to install; one file `bus.db` |
| Browser | Any modern browser, desktop or phone |
| Real SMS (optional) | A **Fast2SMS** (India) or **Twilio** account. Without one the app runs in test mode: SMS is only *logged* |
| Payments | Demo payment page is included. For real money you must connect a gateway (see §6) |

## 2. Install & run

```bash
cd bus_booking
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

python seed.py                      # demo buses, routes, trips for the next 8 days
python app.py
```

* Customers: **http://127.0.0.1:5000**
* Admin: **http://127.0.0.1:5000/admin** — login `admin` / `admin123` (set `ADMIN_PASSWORD` before the first run).

`python seed.py --no-demo` creates only the admin login; add your own buses, routes and trips in the admin panel.
`python seed.py --reset` wipes the database and regenerates demo trips dated from today (it **deletes everything**).
If you skip `seed.py`, `python app.py` creates an empty database and the admin login by itself.

## 3. Try it in 2 minutes (test mode, no SMS account needed)

1. Open the site → search **Bengaluru → Mysuru**, today or tomorrow → **Select seats**.
2. Click seats, enter passenger details and **your mobile number** (any valid 10-digit number starting 6–9) → **Continue to payment**.
3. On the payment page click **Pay** (demo mode: nothing is charged) → ticket page appears.
4. Log in to `/admin` → **SMS log**: the confirmation message is there as *simulated*, exactly as a customer would receive it.

## 4. SMS: how the confirmation reaches the customer

When payment succeeds, the app sets the booking to *confirmed*, then calls `send_booking_sms()` which builds the text from a template,
sends it through the configured provider and writes the result to the `sms_log` table. **A failing SMS never breaks the booking** — the
customer still gets the ticket page, can press *Resend SMS*, and the admin can *Retry* from the SMS log.

Example message:

> Dear Ravi, your bus ticket is CONFIRMED. PNR K7M2QX9P: Bengaluru to Mysuru, 25-10-2026 6:30 AM, Swift Express. Seats 1A, 1B. Paid Rs.900. Boarding: Majestic Bus Stand, Gate 4. -SwiftBus

Other SMS sent automatically: cancellation (with refund amount), operator trip-cancelled (full refund), and the 24-hour reminder.

### Connect a real provider (set before `python app.py`)

```bash
# Fast2SMS (India)
export SMS_PROVIDER=fast2sms
export FAST2SMS_API_KEY=your_key

# or Twilio
export SMS_PROVIDER=twilio
export TWILIO_SID=ACxxxxxxxx
export TWILIO_TOKEN=xxxxxxxx
export TWILIO_FROM=+1234567890
export DEFAULT_COUNTRY_CODE=91

# default: nothing is sent, only logged as "simulated"
export SMS_PROVIDER=console
```
Windows PowerShell: `$env:SMS_PROVIDER="fast2sms"`. Then restart the app and check **Admin → SMS log**: each message shows
*sent* or *failed* with the gateway's reason.

**India (important):** transactional SMS needs a **DLT-registered sender ID and approved templates**; the text sent must match your
approved template. Edit `SMS_TEMPLATES` at the top of `app.py` accordingly. Gateway APIs change, so if sending fails check your
provider's documentation — the failure reason appears in the SMS log. Adding another gateway = one function in `sms.py`.

### Reminder SMS (optional)
`python send_reminders.py` texts every confirmed passenger whose bus leaves within 24 hours (once per booking).
Schedule hourly: `0 * * * * cd /path/to/bus_booking && venv/bin/python send_reminders.py` (Windows: Task Scheduler).
The scheduled job needs the same `SMS_*` environment variables.

## 5. Rules built in

* **No double booking.** Seat checks run inside a database write-lock **and** a unique SQL index
  (`idx_one_active_booking_per_seat` on `trip_id, seat_no WHERE active = 1`) makes it impossible for two customers to hold the same seat,
  even if they click at the same instant. (Tested with 8 simultaneous customers: exactly 1 wins, 7 get a friendly message.)
* **Seat hold.** Seats are reserved for 10 minutes (`HOLD_MINUTES`) while paying. Unpaid holds expire and the seats return to sale;
  late payments are refused. One mobile number may have at most 2 unpaid holds at a time (stops seat-hogging).
* **Cut-off.** No booking within 30 minutes of departure.
* **Refund policy** (edit `CANCEL_POLICY` in `app.py`): cancel ≥24 h before → 90%, ≥6 h → 50%, ≥2 h → 25%, closer → not cancellable online.
  Operator-cancelled trips and admin cancellations refund 100%. A booking can only be cancelled once (no double refund).
* **Privacy.** A ticket is visible only to the browser that booked it, or to someone who enters PNR **and** mobile number
  (guessing is rate-limited). Mobile numbers are masked on customer pages.
* Secure basics: CSRF protection on every form, SQL bound parameters, hashed admin password, login throttling.

> The refund amount is **recorded** and SMSed; moving the money back through your payment gateway is a step you must connect (see §6).

## 6. Before going live

1. **Payments.** The demo page marks bookings paid without charging. In `pay()` (`app.py`) replace the demo block with a real gateway
   (Razorpay, PayU, Stripe…): create the order, and confirm the booking **only** after verifying the gateway's signature/webhook on the server.
   Also issue refunds through the gateway when a booking is cancelled. Set `PAYMENT_MODE` to anything other than `demo` to hide the demo controls.
2. **Secrets.** Set `SECRET_KEY` (long random string), `ADMIN_PASSWORD`, and the SMS keys as environment variables.
3. **Server.** `python app.py` is for development. In production: `pip install waitress` then `waitress-serve --port=8000 app:app`, behind HTTPS (nginx/Caddy).
   SQLite handles a small operator well; for heavy traffic move to PostgreSQL/MySQL (the SQL is standard; `db.py` and the `ON CONFLICT`/index syntax need small changes).
4. **Time zone.** All times are the server's local time — run the server in your operating time zone.
5. **Backups.** Copy `bus.db` regularly. It contains customers' phone numbers — protect it.
6. **Single staff role.** Anyone with the admin login can do everything.

## 7. Settings (environment variables)

`SECRET_KEY`, `BRAND_NAME`, `ADMIN_USER`, `ADMIN_PASSWORD` (first run), `DB_PATH`, `HOST`, `PORT`,
`HOLD_MINUTES` (default 10), `SMS_PROVIDER`, `FAST2SMS_API_KEY`, `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM`,
`DEFAULT_COUNTRY_CODE`, `PAYMENT_MODE`.
To let phones on your network open the site: `HOST=0.0.0.0 python app.py`.

## 8. Project layout & database

```
bus_booking/
├── app.py             routes + business rules (booking, hold, pay, SMS, cancel, admin)
├── sms.py             SMS providers: console / fast2sms / twilio
├── db.py              SQLite helpers, seat-layout generator
├── schema.sql         all tables, the double-booking index, the v_trips view
├── seed.py            demo data        ├── send_reminders.py   24-hour reminder job
├── templates/         HTML (Jinja2)    └── static/style.css
```

```
routes ──< trips >── buses ──< bus_seats
              │
              └──< bookings ──< booking_seats   (UNIQUE active seat per trip)
                       └──< sms_log
users (admin)
```

`v_trips` is an SQL view joining trips, routes and buses and computing live seats-available; search, dashboard and admin all use it.

```sql
-- revenue per route
SELECT r.origin, r.destination, COUNT(*) AS bookings, SUM(b.total_amount - b.refund_amount) AS net
FROM bookings b JOIN trips t ON t.id = b.trip_id JOIN routes r ON r.id = t.route_id
WHERE b.status IN ('confirmed', 'cancelled') GROUP BY r.id ORDER BY net DESC;

-- SMS that failed and need attention
SELECT created_at, phone, kind, response FROM sms_log WHERE status = 'failed' ORDER BY id DESC;
```

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: flask` | Activate the virtual environment, `pip install -r requirements.txt` |
| No buses in search | Run `python seed.py --reset` (demo trips are dated from the day you seed), or add trips in Admin → Trips |
| SMS says **simulated** | `SMS_PROVIDER` not set (test mode). Set it (§4) and restart |
| SMS says **failed** | Open Admin → SMS log: the reason (missing key, DLT template mismatch, balance, bad number) is shown; fix and press Retry |
| "Database already has data" when seeding | Intentional protection. `--reset` wipes everything |
| Port 5000 busy (macOS) | `PORT=5001 python app.py` |

## 10. Ideas for next steps
Customer accounts and OTP login, e-ticket PDF/QR code, boarding/drop-point choice, discount coupons, multi-leg routes with stops,
WhatsApp notifications, driver app with passenger check-in, live bus tracking, PostgreSQL.
