#!/usr/bin/env python3
"""
DA Sales Watcher — pings your phone/PC when your DeviantArt earnings go up.

It fetches one URL (your earnings page or the JSON request behind it) using your
logged-in browser cookie, pulls a number out with a regex, and notifies you via
ntfy and/or Telegram when that number increases.

Run `python watcher.py --test` once to check the setup.
"""
import hashlib
import json
import logging
import os
import re
import sys
import time

import requests

# ---------- config (from environment / .env) ----------
DA_URL = os.environ.get("DA_URL", "").strip()
DA_COOKIE = os.environ.get("DA_COOKIE", "").strip()
DA_USER_AGENT = os.environ.get(
    "DA_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
).strip()
# Default: available balance + pending, as the /studio/earnings page shows them (both groups are added up).
VALUE_REGEX = os.environ.get("VALUE_REGEX", r"ableBalance\W+([\d.,]+)\W+pendingTransactions\W+([\d.,]+)")
INTERVAL = max(60, int(os.environ.get("INTERVAL_SECONDS", "300")))
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
COOKIE_FILE = os.environ.get("COOKIE_FILE", os.path.join(os.path.dirname(STATE_FILE) or ".", "cookies.json"))
REMIND_HOURS = float(os.environ.get("LOGIN_REMIND_HOURS", "6"))
CURRENCY = os.environ.get("CURRENCY_SYMBOL", "$")

NTFY_URL = os.environ.get("NTFY_URL", "").strip()          # e.g. https://ntfy.sh/da-sales-x8k2q
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()      # only for a protected self-hosted ntfy
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("da-watcher")


class SessionExpired(Exception):
    pass


class BotCheck(Exception):
    """DeviantArt's AWS WAF answered with a JavaScript challenge instead of the page."""
    pass


def url_candidates():
    """DA_URL first. Since 10 Nov 2026 DeviantArt puts an AWS WAF challenge on
    /studio/earnings; the rule is case-sensitive, so /STUDIO/earnings still serves
    the same page. Tried only when the first URL gets the challenge."""
    urls = [DA_URL]
    if "/studio/" in DA_URL:
        urls.append(DA_URL.replace("/studio/", "/STUDIO/", 1))
    return urls


def is_waf_challenge(r):
    return r.headers.get("x-amzn-waf-action", "").lower() == "challenge" or (r.status_code == 202 and not r.text)


# ---------- helpers ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def parse_number(raw):
    s = raw.strip().replace(" ", "")
    # handle both 1,234.56 and 1.234,56
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # a single comma followed by exactly 2 digits = decimal comma
        s = s.replace(",", ".") if re.fullmatch(r"\d+,\d{2}", s) else s.replace(",", "")
    return float(s)


def money(v):
    return f"{CURRENCY}{v:,.2f}"


def notify(title, message, priority="high", tags="moneybag"):
    """Send to every configured channel. Title must stay plain ASCII for ntfy headers."""
    sent = False
    if NTFY_URL:
        headers = {"Title": title, "Priority": priority, "Tags": tags}
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        try:
            r = requests.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=15)
            r.raise_for_status()
            sent = True
        except requests.RequestException as e:
            log.error("ntfy failed: %s", e)
    if TG_TOKEN and TG_CHAT:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": f"{title}\n{message}"},
                timeout=15,
            )
            r.raise_for_status()
            sent = True
        except requests.RequestException as e:
            log.error("Telegram failed: %s", e)
    if not sent:
        log.warning("No notification channel worked (check NTFY_URL / TELEGRAM_* settings)")
    return sent


# ---------- cookie jar: keeps DeviantArt's rotating login cookies fresh ----------
SEED_HASH = hashlib.sha256(DA_COOKIE.encode()).hexdigest()[:16]
jar = {}  # cookie name -> value; a refreshed cookie replaces the old one


def parse_cookie_header(header):
    out = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


def load_cookies():
    """Use the saved (refreshed) cookies if they came from the same .env cookie,
    otherwise start from the cookie in .env (e.g. after you paste a new one)."""
    global jar
    try:
        with open(COOKIE_FILE) as f:
            saved = json.load(f)
        if saved.get("seed") == SEED_HASH and saved.get("cookies"):
            jar = dict(saved["cookies"])
            log.info("Loaded %d saved cookies", len(jar))
            return
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    jar = parse_cookie_header(DA_COOKIE)
    log.info("Using cookie from .env")


def save_cookies():
    os.makedirs(os.path.dirname(COOKIE_FILE) or ".", exist_ok=True)
    tmp = COOKIE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seed": SEED_HASH, "cookies": jar}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, COOKIE_FILE)


