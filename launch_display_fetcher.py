"""
Rocket Launch Tracker — Fetcher + Display Edition
Raspberry Pi Zero 2 W | Python 3.13
Version 1.0.0
Date: June 25, 2026
Created by Matthew Sarvas

This script is for the DEDICATED FETCHER Pi only.
It fetches launch data from Launch Library 2, saves it to GitHub as launches.json,
and drives its own 6 LCD displays.

All other display Pis run rocket_launch_tracker.py which reads from GitHub instead.

Display layout:
    3x 16x2 LCDs (0x24, 0x23, 0x22) — Launch Site / Country
    3x 20x4 LCDs (0x27, 0x26, 0x25) — Mission / Vehicle / Date / Time (PST)

Wiring (same for all 6 displays — shared I2C bus):
    VCC → Pi Pin 2  (5V)
    GND → Pi Pin 6  (GND)
    SDA → Pi Pin 3  (GPIO2)
    SCL → Pi Pin 5  (GPIO3)

Toggle button wiring:
    One leg → Pi Pin 13 (GPIO27)
    Other leg → Pi Pin 9 (GND)
    (Internal pull-up resistor is used — no external resistor needed)

Install dependencies:
    pip install requests RPLCD smbus2 lgpio PyGithub --break-system-packages

Setup:
    1. Create a GitHub personal access token with repo write permissions
    2. Save it to ~/github_token.txt
    3. Create a new public GitHub repo called launch_display_data
    4. Set PI_USER and GITHUB_USERNAME below
    5. Run: python3 launch_display_fetcher.py
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import requests

try:
    from RPLCD.i2c import CharLCD
    LCD_AVAILABLE = True
except ImportError:
    LCD_AVAILABLE = False
    print("[WARNING] RPLCD not installed — terminal-only mode.")

try:
    import lgpio
    GPIO_AVAILABLE = True
    _gpio_handle = None
except ImportError:
    GPIO_AVAILABLE = False
    print("[WARNING] lgpio not installed — button toggle disabled.")

BUTTON_LAST_LEVEL = 1

# ── Configuration ─────────────────────────────────────────────────────────────

LCD_16x2_CONFIGS = [
    {"address": 0x24, "slot": 0},
    {"address": 0x23, "slot": 1},
    {"address": 0x22, "slot": 2},
]

LCD_20x4_CONFIGS = [
    {"address": 0x27, "slot": 0},
    {"address": 0x26, "slot": 1},
    {"address": 0x25, "slot": 2},
]

TOTAL_SLOTS           = 3
FETCH_COUNT           = 20
API_BASE              = "https://ll.thespacedevs.com/2.0.0"
REFRESH_INTERVAL_ANON = 360   # anonymous: 15 req/hour → 1 per 6 min
REFRESH_INTERVAL_AUTH = 60    # authenticated: 60 req/hour → 1 per 60s
BUTTON_PIN            = 27

# ── User configuration ────────────────────────────────────────────────────────
# !! UPDATE THESE to match your setup !!
PI_USER          = "pi"
GITHUB_USERNAME  = "mbsarvas"
GITHUB_DATA_REPO = "launch-display-private"
GITHUB_DATA_FILE = "launches.json"
GITHUB_TOKEN_FILE = f"/home/{PI_USER}/github_token.txt"
API_KEY_FILE      = f"/home/{PI_USER}/ll2_api_key.txt"
SCRIPT_PATH       = f"/home/{PI_USER}/launch-display-private/launch_display_fetcher.py"

# ── Auto-update settings ──────────────────────────────────────────────────────
SCRIPT_VERSION  = "1.0.0"
GITHUB_RAW_URL  = "https://raw.githubusercontent.com/mbsarvas/launch-display-private/main/launch_display_fetcher.py"
UPDATE_INTERVAL = 86400   # 24 hours


# ── GitHub token ──────────────────────────────────────────────────────────────

def load_github_token() -> str | None:
    """Load the GitHub personal access token from file."""
    try:
        with open(GITHUB_TOKEN_FILE, "r") as f:
            token = f.read().strip()
            if token:
                return token
    except FileNotFoundError:
        print(f"[WARNING] GitHub token file not found: {GITHUB_TOKEN_FILE}")
    except Exception as e:
        print(f"[WARNING] Could not read GitHub token: {e}")
    return None


# ── API key ───────────────────────────────────────────────────────────────────

def load_api_key() -> str | None:
    """Load the Launch Library 2 API key from file."""
    try:
        with open(API_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[WARNING] Could not read API key: {e}")
    return None


# ── GitHub data push ──────────────────────────────────────────────────────────

def push_to_github(launches: list[dict]) -> bool:
    """
    Push the launch data as launches.json to the GitHub data repo.
    Returns True on success, False on failure.
    """
    token = load_github_token()
    if not token:
        print("[GITHUB] No token found — skipping GitHub push.")
        return False

    url     = f"https://api.github.com/repos/{GITHUB_USERNAME}/{GITHUB_DATA_REPO}/contents/{GITHUB_DATA_FILE}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Build the payload — store raw parsed launches plus a timestamp
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "launches": launches,
    }
    content_b64 = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()

    # Check if the file already exists (need its SHA to update it)
    sha = None
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            sha = resp.json().get("sha")
    except Exception:
        pass

    # Push the file
    body = {
        "message": f"Update launches.json — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "content": content_b64,
    }
    if sha:
        body["sha"] = sha

    try:
        resp = requests.put(url, headers=headers, json=body, timeout=15)
        if resp.status_code in (200, 201):
            print(f"[GITHUB] Successfully pushed {len(launches)} launches to {GITHUB_DATA_REPO}.")
            return True
        else:
            print(f"[GITHUB] Push failed: HTTP {resp.status_code} — {resp.text[:100]}")
            return False
    except Exception as e:
        print(f"[GITHUB] Push error: {e}")
        return False


# ── Auto-updater ──────────────────────────────────────────────────────────────

def get_remote_version(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            if line.strip().startswith("SCRIPT_VERSION"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('"').strip("'")
    except Exception as e:
        print(f"[UPDATE] Could not fetch remote version: {e}")
    return None


def wait_for_update_confirmation(lcds_16: list, lcds_20: list,
                                  remote_ver: str) -> bool:
    """
    Show an update prompt on all displays and wait up to 60 seconds for input.
    Hold button (>2s) = confirm update.
    Short press (<2s) or timeout = cancel.
    Returns True if the user confirmed, False if cancelled or timed out.
    """
    HOLD_SECONDS  = 3.0   # how long to hold button to confirm
    TIMEOUT       = 60    # seconds before auto-cancelling

    for entry in lcds_16:
        write_lines(entry["lcd"], [
            pad("Update Available", 16),
            pad(f"v{SCRIPT_VERSION}->{remote_ver}", 16),
        ], 16)
    for entry in lcds_20:
        write_lines(entry["lcd"], [
            pad(f"Update Available!", 20),
            pad(f"v{SCRIPT_VERSION} -> v{remote_ver}", 20),
            pad("Hold btn: Install", 20),
            pad("Short press: Skip", 20),
        ], 20)

    print(f"[UPDATE] New version v{remote_ver} available.")
    print(f"[UPDATE] Hold button for {HOLD_SECONDS}s to install, short press to skip.")
    print(f"[UPDATE] Auto-cancelling in {TIMEOUT}s if no input.")

    deadline      = time.time() + TIMEOUT
    press_start   = None
    prev_level    = 1

    while time.time() < deadline:
        remaining = int(deadline - time.time())

        # Update countdown on displays every second
        for entry in lcds_20:
            write_lines(entry["lcd"], [
                pad(f"Update Available!", 20),
                pad(f"v{SCRIPT_VERSION} -> v{remote_ver}", 20),
                pad("Hold btn: Install", 20),
                pad(f"Skip in: {remaining}s", 20),
            ], 20)

        # Read button state
        level = 1
        if GPIO_AVAILABLE and _gpio_handle is not None:
            try:
                level = lgpio.gpio_read(_gpio_handle, BUTTON_PIN)
            except Exception:
                pass

        if level == 0 and prev_level == 1:
            # Button just pressed — start timing
            press_start = time.time()
            print("[UPDATE] Button pressed — hold to confirm...")

        elif level == 1 and prev_level == 0 and press_start is not None:
            # Button released — check how long it was held
            held = time.time() - press_start
            press_start = None
            if held >= HOLD_SECONDS:
                print("[UPDATE] Hold confirmed — installing update.")
                for entry in lcds_16:
                    write_lines(entry["lcd"], [
                        pad("Installing...", 16),
                        pad(f"v{remote_ver}", 16),
                    ], 16)
                for entry in lcds_20:
                    write_lines(entry["lcd"], [
                        pad("Installing Update", 20),
                        pad(f"v{SCRIPT_VERSION} -> v{remote_ver}", 20),
                        pad("Please wait...", 20),
                        pad("", 20),
                    ], 20)
                return True
            else:
                print("[UPDATE] Short press — update skipped.")
                for entry in lcds_16:
                    write_lines(entry["lcd"], [
                        pad("Update Skipped", 16),
                        pad("", 16),
                    ], 16)
                for entry in lcds_20:
                    write_lines(entry["lcd"], [
                        pad("Update Skipped", 20),
                        pad("", 20),
                        pad("", 20),
                        pad("", 20),
                    ], 20)
                time.sleep(2)
                return False

        elif level == 0 and press_start is not None:
            # Button still held — show progress
            held = time.time() - press_start
            bars = int((held / HOLD_SECONDS) * 16)
            progress = ("█" * bars).ljust(16)
            for entry in lcds_16:
                write_lines(entry["lcd"], [
                    pad("Hold to confirm:", 16),
                    pad(progress, 16),
                ], 16)

        prev_level = level
        time.sleep(0.1)

    # Timed out
    print("[UPDATE] No input received — update cancelled.")
    for entry in lcds_16:
        write_lines(entry["lcd"], [pad("Update Cancelled", 16), pad("Timed out", 16)], 16)
    for entry in lcds_20:
        write_lines(entry["lcd"], [
            pad("Update Cancelled", 20),
            pad("No input received", 20),
            pad("", 20),
            pad("", 20),
        ], 20)
    time.sleep(2)
    return False

def check_and_apply_update(lcds_16: list, lcds_20: list) -> None:
    """
    Check GitHub for a newer version. If found, prompt the user to confirm
    via button hold before downloading and applying the update.
    """
    print(f"[UPDATE] Checking for updates (current: v{SCRIPT_VERSION})...")
    remote_ver = get_remote_version(GITHUB_RAW_URL)

    if remote_ver is None:
        print("[UPDATE] Could not determine remote version — skipping.")
        return

    if remote_ver == SCRIPT_VERSION:
        print(f"[UPDATE] Already up to date (v{SCRIPT_VERSION}).")
        return

    # Ask user to confirm before installing
    if not wait_for_update_confirmation(lcds_16, lcds_20, remote_ver):
        return

    try:
        resp = requests.get(GITHUB_RAW_URL, timeout=20)
        resp.raise_for_status()
        tmp_path = SCRIPT_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(resp.text)
        os.replace(tmp_path, SCRIPT_PATH)
        print(f"[UPDATE] Updated to v{remote_ver}. Restarting...")
        subprocess.run(["sudo", "systemctl", "restart", "rocketfetcher"], check=False)

    except Exception as e:
        print(f"[UPDATE] Update failed: {e}")
        time.sleep(3)


# ── Timezone helpers ──────────────────────────────────────────────────────────

def utc_to_pacific(dt_utc: datetime) -> tuple[datetime, str]:
    year = dt_utc.year
    march_1  = datetime(year, 3, 8,  2, 0, tzinfo=timezone.utc)
    dst_start = march_1 + timedelta(days=(6 - march_1.weekday()) % 7)
    nov_1    = datetime(year, 11, 1, 2, 0, tzinfo=timezone.utc)
    dst_end  = nov_1 + timedelta(days=(6 - nov_1.weekday()) % 7)
    if dst_start <= dt_utc < dst_end:
        return dt_utc + timedelta(hours=-7), "PDT"
    return dt_utc + timedelta(hours=-8), "PST"


# ── String helpers ────────────────────────────────────────────────────────────

def pad(text: str, width: int) -> str:
    return str(text)[:width].ljust(width)

def center(text: str, width: int) -> str:
    return str(text)[:width].center(width)


# ── Site abbreviation map ─────────────────────────────────────────────────────

SITE_ABBREVIATIONS = {
    "Kennedy Space Center, FL, USA":                "Kennedy SC",
    "Cape Canaveral, FL, USA":                      "Cape Canaveral",
    "Cape Canaveral Space Force Station, FL, USA":  "Cape Canaveral",
    "Vandenberg Space Force Base, CA, USA":         "Vandenberg SFB",
    "Vandenberg AFB, CA, USA":                      "Vandenberg AFB",
    "Wallops Flight Facility, VA, USA":             "Wallops FF",
    "Mid-Atlantic Regional Spaceport, VA, USA":     "MARS, Wallops",
    "Kodiak Launch Complex, AK, USA":               "Kodiak, AK",
    "Pacific Spaceport Complex, AK, USA":           "Pacific SPC, AK",
    "Starbase, TX, USA":                            "Starbase TX",
    "SpaceX South Texas Launch Site, TX, USA":      "Starbase TX",
    "Rocket Lab Launch Complex 1, NZ":              "RL LC-1, NZ",
    "Mahia Peninsula, NZ":                          "Mahia, NZ",
    "Baikonur Cosmodrome, Kazakhstan":              "Baikonur",
    "Plesetsk Cosmodrome, Russia":                  "Plesetsk",
    "Vostochny Cosmodrome, Russia":                 "Vostochny",
    "Jiuquan Satellite Launch Center, China":       "Jiuquan, China",
    "Xichang Satellite Launch Center, China":       "Xichang, China",
    "Wenchang Space Launch Site, China":            "Wenchang, China",
    "Taiyuan Satellite Launch Center, China":       "Taiyuan, China",
    "Haiyang Oriental Spaceport, China":            "Haiyang, China",
    "Guiana Space Centre, French Guiana, France":   "Kourou, FG",
    "Kourou, French Guiana, France":                "Kourou, FG",
    "Satish Dhawan Space Centre, India":            "Sriharikota",
    "Tanegashima Space Center, Japan":              "Tanegashima",
    "Uchinoura Space Center, Japan":                "Uchinoura",
    "Naro Space Center, South Korea":               "Naro SC, Korea",
    "Shahroud Missile Test Site, Iran":             "Shahroud, Iran",
    "Palmachim Airbase, Israel":                    "Palmachim, IL",
}

VANDENBERG_KEYS = ["Vandenberg"]

COUNTRY_NAMES = {
    "USA": "United States",   "RUS": "Russia",          "CHN": "China",
    "FRA": "France",          "IND": "India",            "JPN": "Japan",
    "NZL": "New Zealand",     "KAZ": "Kazakhstan",       "IRN": "Iran",
    "KOR": "South Korea",     "ISR": "Israel",           "BRA": "Brazil",
    "GBR": "United Kingdom",  "AUS": "Australia",        "CAN": "Canada",
    "ITA": "Italy",           "DEU": "Germany",          "ARE": "United Arab Emirates",
    "ARG": "Argentina",       "MEX": "Mexico",           "UKR": "Ukraine",
    "SWE": "Sweden",          "NOR": "Norway",           "FIN": "Finland",
    "ESP": "Spain",           "PRT": "Portugal",         "POL": "Poland",
    "PAK": "Pakistan",        "IDN": "Indonesia",        "MYS": "Malaysia",
    "SGP": "Singapore",       "ZAF": "South Africa",     "EGY": "Egypt",
    "TUR": "Turkey",
}


# ── API fetch ─────────────────────────────────────────────────────────────────

_ERR_NO_INTERNET = "no_internet"
_ERR_OTHER       = "other_error"

def _do_fetch(page_limit: int, headers: dict):
    try:
        resp = requests.get(
            f"{API_BASE}/launch/upcoming/",
            params={"limit": page_limit, "mode": "normal", "ordering": "net"},
            headers=headers,
            timeout=20,
        )
        if resp.status_code == 429:
            retry_after = None
            try:
                detail = resp.json().get("detail", "")
                match = re.search(r"(\d+)\s+second", detail)
                if match:
                    retry_after = int(match.group(1))
            except Exception:
                pass
            return [], retry_after
        resp.raise_for_status()
        return resp.json().get("results", []), None
    except requests.exceptions.ConnectionError:
        print("[ERROR] No internet connection.")
        return _ERR_NO_INTERNET, None
    except requests.exceptions.Timeout:
        print("[ERROR] Request timed out.")
        return _ERR_OTHER, None
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTP {e.response.status_code}")
        return _ERR_OTHER, None
    except Exception as e:
        print(f"[ERROR] {e}")
        return _ERR_OTHER, None


def fetch_launches_from_api(count: int) -> tuple[list[dict], int | None, bool]:
    """
    Fetch launches from Launch Library 2 API.
    Returns (raw_list, retry_after, no_internet).
    """
    page_limit = count
    attempts   = []
    api_key    = load_api_key()
    if api_key:
        attempts.append(("authenticated", {"Authorization": f"Token {api_key}"}))
    attempts.append(("anonymous", {}))

    raw_list    = None
    retry_after = None
    no_internet = False

    for label, headers in attempts:
        print(f"  Trying {label} API access...")
        raw_list, retry_after = _do_fetch(page_limit, headers)

        if retry_after is not None:
            print(f"[ERROR] Rate limited ({label}). Retry in {retry_after}s.")
            return [], retry_after, False

        if raw_list is _ERR_NO_INTERNET:
            no_internet = True
            break

        if raw_list is _ERR_OTHER:
            if label == "authenticated":
                print("  [WARNING] Authenticated request failed, falling back to anonymous.")
            raw_list = None
            continue

        print(f"  ✓ Using {label} access.")
        break

    if no_internet:
        return [], None, True
    if raw_list is None or raw_list in (_ERR_NO_INTERNET, _ERR_OTHER):
        return [], None, False

    # Filter to future launches only
    now_utc = datetime.now(timezone.utc)
    results = []
    for launch in raw_list:
        net_str = launch.get("net", "")
        if not net_str:
            continue
        try:
            net_utc = datetime.fromisoformat(net_str.replace("Z", "+00:00"))
            if net_utc > now_utc:
                results.append(launch)
                if len(results) >= count:
                    break
        except Exception:
            continue

    return results, None, False


# ── Parse launch ──────────────────────────────────────────────────────────────

def parse_launch(raw: dict) -> dict:
    full_name    = raw.get("name", "Unknown")
    parts        = full_name.split("|")
    mission      = parts[-1].strip() if len(parts) > 1 else full_name
    rocket       = parts[0].strip()  if len(parts) > 1 else "Unknown"

    pad_obj      = raw.get("pad", {})
    location_obj = pad_obj.get("location", {})
    country_code = location_obj.get("country_code", "??")
    country      = COUNTRY_NAMES.get(country_code, country_code)
    location_full = location_obj.get("name", "Unknown Site")
    site_name    = SITE_ABBREVIATIONS.get(location_full, location_full)

    net_str = raw.get("net", "")
    if net_str:
        try:
            net_utc      = datetime.fromisoformat(net_str.replace("Z", "+00:00"))
            net_local, tz_label = utc_to_pacific(net_utc)
            date_str = net_local.strftime("%a %b %d %Y")
            time_str = net_local.strftime(f"%I:%M %p {tz_label}")
        except Exception:
            date_str = "Date TBD"
            time_str = "Time TBD"
    else:
        date_str = "Date TBD"
        time_str = "Time TBD"

    return {
        "mission":  mission,
        "rocket":   rocket,
        "site":     site_name,
        "country":  country,
        "date":     date_str,
        "time":     time_str,
        "status":   raw.get("status", {}).get("name", "Unknown"),
        "net":      net_str,
    }


# ── LCD init & write ──────────────────────────────────────────────────────────

def init_lcd(address: int, cols: int, rows: int):
    if not LCD_AVAILABLE:
        return None
    try:
        lcd = CharLCD(
            i2c_expander="PCF8574", address=address, port=1,
            cols=cols, rows=rows, dotsize=8, charmap="A02",
            auto_linebreaks=False, backlight_enabled=True,
        )
        lcd.clear()
        return lcd
    except Exception as e:
        print(f"  [WARNING] LCD @ {hex(address)} init failed: {e}")
        return None


def write_lines(lcd, lines: list[str], cols: int) -> None:
    if lcd is None:
        return
    try:
        for row, text in enumerate(lines):
            lcd.cursor_pos = (row, 0)
            lcd.write_string(pad(text, cols))
    except Exception as e:
        print(f"  [LCD ERROR] {e}")


def update_16x2(entry: dict, launch: dict | None) -> None:
    cols = 16
    addr = entry["address"]
    lines = [pad(launch["site"], cols), pad(launch["country"], cols)] if launch else [pad("No data", cols), pad("", cols)]
    print(f"  [16x2 @ {hex(addr)}]  {lines[0].strip()} / {lines[1].strip()}")
    write_lines(entry["lcd"], lines, cols)


def update_20x4(entry: dict, launch: dict | None) -> None:
    cols = 20
    addr = entry["address"]
    if launch:
        lines = [pad(launch["mission"], cols), pad(launch["rocket"], cols),
                 pad(launch["date"], cols), pad(launch["time"], cols)]
    else:
        lines = [pad("No data", cols)] * 4
    print(f"  [20x4 @ {hex(addr)}]")
    for i, l in enumerate(lines):
        print(f"          Row {i}: {l.strip()}")
    write_lines(entry["lcd"], lines, cols)


def show_startup(lcds_16: list, lcds_20: list) -> None:
    for e in lcds_16:
        write_lines(e["lcd"], [pad("Rocket Fetcher", 16), pad("Starting...", 16)], 16)
    for e in lcds_20:
        write_lines(e["lcd"], [
            center("Rocket Launch", 20),
            center("Fetcher v1.0.0", 20),
            center("By Matthew Sarvas", 20),
            center("Fetching data...", 20),
        ], 20)
    time.sleep(2)


def clear_all(lcds_16: list, lcds_20: list) -> None:
    for entry in lcds_16 + lcds_20:
        lcd = entry.get("lcd")
        if lcd:
            try:
                lcd.clear()
                lcd.backlight_enabled = False
            except Exception:
                pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _gpio_handle

    parser = argparse.ArgumentParser(description="Rocket Launch Fetcher + Display")
    parser.add_argument("--refresh",  type=int, default=None)
    parser.add_argument("--launches", type=int, default=FETCH_COUNT)
    args = parser.parse_args()

    if args.refresh is not None:
        refresh_interval = args.refresh
        refresh_source   = "manual override"
    elif load_api_key():
        refresh_interval = REFRESH_INTERVAL_AUTH
        refresh_source   = "authenticated (60 req/hr)"
    else:
        refresh_interval = REFRESH_INTERVAL_ANON
        refresh_source   = "anonymous (15 req/hr)"

    print("=" * 52)
    print("  🚀  Rocket Launch Fetcher + Display")
    print(f"  Fetch count: {args.launches} | Refresh: {refresh_interval}s ({refresh_source})")
    print("=" * 52)

    # Init LCDs
    lcds_16 = []
    for cfg in LCD_16x2_CONFIGS:
        lcd = init_lcd(cfg["address"], cols=16, rows=2)
        lcds_16.append({**cfg, "lcd": lcd})
        print(f"  16x2 @ {hex(cfg['address'])} (slot {cfg['slot']}) → {'OK' if lcd else 'FAILED'}")

    lcds_20 = []
    for cfg in LCD_20x4_CONFIGS:
        lcd = init_lcd(cfg["address"], cols=20, rows=4)
        lcds_20.append({**cfg, "lcd": lcd})
        print(f"  20x4 @ {hex(cfg['address'])} (slot {cfg['slot']}) → {'OK' if lcd else 'FAILED'}")

    show_startup(lcds_16, lcds_20)

    # Button setup
    vandenberg_only   = False
    filter_lock       = threading.Lock()
    last_fetch        = 0
    all_launches      = []
    last_update_check = 0
    last_button_time  = 0.0
    BUTTON_DEBOUNCE_S = 1.0

    def on_button_press(channel):
        nonlocal vandenberg_only, last_button_time
        now_t = time.time()
        if now_t - last_button_time < BUTTON_DEBOUNCE_S:
            return
        last_button_time = now_t
        with filter_lock:
            vandenberg_only = not vandenberg_only
            mode = "Vandenberg only" if vandenberg_only else "All locations"
            print(f"\n[BUTTON] Filter toggled -> {mode} (using cached data)")
            for entry in lcds_16:
                write_lines(entry["lcd"], [pad("Filter:", 16), pad(mode, 16)], 16)
            for entry in lcds_20:
                write_lines(entry["lcd"], [
                    pad("Filter changed:", 20), pad(mode, 20),
                    pad("Updating...", 20), pad("", 20),
                ], 20)

    if GPIO_AVAILABLE:
        try:
            _gpio_handle = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_input(_gpio_handle, BUTTON_PIN, lgpio.SET_PULL_UP)
            print(f"  Button on GPIO{BUTTON_PIN} ready — press to toggle Vandenberg filter.")
        except Exception as e:
            print(f"  [WARNING] Button setup failed: {e}")

    # Main loop
    try:
        while True:
            now = time.time()

            with filter_lock:
                current_filter = vandenberg_only

            # Auto-update check
            if now - last_update_check >= UPDATE_INTERVAL:
                last_update_check = time.time()
                check_and_apply_update(lcds_16, lcds_20)

            # Fetch from API and push to GitHub
            if now - last_fetch >= refresh_interval or not all_launches:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{ts}] Fetching {args.launches} launches from API...")
                raw_list, retry_after, no_internet = fetch_launches_from_api(args.launches)

                if no_internet:
                    print("  ERROR: No internet.")
                    for entry in lcds_16:
                        write_lines(entry["lcd"], [pad("No Internet", 16), pad("Connection", 16)], 16)
                    for entry in lcds_20:
                        write_lines(entry["lcd"], [
                            pad("No Internet Connection", 20), pad("Check network and", 20),
                            pad("restart if needed.", 20), pad("Retrying shortly...", 20),
                        ], 20)
                    time.sleep(30)
                    last_fetch = 0
                    continue

                if retry_after is not None:
                    print(f"  WARNING: Rate limited. Counting down {retry_after}s.")
                    deadline = time.time() + retry_after
                    while True:
                        remaining = int(deadline - time.time())
                        if remaining <= 0:
                            break
                        mins     = remaining // 60
                        secs     = remaining % 60
                        wait_str = f"{mins}m {secs:02d}s" if mins else f"{remaining}s"
                        for entry in lcds_16:
                            write_lines(entry["lcd"], [pad("API Rate Limited", 16), pad(f"Retry in {wait_str}", 16)], 16)
                        for entry in lcds_20:
                            write_lines(entry["lcd"], [
                                pad("API Rate Limited", 20), pad("Too many requests", 20),
                                pad(f"Retry in: {wait_str}", 20), pad("", 20),
                            ], 20)
                        print(f"  Rate limit countdown: {wait_str}    ", end="\r")
                        time.sleep(1)
                    print()
                    last_fetch = 0
                    continue

                all_launches = [parse_launch(r) for r in raw_list]
                last_fetch   = time.time()
                print(f"  ✓ {len(all_launches)} launches fetched.")

                # Push raw parsed data to GitHub
                push_to_github(all_launches)

            # Apply local filter
            if current_filter:
                filtered = [l for l in all_launches
                            if any(k.lower() in l["site"].lower() for k in VANDENBERG_KEYS)]
            else:
                filtered = all_launches
            launches = filtered[:TOTAL_SLOTS]

            # Update displays
            mode_label = " [VAFB]" if current_filter else ""
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Updating displays{mode_label}:")
            for entry in lcds_16:
                slot   = entry["slot"]
                update_16x2(entry, launches[slot] if slot < len(launches) else None)
            for entry in lcds_20:
                slot   = entry["slot"]
                update_20x4(entry, launches[slot] if slot < len(launches) else None)

            # Sleep with button polling
            elapsed   = time.time() - last_fetch
            sleep_for = max(0, refresh_interval - elapsed)
            print(f"\n  Next refresh in {int(sleep_for)}s  (Ctrl+C to quit)\n")
            sleep_end   = time.time() + sleep_for
            prev_filter = current_filter
            global BUTTON_LAST_LEVEL
            while time.time() < sleep_end:
                if GPIO_AVAILABLE and _gpio_handle is not None:
                    try:
                        level = lgpio.gpio_read(_gpio_handle, BUTTON_PIN)
                        if level == 0 and BUTTON_LAST_LEVEL == 1:
                            on_button_press(BUTTON_PIN)
                        BUTTON_LAST_LEVEL = level
                    except Exception:
                        pass
                with filter_lock:
                    new_filter = vandenberg_only
                if new_filter != prev_filter:
                    break
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n\n[INFO] Shutting down...")
        if GPIO_AVAILABLE and _gpio_handle is not None:
            try:
                lgpio.gpiochip_close(_gpio_handle)
            except Exception:
                pass
        clear_all(lcds_16, lcds_20)
        print("[INFO] Done. Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
