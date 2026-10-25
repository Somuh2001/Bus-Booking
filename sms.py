"""SMS sending with pluggable providers.

Choose a provider with the SMS_PROVIDER environment variable:

    console   (default) nothing is sent; the message is only logged. Good for testing.
    fast2sms  Indian gateway. Needs FAST2SMS_API_KEY.
    twilio    Needs TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM.

send_sms() never raises, so an SMS problem can never break a booking.
It returns (status, response_text) where status is 'sent', 'simulated' or 'failed'.

India note: transactional SMS must go out with a DLT-registered sender ID and an approved
template; the message text has to match your template. Edit SMS_TEMPLATES in app.py.
"""
import os
import re

import requests

TIMEOUT = 10  # seconds


def normalize_phone(phone, country_code=None):
    digits = re.sub(r"\D", "", phone or "")
    cc = country_code or os.environ.get("DEFAULT_COUNTRY_CODE", "91")
    return cc + digits if len(digits) == 10 else digits


def send_sms(phone, message):
    provider = os.environ.get("SMS_PROVIDER", "console").lower()
    try:
        if provider == "console":
            return "simulated", "SMS_PROVIDER=console: message not actually sent"
        if provider == "fast2sms":
            return _fast2sms(phone, message)
        if provider == "twilio":
            return _twilio(phone, message)
        return "failed", f"Unknown SMS_PROVIDER '{provider}'"
    except requests.RequestException as exc:
        return "failed", f"Network error: {exc}"
    except Exception as exc:
        return "failed", f"Unexpected error: {exc}"


def _fast2sms(phone, message):
    key = os.environ.get("FAST2SMS_API_KEY")
    if not key:
        return "failed", "FAST2SMS_API_KEY is not set"
    resp = requests.post(
        "https://www.fast2sms.com/dev/bulkV2",
        headers={"authorization": key},
        data={"route": "q", "message": message, "language": "english", "flash": 0,
              "numbers": normalize_phone(phone)[-10:]},
        timeout=TIMEOUT,
    )
    try:
        body = resp.json()
    except ValueError:
        return "failed", f"HTTP {resp.status_code}: {resp.text[:200]}"
    return ("sent" if body.get("return") is True else "failed"), str(body)[:300]


def _twilio(phone, message):
    sid, token, sender = (os.environ.get(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"))
    if not (sid and token and sender):
        return "failed", "TWILIO_SID / TWILIO_TOKEN / TWILIO_FROM not all set"
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={"From": sender, "To": "+" + normalize_phone(phone), "Body": message},
        timeout=TIMEOUT,
    )
    if resp.status_code in (200, 201):
        return "sent", resp.text[:300]
    return "failed", f"HTTP {resp.status_code}: {resp.text[:300]}"
