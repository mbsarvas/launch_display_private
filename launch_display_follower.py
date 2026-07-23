"""
Rocket Launch Follower — Multi-Display Edition
Raspberry Pi Zero 2 W or Zero W | Python 3.13
Version 1.0.0
Date: June 25, 2026
Created by Matthew Sarvas

Display layout:
    3x 16x2 LCDs (0x24, 0x23, 0x22) — Launch Site / Country
    3x 20x4 LCDs (0x27, 0x26, 0x25) — Mission / Vehicle / Date / Time (PST)

Each display pair (one 16x2 + one 20x4) shows a different upcoming launch.
Launches are refreshed from Launch Library 2 every REFRESH_INTERVAL seconds.

Wiring (same for all 6 displays — shared I2C bus):
    VCC → Pi Pin 2  (5V)
    GND → Pi Pin 6  (GND)
    SDA → Pi Pin 3  (GPIO2)
    SCL → Pi Pin 5  (GPIO3)

Toggle button wiring:
    One leg → Pi Pin 11 (GPIO17)
    Other leg → Pi Pin 9 (GND)
    (Internal pull-up resistor is used — no external resistor needed)

Install dependencies:
    pip install requests RPLCD smbus2 RPi.GPIO

Run:
    python3 rocket_launch_tracker.py [--refresh 300] [--launches 3]
"""

import argparse
import os
import subprocess
import time
import sys
import threading
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
    _gpio_handle = None   # lgpio chip handle, opened in main()
except ImportError:
    GPIO_AVAILABLE = False
    print("[WARNING] lgpio not installed — button toggle disabled.")

BUTTON_LAST_LEVEL = 1    # tracks previous pin level for edge detection via polling

# ── Configuration ─────────────────────────────────────────────────────────────

# 16x2 displays → one per launch slot
# Line 1: Launch site name   |  Line 2: Country
LCD_16x2_CONFIGS = [
    {"address": 0x24, "slot": 0},
    {"address": 0x23, "slot": 1},
    {"address": 0x22, "slot": 2},
]

# 20x4 displays → one per launch slot
# Line 1: Mission name  |  Line 2: Rocket  |  Line 3: Date (PST)  |  Line 4: Time (PST)
LCD_20x4_CONFIGS = [
    {"address": 0x27, "slot": 0},
    {"address": 0x26, "slot": 1},
    {"address": 0x25, "slot": 2},
]

TOTAL_SLOTS      = 3     # number of launch slots shown at once (one per display pair)
FETCH_COUNT      = 20    # how many launches to cache locally (used for local filtering)
API_BASE         = "https://ll.thespacedevs.com/2.0.0"
REFRESH_INTERVAL_ANON = 360  # anonymous: 15 req/hour → 1 per 6 min (360s)
REFRESH_INTERVAL_AUTH = 60   # authenticated: 60 req/hour → 1 per 60s
BUTTON_PIN       = 27    # GPIO pin for the Vandenberg filter toggle button

# ── User configuration ─────────────────────────────────────────────────────────
# PI_USER is detected automatically from the current system user — no manual edit needed.
# Only change SCRIPT_SUBDIR if you installed the script in a different folder name.
import os as _os
PI_USER          = _os.environ.get("USER") or _os.environ.get("LOGNAME") or "pi"
SCRIPT_SUBDIR    = "launch-display"   # folder the script lives in
API_KEY_FILE     = f"/home/{PI_USER}/ll2_api_key.txt"
SCRIPT_PATH      = f"/home/{PI_USER}/{SCRIPT_SUBDIR}/launch_display_follower.py"

# ── Auto-update settings ───────────────────────────────────────────────────────
SCRIPT_VERSION   = "1.0.1"
GITHUB_RAW_URL   = "https://raw.githubusercontent.com/mbsarvas/launch-display-alternate/main/launch_display_follower.py"
UPDATE_INTERVAL  = 86400   # seconds between update checks (86400 = 24 hours)
GITHUB_DATA_URL  = "https://raw.githubusercontent.com/mbsarvas/launch-display-alternate/main/launches.json"

# ── API key ──────────────────────────────────────────────────────────────────