def fetch():
    s = requests.Session()
    s.cookies.update(jar)  # sent on the request and kept across redirects
    for url in url_candidates():
        r = s.get(
            url,
            headers={
                "User-Agent": DA_USER_AGENT,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Referer": "https://www.deviantart.com/",
            },
            timeout=30,
            allow_redirects=True,
        )
        if not is_waf_challenge(r):
            break
    else:
        raise BotCheck(f"HTTP {r.status_code}, x-amzn-waf-action={r.headers.get('x-amzn-waf-action')} on {', '.join(url_candidates())}")
    if r.status_code in (401, 403) or "/users/login" in r.url:
        raise SessionExpired(f"HTTP {r.status_code}, landed on {r.url}")
    r.raise_for_status()
    # collect every cookie DeviantArt set on this request (including redirects), newest wins
    updates = {}
    for resp in list(r.history) + [r]:
        for c in resp.cookies:
            updates[c.name] = c.value
    changed = {k: v for k, v in updates.items() if jar.get(k) != v}
    if changed:
        jar.update(changed)
        save_cookies()
        log.info("DeviantArt refreshed %d cookie(s), saved: %s", len(changed), ", ".join(sorted(changed)))
    return r.text


def extract(text):
    m = re.search(VALUE_REGEX, text)
    if not m:
        return None
    return (sum(parse_number(g) for g in m.groups() if g) if m.groups() else parse_number(m.group(0)))


# ---------- modes ----------
def run_test():
    load_cookies()
    print(f"URL:   {DA_URL}")
    print(f"Regex: {VALUE_REGEX}")
    try:
        text = fetch()
    except SessionExpired as e:
        print(f"\n✗ Not logged in ({e}). Copy a fresh Cookie header into .env.")
        return 1
    except BotCheck as e:
        print(f"\n✗ DeviantArt bot check ({e}). The page needs a real browser now.")
        return 1
    except requests.RequestException as e:
        print(f"\n✗ Request failed: {e}")
        return 1
    value = extract(text)
    if value is None:
        print("\n✗ Page loaded, but the regex found nothing.")
        print("  First 1500 characters of the response, to help adjust VALUE_REGEX:\n")
        print(text[:1500])
        return 1
    print(f"\n✓ Found value: {money(value)}")
    ok = notify("DA watcher test", f"Setup works. Current value: {money(value)}", priority="default", tags="white_check_mark")
    print("✓ Test notification sent" if ok else "✗ Notification not sent — check NTFY_URL / TELEGRAM_*")
    return 0 if ok else 1


def run_loop():
    state = load_state()
    load_cookies()
    log.info("Watching %s every %ss", DA_URL, INTERVAL)
    while True:
        try:
            value = extract(fetch())
            if state.get("botcheck_alerted"):
                state["botcheck_alerted"] = False
                notify("DA watcher: page readable again", "The bot check is gone, watching for sales.", priority="default", tags="white_check_mark")
            if state.get("session_alerted"):
                state["session_alerted"] = False
                notify("DA watcher back online", "Logged in again, watching for sales.", priority="default", tags="white_check_mark")

            if value is None:
                log.warning("Value not found in response")
                if not state.get("parse_alerted"):
                    notify("DA watcher: can't read earnings",
                           "The page loaded but the number wasn't found. DeviantArt may have changed the page; adjust VALUE_REGEX.",
                           priority="default", tags="warning")
                    state["parse_alerted"] = True
            else:
                state["parse_alerted"] = False
                last = state.get("last_value")
                if last is None:
                    log.info("First reading: %s", money(value))
                    notify("DA watcher started", f"Current value: {money(value)}", priority="low", tags="eyes")
                elif value > last + 0.004:
                    diff = value - last
                    log.info("SALE +%s (now %s)", money(diff), money(value))
                    notify("New sale on DeviantArt!", f"+{money(diff)}  (total now {money(value)})")
                elif value < last - 0.004:
                    log.info("Value dropped %s -> %s (withdrawal/refund?)", money(last), money(value))
                    notify("DA earnings went down",
                           f"{money(last)} -> {money(value)}. A withdrawal or refund?",
                           priority="low", tags="arrow_down")
                else:
                    log.info("No change (%s)", money(value))
                state["last_value"] = value
            save_state(state)

        except SessionExpired as e:
            log.error("Session expired: %s", e)
            now = time.time()
            if not state.get("session_alerted") or now - state.get("session_alert_time", 0) > REMIND_HOURS * 3600:
                notify("DA watcher: login expired",
                       "Not seeing sales right now. Copy a fresh Cookie header from your browser into .env and recreate the container.",
                       priority="high", tags="warning")
                state["session_alerted"] = True
                state["session_alert_time"] = now
                save_state(state)
        except BotCheck as e:
            log.error("Bot check: %s", e)
            if not state.get("botcheck_alerted"):
                notify("DA watcher: DeviantArt bot check",
                       "The earnings page now asks for a browser check (AWS WAF challenge), so the number can't be read. "
                       "The regex is fine; the fetch method needs changing.",
                       priority="default", tags="warning")
                state["botcheck_alerted"] = True
                save_state(state)
        except requests.RequestException as e:
            log.error("Network/HTTP error: %s", e)  # temporary, just retry next round
        except Exception:
            log.exception("Unexpected error")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    missing = [k for k, v in {"DA_URL": DA_URL, "DA_COOKIE": DA_COOKIE}.items() if not v]
    if missing:
        sys.exit(f"Missing settings in .env: {', '.join(missing)}")
    if not NTFY_URL and not (TG_TOKEN and TG_CHAT):
        sys.exit("Set NTFY_URL or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env")
    sys.exit(run_test() if "--test" in sys.argv else run_loop())
