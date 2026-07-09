#!/usr/bin/env python3
"""
hotel_watcher.py

Polls the onPeak Compass hotel page for a specific event and emails you
the moment Hilton Anaheim or Anaheim Marriott show availability instead
of "Sold Out".

WHY PLAYWRIGHT: compass.onpeak.com is a JS-rendered single-page app.
A plain requests.get() will return an near-empty shell, not the hotel
list, so we drive a real (headless) browser instead.

--------------------------------------------------------------------
ONE-TIME SETUP
--------------------------------------------------------------------
1. Install dependencies:
     pip3 install playwright
     python3 -m playwright install chromium

2. Set your email credentials as environment variables (don't hardcode
   passwords in the script). Easiest with Gmail + an App Password
   (myaccount.google.com/apppasswords -> requires 2FA enabled):

     export SMTP_USER="you@gmail.com"
     export SMTP_PASS="your16charapppassword"
     export TO_EMAIL="you@gmail.com"

   (Any SMTP provider works -- just adjust SMTP_SERVER/SMTP_PORT below
   if you're not using Gmail.)

3. First run: set HEADLESS=false and DEBUG=1 so a real browser window
   opens and you can watch it load, and so it dumps the matched hotel
   "card" HTML to debug_cards.html. This lets us confirm the detection
   logic actually matches how Compass marks a hotel as sold out.

     HEADLESS=false DEBUG=1 python3 hotel_watcher.py --once

4. Once it's reliably detecting sold-out vs available, run it for real:

     python3 hotel_watcher.py

   It will loop forever, checking every ~30 minutes (with a little
   random jitter), and email you only when something changes from
   sold-out -> available (so you're not spammed every cycle).

--------------------------------------------------------------------
RUNNING IT IN THE BACKGROUND (macOS/Linux)
--------------------------------------------------------------------
Simplest: leave a terminal tab open with:
     nohup python3 hotel_watcher.py > watcher.log 2>&1 &

To survive reboots/terminal closing on Mac, use a launchd job or just
a `screen`/`tmux` session. Happy to write a launchd plist if useful.
--------------------------------------------------------------------
"""

import os
import re
import sys
import time
import random
import smtplib
import argparse
import subprocess
import platform
from datetime import datetime
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# ----------------------------- CONFIG -------------------------------

EVENT_URL = "https://compass.onpeak.com/e/012607077/3#hotels"

# Text to look for. Keep these loose (lowercase, partial) since the
# page may render "Anaheim Marriott" vs "Marriott Anaheim" etc.
WATCHED_HOTELS = [
    "hilton anaheim",
    "anaheim marriott",
]

# Keywords that indicate NO availability. If a hotel's card contains
# none of these, we treat it as available and fire an alert.
SOLD_OUT_KEYWORDS = [
    "sold out",
    "no availability",
    "not available",
    "unavailable",
]

CHECK_INTERVAL_SECONDS = 30 * 60          # ~30 minutes
JITTER_SECONDS = 5 * 60                    # +/- up to 5 min, to avoid a
                                            # perfectly robotic pattern
STATE_FILE = "hotel_watcher_state.txt"     # remembers last known status
DEBUG_FILE = "debug_cards.html"

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
TO_EMAIL = os.environ.get("TO_EMAIL", SMTP_USER)

HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
DEBUG = os.environ.get("DEBUG", "0") == "1"

# "desktop" = macOS notification banner + sound, no setup needed.
# "email"   = requires SMTP_USER/SMTP_PASS above.
# "both"    = do both.
NOTIFY_METHOD = os.environ.get("NOTIFY_METHOD", "desktop")

# ---------------------------------------------------------------------


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def notify_desktop(subject, body):
    if platform.system() != "Darwin":
        log("NOTIFY_METHOD=desktop only works on macOS -- skipping, printing instead:")
        log(f"{subject}: {body}")
        return
    # Escape double quotes for AppleScript
    safe_body = body.replace('"', '\\"')
    safe_subject = subject.replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_subject}" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=True)
        log(f"Desktop notification shown: {subject}")
    except Exception as e:
        log(f"FAILED to show desktop notification: {e}")