def load_api_key() -> str | None:
    """
    Load the Launch Library 2 API key from a plain text file.
    The file should contain just the key on a single line, e.g.:
        abc123yourkeyhere
    Returns the key string, or None if the file doesn't exist or is empty.
    """
    try:
        with open(API_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[WARNING] Could not read API key file: {e}")
    return None


# ── Auto-updater ─────────────────────────────────────────────────────────────

def get_remote_version(url: str) -> str | None:
    """
    Fetch the remote script and extract its version number from the
    'SCRIPT_VERSION = "x.x"' line. Returns the version string or None.
    """
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            if line.strip().startswith("SCRIPT_VERSION"):
                # e.g.  SCRIPT_VERSION   = "1.1"
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
    print(f"[UPDATE] Checking for updates (current version: {SCRIPT_VERSION})...")
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
        print(f"[UPDATE] Script updated to v{remote_ver}. Restarting service...")
        subprocess.run(["sudo", "systemctl", "restart", "launchfollower"], check=False)

    except Exception as e:
        print(f"[UPDATE] Update failed: {e}")
        for entry in lcds_16:
            write_lines(entry["lcd"], [pad("Update Failed", 16), pad("See terminal", 16)], 16)
        for entry in lcds_20:
            write_lines(entry["lcd"], [
                pad("Update Failed!", 20), pad(str(e)[:20], 20),
                pad("Continuing...", 20),  pad("", 20),
            ], 20)
        time.sleep(3)


# ── Timezone helpers ──────────────────────────────────────────────────────────

def utc_to_pacific(dt_utc: datetime) -> tuple[datetime, str]:
    """
    Convert a UTC-aware datetime to Pacific time.
    Returns (local_datetime, label) where label is 'PST' or 'PDT'.
    Uses the standard US DST rule:
        Start: 2nd Sunday in March at 02:00 UTC
        End:   1st Sunday in November at 02:00 UTC
    """
    year = dt_utc.year

    # 2nd Sunday in March
    march_1 = datetime(year, 3, 8, 2, 0, tzinfo=timezone.utc)
    dst_start = march_1 + timedelta(days=(6 - march_1.weekday()) % 7)

    # 1st Sunday in November
    nov_1 = datetime(year, 11, 1, 2, 0, tzinfo=timezone.utc)
    dst_end = nov_1 + timedelta(days=(6 - nov_1.weekday()) % 7)

    if dst_start <= dt_utc < dst_end:
        return dt_utc + timedelta(hours=-7), "PDT"
    return dt_utc + timedelta(hours=-8), "PST"


# ── String helpers ────────────────────────────────────────────────────────────

def pad(text: str, width: int) -> str:
    """Truncate and left-pad a string to exactly `width` characters."""
    return str(text)[:width].ljust(width)


def center(text: str, width: int) -> str:
    """Center-justify a string within `width` characters."""
    return str(text)[:width].center(width)


# ── Site abbreviation map ─────────────────────────────────────────────────────
# Maps the Launch Library 2 location "name" to a 16-char-friendly label.
# Add more entries here if you see a full name appearing on the 16x2 screen.

SITE_ABBREVIATIONS = {
    # USA
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
    # New Zealand
    "Rocket Lab Launch Complex 1, NZ":              "RL LC-1, NZ",
    "Mahia Peninsula, NZ":                          "Mahia, NZ",
    # Russia / Kazakhstan
    "Baikonur Cosmodrome, Kazakhstan":              "Baikonur",
    "Plesetsk Cosmodrome, Russia":                  "Plesetsk",
    "Vostochny Cosmodrome, Russia":                 "Vostochny",
    # China
    "Jiuquan Satellite Launch Center, China":       "Jiuquan, China",
    "Xichang Satellite Launch Center, China":       "Xichang, China",
    "Wenchang Space Launch Site, China":            "Wenchang, China",
    "Taiyuan Satellite Launch Center, China":       "Taiyuan, China",
    "Haiyang Oriental Spaceport, China":            "Haiyang, China",
    # Europe
    "Guiana Space Centre, French Guiana, France":   "Kourou, FG",
    "Kourou, French Guiana, France":                "Kourou, FG",
    # India
    "Satish Dhawan Space Centre, India":            "Sriharikota",
    # Japan
    "Tanegashima Space Center, Japan":              "Tanegashima",
    "Uchinoura Space Center, Japan":                "Uchinoura",
    # South Korea
    "Naro Space Center, South Korea":               "Naro SC, Korea",
    # Iran
    "Shahroud Missile Test Site, Iran":             "Shahroud, Iran",
    # Israel
    "Palmachim Airbase, Israel":                    "Palmachim, IL",
}

# Location name substrings used to identify Vandenberg launches.
# Any launch whose location name contains one of these strings will be included
# when the Vandenberg filter is active.
VANDENBERG_KEYS = [
    "Vandenberg",
]


# ── Country code map ──────────────────────────────────────────────────────────

COUNTRY_NAMES = {
    "USA": "United States",
    "RUS": "Russia",
    "CHN": "China",
    "FRA": "France",
    "IND": "India",
    "JPN": "Japan",
    "NZL": "New Zealand",
    "KAZ": "Kazakhstan",
    "IRN": "Iran",
    "KOR": "South Korea",
    "ISR": "Israel",
    "BRA": "Brazil",
    "GBR": "United Kingdom",
    "AUS": "Australia",
    "CAN": "Canada",
    "ITA": "Italy",
    "DEU": "Germany",
    "ARE": "United Arab Emirates",
    "ARG": "Argentina",
    "MEX": "Mexico",
    "UKR": "Ukraine",
    "SWE": "Sweden",
    "NOR": "Norway",
    "FIN": "Finland",
    "ESP": "Spain",
    "PRT": "Portugal",
    "POL": "Poland",
    "PAK": "Pakistan",
    "IDN": "Indonesia",
    "MYS": "Malaysia",
    "SGP": "Singapore",
    "ZAF": "South Africa",
    "EGY": "Egypt",
    "TUR": "Turkey",
}


# ── GitHub data source ───────────────────────────────────────────────────────

def fetch_from_github() -> list[dict] | None:
    """
    Fetch cached launch data from the GitHub data repo.
    Returns a list of parsed launch dicts, or None if unavailable.
    Handles connection errors, DNS failures, unexpected formats, and empty results.
    """
    try:
        resp = requests.get(GITHUB_DATA_URL, timeout=15)
        resp.raise_for_status()

        # Guard against non-JSON responses (e.g. GitHub maintenance pages)
        try:
            payload = resp.json()
        except Exception:
            print("  [WARNING] GitHub data returned non-JSON response — skipping.")
            return None

        # Guard against unexpected JSON structure
        if not isinstance(payload, dict) or "launches" not in payload:
            print("  [WARNING] GitHub data has unexpected format — skipping.")
            return None

        launches = payload.get("launches", [])
        fetched  = payload.get("fetched_at", "unknown")

        if not launches:
            print("  [WARNING] GitHub data file is empty — no launches found.")
            return None

        # Filter out any launches that have already passed
        now_utc = datetime.now(timezone.utc)
        valid   = []
        for launch in launches:
            net_str = launch.get("net", "")
            if not net_str:
                valid.append(launch)
                continue
            try:
                net_utc = datetime.fromisoformat(net_str.replace("Z", "+00:00"))
                if net_utc > now_utc:
                    valid.append(launch)
            except Exception:
                valid.append(launch)

        if not valid:
            print("  [WARNING] All launches in GitHub cache have already passed.")
            return None

        print(f"  ✓ Got {len(valid)} launches from GitHub data (fetched at {fetched}).")
        return valid

    except requests.exceptions.ConnectionError:
        print("  [ERROR] No internet — cannot reach GitHub data.")
        return None
    except requests.exceptions.Timeout:
        print("  [WARNING] GitHub data request timed out.")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"  [WARNING] GitHub data HTTP error: {e.response.status_code}")
        return None
    except Exception as e:
        print(f"  [WARNING] GitHub data fetch failed: {e}")
        return None


