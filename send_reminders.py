"""Send a departure-reminder SMS to every confirmed passenger whose bus leaves within the next N hours.

    python send_reminders.py          # next 24 hours
    python send_reminders.py 12       # next 12 hours

Each booking is reminded only once. Schedule it every hour, e.g. with cron:
    0 * * * *  cd /path/to/bus_booking && /path/to/venv/bin/python send_reminders.py
(Windows: Task Scheduler running the same command.)  Remember to set the SMS_* environment
variables for the scheduled job as well.
"""
import sys

import app as appmod


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    with appmod.app.app_context():
        sent, failed = appmod.send_due_reminders(hours)
    print(f"Reminders: {sent} sent, {failed} failed (failed ones are retried on the next run).")


if __name__ == "__main__":
    main()
