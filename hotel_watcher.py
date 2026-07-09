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

EVENT_URL = "https://compass.onpeak.com/e/012607077/5#hotels"

# Exact card titles as they render on the page (case-insensitive match).
# Use the FULL exact title -- e.g. "Anaheim Marriott Hotel", not just
# "Anaheim Marriott" -- so we don't accidentally match a different
# nearby property like "Anaheim Marriott Suites".
WATCHED_HOTELS = [
    "Hilton Anaheim",
    "Anaheim Marriott Hotel",
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
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    to_email = os.environ.get("TO_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        log("SMTP_USER/SMTP_PASS not set -- skipping email, printing instead:")
        log(f"SUBJECT: {subject}\n{body}")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [to_email], msg.as_string())
        log(f"Email sent to {to_email}: {subject}")
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


CARD_LOOKUP_JS = """
(titleText) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node, titleEl = null;
    const target = titleText.trim().toUpperCase();
    while (node = walker.nextNode()) {
        if (node.children.length === 0) {
            const t = (node.textContent || '').trim().toUpperCase();
            if (t === target) { titleEl = node; break; }
        }
    }
    if (!titleEl) return null;
    let el = titleEl;
    for (let i = 0; i < 8 && el; i++) {
        const txt = el.innerText || '';
        // A "card" is the ancestor that also contains the price ($)
        // and either an availability badge or a rooms-available line.
        if (txt.includes('$') && /available/i.test(txt)) {
            return txt;
        }
        el = el.parentElement;
    }
    return titleEl.parentElement ? titleEl.parentElement.innerText : titleEl.innerText;
}
"""


def find_hotel_card_text(page, hotel_title):
    try:
        return page.evaluate(CARD_LOOKUP_JS, hotel_title)
    except Exception as e:
        log(f"JS lookup failed for '{hotel_title}': {e}")
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
    page.wait_for_timeout(6000)

    # The hotel list may be a scrollable/virtualized grid -- scroll down
    # a few times so all cards (including ones further down, like a
    # "Hilton Anaheim" that sorts after several "A..." hotels) get
    # rendered into the DOM before we search it.
    for _ in range(6):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(500)
    page.wait_for_timeout(1000)

    body_text = page.inner_text("body") if page.locator("body").count() else ""

    # Always write a lightweight debug summary (safe to commit, small).
    with open("debug_summary.txt", "w") as f:
        f.write(f"Checked at: {datetime.now().isoformat()}\n")
        f.write(f"Page title: {page.title()}\n")
        f.write(f"'anaheim' appears in body text: {'anaheim' in body_text.lower()}\n")
        f.write(f"'hilton' appears in body text: {'hilton' in body_text.lower()}\n")
        f.write(f"'marriott' appears in body text: {'marriott' in body_text.lower()}\n")
        f.write(f"'unavailable' appears in body text: {'unavailable' in body_text.lower()}\n")
        for hotel in WATCHED_HOTELS:
            card = find_hotel_card_text(page, hotel)
            f.write(f"\n--- Card lookup for '{hotel}' ---\n")
            f.write(repr(card)[:500] if card else "NOT FOUND")
            f.write("\n")

    if DEBUG:
        with open(DEBUG_FILE, "w") as f:
            f.write(page.content())
        log(f"Wrote full rendered HTML to {DEBUG_FILE} for inspection.")

    results = {}
    for hotel in WATCHED_HOTELS:
        card_text = find_hotel_card_text(page, hotel)
        if card_text is None:
            log(f"WARNING: could not find card for '{hotel}'. Check debug_summary.txt.")
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