# ── API ───────────────────────────────────────────────────────────────────────

# Sentinel values returned by _do_fetch for error states
_ERR_NO_INTERNET = "no_internet"
_ERR_OTHER       = "other_error"

def _do_fetch(page_limit: int, headers: dict):
    """
    Make a single request to the API.
    Returns:
        (raw_list, None)           on success
        ([],       retry_after)    on 429 rate limit (retry_after = seconds)
        (_ERR_NO_INTERNET, None)   on connection error
        (_ERR_OTHER,       None)   on timeout or other HTTP error
    """
    import re
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
    except requests.exceptions.ConnectionError as e:
        if "NameResolutionError" in str(type(e).__mro__[1].__name__) or "getaddrinfo" in str(e).lower():
            print("[ERROR] DNS resolution failed — cannot resolve API hostname.")
        else:
            print("[ERROR] No internet connection.")
        return _ERR_NO_INTERNET, None
    except requests.exceptions.Timeout:
        print("[ERROR] Request timed out.")
        return _ERR_OTHER, None
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTP {e.response.status_code}")
        return _ERR_OTHER, None
    except ValueError:
        print("[ERROR] API returned unexpected non-JSON response.")
        return _ERR_OTHER, None
    except Exception as e:
        print(f"[ERROR] {e}")
        return _ERR_OTHER, None