def notify(subject, body):
    if NOTIFY_METHOD in ("desktop", "both"):
        notify_desktop(subject, body)
    if NOTIFY_METHOD in ("email", "both"):
        send_email(subject, body)


def send_email(subject, body):
    if not SMTP_USER or not SMTP_PASS:
        log("SMTP_USER/SMTP_PASS not set -- skipping email, printing instead:")
        log(f"SUBJECT: {subject}\n{body}")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = TO_EMAIL
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())
        log(f"Email sent to {TO_EMAIL}: {subject}")
    except Exception as e:
        log(f"FAILED to send email: {e}")


def load_last_state():
    if not os.path.exists(STATE_FILE):
        return {}
    state = {}
    with open(STATE_FILE) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                state[k] = v
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        for k, v in state.items():
            f.write(f"{k}={v}\n")


def find_hotel_card_text(full_text_blocks, hotel_key):
    """
    full_text_blocks: list of (element_text) strings, roughly one per
    visual "card" on the page (see extraction logic in check_once).
    Returns the block of text containing the hotel name, or None.
    """
    for block in full_text_blocks:
        if hotel_key in block.lower():
            return block
    return None


def check_once(playwright):
    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )
    page = context.new_page()
    log(f"Loading {EVENT_URL} ...")
    page.goto(EVENT_URL, wait_until="networkidle", timeout=60000)

    # Give the SPA a little extra time to finish rendering hotel cards
    page.wait_for_timeout(4000)

    # Heuristic: grab every element that looks like a "card" (has both
    # some text and a reasonable size) rather than relying on a class
    # name we haven't verified. We pull all leaf-ish containers' text.
    # This is intentionally broad -- refine with DEBUG=1 output.
    candidates = page.locator("div, li, article").all()
    blocks = []
    for el in candidates:
        try:
            txt = el.inner_text(timeout=500).strip()
        except Exception:
            continue
        if txt and 15 < len(txt) < 600:
            blocks.append(txt)

    if DEBUG:
        with open(DEBUG_FILE, "w") as f:
            f.write(page.content())
        log(f"Wrote full rendered HTML to {DEBUG_FILE} for inspection.")

    results = {}
    for hotel in WATCHED_HOTELS:
        card_text = find_hotel_card_text(blocks, hotel)
        if card_text is None:
            log(f"WARNING: could not find any card mentioning '{hotel}'. "
                f"Page structure may differ from expected -- check {DEBUG_FILE}.")
            results[hotel] = "not_found"
            continue
        lower = card_text.lower()
        is_sold_out = any(kw in lower for kw in SOLD_OUT_KEYWORDS)
        results[hotel] = "sold_out" if is_sold_out else "available"
        if DEBUG:
            log(f"--- Card text for '{hotel}' ---\n{card_text}\n---")

    browser.close()
    return results


def run_once():
    with sync_playwright() as p:
        return check_once(p)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                         help="Run a single check and exit (for testing).")
    args = parser.parse_args()

    last_state = load_last_state()

    while True:
        try:
            results = run_once()
        except Exception as e:
            log(f"Check failed with error: {e}")
            results = {}

        for hotel, status in results.items():
            prev = last_state.get(hotel)
            log(f"{hotel}: {status} (previous: {prev})")

            if status == "available" and prev != "available":
                notify(
                    subject=f"Room opened up: {hotel.title()}",
                    body=(
                        f"{hotel.title()} now shows availability on Compass.\n\n"
                        f"Book it here: {EVENT_URL}\n\n"
                        f"Checked at {datetime.now().isoformat(timespec='seconds')}"
                    ),
                )
            last_state[hotel] = status

        save_state(last_state)

        if args.once:
            break

        sleep_for = CHECK_INTERVAL_SECONDS + random.randint(-JITTER_SECONDS, JITTER_SECONDS)
        log(f"Sleeping {sleep_for // 60} min until next check...")
        time.sleep(max(60, sleep_for))


if __name__ == "__main__":
    main()