def fetch_launches(count: int, vandenberg_only: bool = False) -> tuple[list[dict], int | None]:
    """
    Fetch the next `count` upcoming launches strictly in the future.
    If vandenberg_only is True, only launches from Vandenberg are returned.

    Tries authenticated access first (if an API key file exists), then
    falls back to anonymous access automatically if the key fails.

    Returns (launches, retry_after, no_internet) where:
        retry_after  = seconds to wait if rate-limited (429), else None
        no_internet  = True if a connection error occurred
    """
    now_utc    = datetime.now(timezone.utc)
    page_limit = 50 if vandenberg_only else 20

    # Build list of (label, headers) to try in order
    attempts = []
    api_key  = load_api_key()
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
            print(f"  [ERROR] No internet connection.")
            break   # no point retrying other auth methods without internet

        if raw_list is _ERR_OTHER:
            if label == "authenticated":
                print(f"  [WARNING] Authenticated request failed, falling back to anonymous.")
            raw_list = None
            continue

        # Success
        if label == "authenticated":
            print(f"  ✓ Using authenticated access (higher rate limits).")
        else:
            print(f"  ✓ Using anonymous access.")
        break

    if no_internet:
        return [], None, True

    if raw_list is None or raw_list in (_ERR_NO_INTERNET, _ERR_OTHER):
        return [], None, False

    # Filter to future launches (and optionally Vandenberg only)
    results = []
    for launch in raw_list:
        net_str = launch.get("net", "")
        if not net_str:
            continue
        try:
            net_utc = datetime.fromisoformat(net_str.replace("Z", "+00:00"))
            if net_utc <= now_utc:
                continue

            if vandenberg_only:
                loc_name = (launch.get("pad", {})
                                  .get("location", {})
                                  .get("name", ""))
                if not any(key.lower() in loc_name.lower() for key in VANDENBERG_KEYS):
                    continue

            results.append(launch)
            if len(results) >= count:
                break
        except Exception:
            continue

    return results, None, False


def parse_launch(raw: dict) -> dict:
    """Extract and format all display-ready fields from a raw API launch object."""

    # ── Mission & rocket name ─────────────────────────────────────────────────
    full_name = raw.get("name", "Unknown")
    parts     = full_name.split("|")
    mission   = parts[-1].strip() if len(parts) > 1 else full_name
    rocket    = parts[0].strip()  if len(parts) > 1 else "Unknown"

    # ── Launch site & country ─────────────────────────────────────────────────
    pad_obj      = raw.get("pad", {})
    location_obj = pad_obj.get("location", {})
    country_code = location_obj.get("country_code", "??")
    country      = COUNTRY_NAMES.get(country_code, country_code)

    # Use the location name (the base/range, not the individual complex)
    # and shorten it to fit 16 chars using a friendly abbreviation map.
    location_full = location_obj.get("name", "Unknown Site")
    site_name = SITE_ABBREVIATIONS.get(location_full, location_full)

    # ── Date & time (Pacific) ─────────────────────────────────────────────────
    net_str = raw.get("net", "")
    if net_str:
        try:
            net_utc      = datetime.fromisoformat(net_str.replace("Z", "+00:00"))
            net_local, tz_label = utc_to_pacific(net_utc)
            # e.g. "Mon Apr 28 2026"
            date_str = net_local.strftime("%a %b %d %Y")
            # e.g. "09:30 AM PDT"
            time_str = net_local.strftime(f"%I:%M %p {tz_label}")
        except Exception:
            date_str = "Date TBD"
            time_str = "Time TBD"
    else:
        date_str = "Date TBD"
        time_str = "Time TBD"

    return {
        "mission": mission,
        "rocket":  rocket,
        "site":    site_name,
        "country": country,
        "date":    date_str,
        "time":    time_str,
        "status":  raw.get("status", {}).get("name", "Unknown"),
    }


# ── LCD initialisation ────────────────────────────────────────────────────────

def init_lcd(address: int, cols: int, rows: int) -> "CharLCD | None":
    if not LCD_AVAILABLE:
        return None
    try:
        lcd = CharLCD(
            i2c_expander="PCF8574",
            address=address,
            port=1,
            cols=cols,
            rows=rows,
            dotsize=8,
            charmap="A02",
            auto_linebreaks=False,
            backlight_enabled=True,
        )
        lcd.clear()
        return lcd
    except Exception as e:
        print(f"  [WARNING] LCD @ {hex(address)} init failed: {e}")
        return None


def write_lines(lcd: "CharLCD | None", lines: list[str], cols: int) -> None:
    """Write a list of pre-padded strings (one per row) to an LCD."""
    if lcd is None:
        return
    try:
        for row, text in enumerate(lines):
            lcd.cursor_pos = (row, 0)
            lcd.write_string(pad(text, cols))
    except Exception as e:
        print(f"  [LCD ERROR] {e}")


# ── Per-display update ────────────────────────────────────────────────────────

def update_16x2(entry: dict, launch: dict | None) -> None:
    """
    16x2 LCD:
        Row 0 — Launch site name
        Row 1 — Country
    """
    cols = 16
    addr = entry["address"]

    if launch:
        lines = [pad(launch["site"], cols), pad(launch["country"], cols)]
    else:
        lines = [pad("No data", cols), pad("", cols)]

    print(f"  [16x2 @ {hex(addr)}]  {lines[0].strip()} / {lines[1].strip()}")
    write_lines(entry["lcd"], lines, cols)


def update_20x4(entry: dict, launch: dict | None) -> None:
    """
    20x4 LCD:
        Row 0 — Mission name
        Row 1 — Launch vehicle (rocket)
        Row 2 — Date in PST/PDT
        Row 3 — Time in PST/PDT
    """
    cols = 20
    addr = entry["address"]

    if launch:
        lines = [
            pad(launch["mission"], cols),
            pad(launch["rocket"],  cols),
            pad(launch["date"],    cols),
            pad(launch["time"],    cols),
        ]
    else:
        lines = [pad("No data", cols)] * 4

    print(f"  [20x4 @ {hex(addr)}]")
    for i, l in enumerate(lines):
        print(f"          Row {i}: {l.strip()}")

    write_lines(entry["lcd"], lines, cols)


# ── Startup / shutdown screens ────────────────────────────────────────────────

def show_startup(lcds_16: list, lcds_20: list) -> None:
    for e in lcds_16:
        write_lines(e["lcd"],
                    [pad("Launch Follower", 16), pad("Starting...", 16)], 16)
    for e in lcds_20:
        write_lines(e["lcd"], [
            center("Rocket Launch", 20),
            center("Follower v1.0.0", 20),
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
    parser = argparse.ArgumentParser(description="Rocket Launch Tracker — Multi-Display")
    parser.add_argument("--refresh",  type=int, default=None,
                        help="Override API refresh interval in seconds")
    parser.add_argument("--launches", type=int, default=TOTAL_SLOTS,
                        help=f"Number of launches to display (default: {TOTAL_SLOTS})")
    args = parser.parse_args()

    num_slots = max(args.launches, TOTAL_SLOTS)

    # Auto-select refresh rate based on whether an API key is present
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
    print("  🚀  Rocket Launch Tracker — Multi-Display")
    print(f"  Slots: {TOTAL_SLOTS} | Refresh: {refresh_interval}s ({refresh_source})")
    print("=" * 52)

    # ── Init all LCDs ─────────────────────────────────────────────────────────
    lcds_16 = []
    for cfg in LCD_16x2_CONFIGS:
        lcd = init_lcd(cfg["address"], cols=16, rows=2)
        lcds_16.append({**cfg, "lcd": lcd})
        print(f"  16x2 @ {hex(cfg['address'])} (slot {cfg['slot']}) "
              f"→ {'OK' if lcd else 'FAILED (terminal only)'}")

    lcds_20 = []
    for cfg in LCD_20x4_CONFIGS:
        lcd = init_lcd(cfg["address"], cols=20, rows=4)
        lcds_20.append({**cfg, "lcd": lcd})
        print(f"  20x4 @ {hex(cfg['address'])} (slot {cfg['slot']}) "
              f"→ {'OK' if lcd else 'FAILED (terminal only)'}")

    show_startup(lcds_16, lcds_20)

    # ── Button setup ──────────────────────────────────────────────────────────
    vandenberg_only    = False
    filter_lock        = threading.Lock()
    global _gpio_handle
    last_fetch         = 0
    all_launches       = []   # full cache fetched from API (up to FETCH_COUNT)
    last_update_check  = 0    # timestamp of last GitHub update check
    last_button_time   = 0.0      # timestamp of last accepted press
    BUTTON_DEBOUNCE_S  = 1.0      # ignore any press within this many seconds of the last

    def on_button_press(channel):
        nonlocal vandenberg_only, last_button_time

        # Software debounce — reject rapid/held presses beyond GPIO bouncetime
        now_t = time.time()
        if now_t - last_button_time < BUTTON_DEBOUNCE_S:
            return
        last_button_time = now_t

        with filter_lock:
            vandenberg_only = not vandenberg_only
            mode = "Vandenberg only" if vandenberg_only else "All locations"
            print(f"\n[BUTTON] Filter toggled -> {mode} (using cached data, no API call)")
            for entry in lcds_16:
                write_lines(entry["lcd"],
                            [pad("Filter:", 16), pad(mode, 16)], 16)
            for entry in lcds_20:
                write_lines(entry["lcd"], [
                    pad("Filter changed:", 20),
                    pad(mode,             20),
                    pad("Updating...",    20),
                    pad("",              20),
                ], 20)
            # No last_fetch = 0 here — filter uses cached data, no API call needed

    if GPIO_AVAILABLE:
        try:
            _gpio_handle = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_input(_gpio_handle, BUTTON_PIN, lgpio.SET_PULL_UP)
            print(f"  Button on GPIO{BUTTON_PIN} ready — press to toggle Vandenberg filter.")
        except Exception as e:
            print(f"  [WARNING] Button setup failed: {e} — toggle disabled.")
    else:
        print("  [WARNING] lgpio not available — button toggle disabled.")

    # ── Main loop ─────────────────────────────────────────────────────────────
    try:
        while True:
            now = time.time()

            with filter_lock:
                current_filter = vandenberg_only

            # ── GitHub update check (every 24 hours) ───────────────────────────
            if now - last_update_check >= UPDATE_INTERVAL:
                last_update_check = time.time()
                check_and_apply_update(lcds_16, lcds_20)

            # ── Fetch data (GitHub first, API fallback) ────────────────────────
            if now - last_fetch >= refresh_interval or not all_launches:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{ts}] Fetching launch data...")

                # Try GitHub data source first
                print("  Trying GitHub data source...")
                github_launches = fetch_from_github()

                if github_launches is not None:
                    all_launches = github_launches
                    last_fetch   = time.time()
                    print(f"  ✓ {len(all_launches)} launches loaded from GitHub.\n")
                else:
                    # Fall back to direct API
                    print("  GitHub data unavailable — falling back to Launch Library 2 API...")
                    raw_list, retry_after, no_internet = fetch_launches(FETCH_COUNT, vandenberg_only=False)

                    if no_internet:
                        print(f"  ERROR: No internet. Showing message on displays.")
                        for entry in lcds_16:
                            write_lines(entry["lcd"], [
                                pad("No Internet", 16),
                                pad("Connection", 16),
                            ], 16)
                        for entry in lcds_20:
                            write_lines(entry["lcd"], [
                                pad("No Internet Connection", 20),
                                pad("Check network and",     20),
                                pad("restart if needed.",    20),
                                pad("Retrying shortly...",   20),
                            ], 20)
                        time.sleep(30)
                        last_fetch = 0
                        continue

                    if retry_after is not None:
                        print(f"  WARNING: Rate limited. Counting down {retry_after}s on displays.")
                        deadline = time.time() + retry_after
                        while True:
                            remaining = int(deadline - time.time())
                            if remaining <= 0:
                                break
                            mins     = remaining // 60
                            secs     = remaining % 60
                            wait_str = f"{mins}m {secs:02d}s" if mins else f"{remaining}s"
                            for entry in lcds_16:
                                write_lines(entry["lcd"], [
                                    pad("API Rate Limited", 16),
                                    pad(f"Retry in {wait_str}", 16),
                                ], 16)
                            for entry in lcds_20:
                                write_lines(entry["lcd"], [
                                    pad("API Rate Limited",       20),
                                    pad("Too many requests",      20),
                                    pad(f"Retry in: {wait_str}", 20),
                                    pad("",                       20),
                                ], 20)
                            print(f"  Rate limit countdown: {wait_str}    ", end="\r")
                            time.sleep(1)
                        print()
                        last_fetch = 0
                        continue

                    all_launches = [parse_launch(r) for r in raw_list]
                    last_fetch   = time.time()
                    print(f"  OK {len(all_launches)} launches loaded from API.\n")

            # ── Apply filter locally from cache (no API call) ──────────────────
            if current_filter:
                filtered = [l for l in all_launches
                            if any(k.lower() in l["site"].lower()
                                   or k.lower() in l["country"].lower()
                                   for k in VANDENBERG_KEYS)]
            else:
                filtered = all_launches

            launches = filtered[:TOTAL_SLOTS]

            # ── Handle empty states with descriptive messages ──────────────────
            if not all_launches:
                # Cache is completely empty — fetch failed without a specific error
                print(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: No launch data in cache — fetch may have failed silently.")
                for entry in lcds_16:
                    write_lines(entry["lcd"], [
                        pad("No Launch Data", 16),
                        pad("Retrying soon", 16),
                    ], 16)
                for entry in lcds_20:
                    write_lines(entry["lcd"], [
                        pad("No Launch Data", 20),
                        pad("Data fetch failed.", 20),
                        pad("Check connection", 20),
                        pad("Retrying shortly", 20),
                    ], 20)
                last_fetch = 0   # force retry on next loop
                time.sleep(30)
                continue

            if current_filter and not filtered:
                # Filter is active but no Vandenberg launches in cache
                print(f"[{datetime.now().strftime('%H:%M:%S')}] No Vandenberg launches in current cache.")
                for entry in lcds_16:
                    write_lines(entry["lcd"], [
                        pad("No VAFB Launches", 16),
                        pad("In cache", 16),
                    ], 16)
                for entry in lcds_20:
                    write_lines(entry["lcd"], [
                        pad("No Vandenberg", 20),
                        pad("Launches Found", 20),
                        pad("In current cache.", 20),
                        pad("Try all locations", 20),
                    ], 20)
                # Don't force a re-fetch — the data is fine, just no VAFB launches right now
                # Fall through to sleep so displays update again next cycle

            # ── Write each slot to its paired displays ─────────────────────────
            mode_label = " [VAFB]" if current_filter else ""
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Updating displays{mode_label} "
                  f"({len(launches)} shown of {len(all_launches)} cached):")

            for entry in lcds_16:
                slot   = entry["slot"]
                launch = launches[slot] if slot < len(launches) else None
                update_16x2(entry, launch)

            for entry in lcds_20:
                slot   = entry["slot"]
                launch = launches[slot] if slot < len(launches) else None
                update_20x4(entry, launch)

            elapsed   = time.time() - last_fetch
            sleep_for = max(0, refresh_interval - elapsed)
            print(f"\n  Next refresh in {int(sleep_for)}s  (Ctrl+C to quit)\n")
            # Poll every 0.1s — checks both filter change and button state
            sleep_end    = time.time() + sleep_for
            prev_filter  = current_filter
            global BUTTON_LAST_LEVEL
            while time.time() < sleep_end:
                # Poll button via lgpio
                if GPIO_AVAILABLE and _gpio_handle is not None:
                    try:
                        level = lgpio.gpio_read(_gpio_handle, BUTTON_PIN)
                        if level == 0 and BUTTON_LAST_LEVEL == 1:
                            # Falling edge detected — treat as button press
                            on_button_press(BUTTON_PIN)
                        BUTTON_LAST_LEVEL = level
                    except Exception:
                        pass
                with filter_lock:
                    new_filter = vandenberg_only
                if new_filter != prev_filter:
                    break   # filter changed — re-run loop to apply local filter
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n\n[INFO] Shutting down — clearing all displays...")
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
