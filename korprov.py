#!/usr/bin/env python3
"""Hitta den tidigaste lediga uppkörningstiden (körprov B, automat) i hela Sverige.

Skriptet använder samma (odokumenterade) API som Trafikverkets bokningssida
https://fp.trafikverket.se/Boka/ och kräver att du är inloggad med BankID.
Det bokar ingenting, det läser bara lediga tider.

  python korprov.py login     # öppnar en webbläsare, logga in med BankID
  python korprov.py sok       # söker igenom alla provorter

I stället för `login` kan du klistra in Cookie-headern från din egen inloggade
webbläsare i miljövariabeln KORPROV_COOKIE.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://fp.trafikverket.se/Boka/"
SESSION_FILE = Path(__file__).with_name("session.json")
STOCKHOLM = ZoneInfo("Europe/Stockholm")

# Trafikverkets WAF hänger requesten tills timeout om dessa saknas.
HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "sv-SE",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://fp.trafikverket.se",
    "Referer": "https://fp.trafikverket.se/Boka/ng/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


class SessionExpired(Exception):
    pass


# --------------------------------------------------------------------------- #
# Inloggning
# --------------------------------------------------------------------------- #

def login(timeout_s: int = 300) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright saknas. Kör:\n"
            "  pip install playwright && playwright install chromium\n"
            "eller sätt KORPROV_COOKIE i stället (se README)."
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="sv-SE")
        page = context.new_page()
        page.goto(BASE_URL + "ng/")
        print("Logga in med BankID i webbläsarfönstret som öppnades ...")

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                resp = context.request.post(
                    BASE_URL + "licence-information",
                    data="{}",
                    headers=HEADERS,
                    max_redirects=0,
                    timeout=15_000,
                )
                if resp.ok and (resp.json() or {}).get("data"):
                    context.storage_state(path=str(SESSION_FILE))
                    print(f"Inloggad. Sessionen sparad i {SESSION_FILE.name} (gäller ca 1 timme).")
                    browser.close()
                    return
            except Exception:
                pass
            time.sleep(3)
        browser.close()
        sys.exit("Ingen inloggning inom tidsgränsen.")


def load_cookies() -> dict[str, str]:
    raw = os.environ.get("KORPROV_COOKIE", "").strip()
    if raw:
        if raw.lower().startswith("cookie:"):
            raw = raw[len("cookie:"):]
        cookies = {}
        for part in raw.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep:
                cookies[name] = value
        return cookies

    if not SESSION_FILE.exists():
        raise SessionExpired("Ingen session hittades. Kör `python korprov.py login` först.")
    state = json.loads(SESSION_FILE.read_text())
    cookies = {
        c["name"]: c["value"]
        for c in state.get("cookies", [])
        if "trafikverket.se" in c.get("domain", "")
    }
    if not cookies:
        raise SessionExpired("Sessionsfilen innehåller inga Trafikverket-cookies. Kör `login` igen.")
    return cookies


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

class Client:
    def __init__(self, cookies: dict[str, str]):
        self.http = requests.Session()
        self.http.headers.update(HEADERS)
        self.http.cookies.update(cookies)

    def post(self, path: str, body: dict):
        try:
            r = self.http.post(BASE_URL + path, json=body, timeout=30, allow_redirects=False)
        except requests.Timeout as e:
            raise SessionExpired(f"Timeout mot {path} (WAF eller utgången session).") from e
        if r.status_code in (301, 302, 303, 307, 308, 400, 401, 403):
            raise SessionExpired(
                f"{path} svarade {r.status_code}. BankID-sessionen har troligen gått ut, "
                "kör `python korprov.py login` igen."
            )
        r.raise_for_status()
        return r.json().get("data")


def booking_session(ssn: str, licence_id: int, examination_type_id: int = 0) -> dict:
    return {
        "socialSecurityNumber": ssn,
        "licenceId": licence_id,
        "bookingModeId": 0,
        "ignoreDebt": False,
        "ignoreBookingHindrance": False,
        "examinationTypeId": examination_type_id,
        "excludeExaminationCategories": [],
        "rescheduleTypeId": 0,
        "paymentIsActive": False,
        "paymentReference": None,
        "paymentUrl": None,
        "searchedMonths": 0,
    }


def find_named(items: list[dict], *needles: str) -> dict | None:
    for needle in needles:
        for item in items or []:
            if needle.lower() in str(item.get("name", "")).lower():
                return item
    return None


def unwrap_location(loc: dict) -> dict:
    return loc.get("location", loc) if isinstance(loc, dict) else {}


def iter_occasions(data):
    """Plocka ut alla tillfällen ur svaret oavsett exakt nästling."""
    if isinstance(data, dict):
        if isinstance(data.get("occasions"), list):
            yield from data["occasions"]
        else:
            for v in data.values():
                yield from iter_occasions(v)
    elif isinstance(data, list):
        for v in data:
            yield from iter_occasions(v)


def occasion_start(occ: dict) -> datetime | None:
    start = (occ.get("duration") or {}).get("start")
    if not start and occ.get("date"):
        start = f"{occ['date']}T{occ.get('time') or '00:00'}"
    if not start:
        return None
    try:
        t = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Tider utan tidszon är svensk lokaltid; visa allt i svensk tid.
    return t.replace(tzinfo=STOCKHOLM) if t.tzinfo is None else t.astimezone(STOCKHOLM)


# --------------------------------------------------------------------------- #
# Sökning
# --------------------------------------------------------------------------- #

def search(licence_name: str, exam_name: str, vehicle_name: str, delay: float, top: int) -> int:
    client = Client(load_cookies())

    info = client.post("licence-information", {})
    ssn = info.get("socialSecurityNumber", "")
    licences = [l for cat in info.get("licenceCategories") or [] for l in cat.get("licences") or []]
    wanted = licence_name.strip().upper()
    licence = next((l for l in licences if str(l.get("name", "")).strip().upper() == wanted), None) or next(
        (l for l in licences if str(l.get("name", "")).strip().upper().startswith(wanted + " ")), None)
    if not licence:
        sys.exit(f"Hittade ingen behörighet '{licence_name}'. Tillgängliga: "
                 + ", ".join(str(l.get("name")) for l in licences))

    base = client.post("search-information", {"bookingSession": booking_session(ssn, licence["id"])})
    exam = find_named(base.get("examinationTypes"), exam_name)
    if not exam:
        sys.exit(f"Hittade ingen provtyp '{exam_name}'. Tillgängliga: "
                 + ", ".join(str(e.get("name")) for e in base.get("examinationTypes") or []))

    session = booking_session(ssn, licence["id"], exam["id"])
    opts = client.post("search-information", {"bookingSession": session}) or base

    vehicles = opts.get("vehicleTypes") or base.get("vehicleTypes") or []
    vehicle = find_named(vehicles, vehicle_name)
    if not vehicle:
        sys.exit(f"Hittade ingen fordonstyp '{vehicle_name}'. Tillgängliga: "
                 + ", ".join(str(v.get("name")) for v in vehicles))

    languages = opts.get("languages") or []
    language = find_named(languages, "svenska")
    language_id = language["id"] if language else opts.get("languageId")

    locations = [unwrap_location(l) for l in opts.get("locations") or base.get("locations") or []]
    locations = [l for l in locations if l.get("id") is not None]
    if not locations:
        sys.exit("Fick inga provorter från Trafikverket.")

    print(f"Söker {exam['name']} ({licence['name']}, {vehicle['name']}) på {len(locations)} provorter ...\n")

    start_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    earliest: list[tuple[datetime, str, dict]] = []
    failed: list[str] = []

    for i, loc in enumerate(locations, 1):
        name = loc.get("name") or str(loc["id"])
        query = {
            "startDate": start_date,
            "searchedMonths": 0,
            "locationId": loc["id"],
            "nearbyLocationIds": [],
            "languageId": language_id,
            "vehicleTypeId": vehicle["id"],
            "tachographTypeId": opts.get("tachographTypeId"),
            "occasionChoiceId": opts.get("occasionChoiceId"),
            "examinationTypeId": exam["id"],
        }
        query = {k: v for k, v in query.items() if v is not None}
        try:
            data = client.post("occasion-bundles", {"bookingSession": session, "occasionBundleQuery": query})
        except requests.RequestException as e:
            failed.append(f"{name} ({e.__class__.__name__})")
            continue

        times = [(occasion_start(o), o) for o in iter_occasions(data)]
        times = [(t, o) for t, o in times if t]
        if times:
            t, occ = min(times, key=lambda x: x[0])
            earliest.append((t, occ.get("locationName") or name, occ))
            status = t.strftime("%Y-%m-%d %H:%M")
        else:
            status = "inga tider"
        print(f"  [{i:>3}/{len(locations)}] {name:<28} {status}", flush=True)
        time.sleep(delay * random.uniform(0.8, 1.2))

    earliest.sort(key=lambda x: x[0])
    print()
    if not earliest:
        print("Inga lediga tider hittades på någon provort.")
    else:
        print(f"Tidigaste lediga tider ({min(top, len(earliest))} av {len(earliest)} orter med tider):\n")
        print(f"  {'Datum':<10} {'Tid':<5}  {'Provort':<28} Pris")
        for t, place, occ in earliest[:top]:
            print(f"  {t:%Y-%m-%d} {t:%H:%M}  {place:<28} {occ.get('cost') or ''}")
        t, place, _ = earliest[0]
        print(f"\nTIDIGAST I SVERIGE: {t:%Y-%m-%d kl %H:%M} i {place}")
        print("Boka själv på https://fp.trafikverket.se/Boka/ (tiderna kan försvinna snabbt).")
    if failed:
        print(f"\nKunde inte läsa {len(failed)} orter: " + ", ".join(failed))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="logga in med BankID och spara sessionen")
    s = sub.add_parser("sok", help="sök tidigaste tid på alla provorter")
    s.add_argument("--behorighet", default="B")
    s.add_argument("--prov", default="Körprov")
    s.add_argument("--fordon", default="Automat")
    s.add_argument("--fordrojning", type=float, default=1.5, help="sekunder mellan anrop (var snäll mot servern)")
    s.add_argument("--topp", type=int, default=15, help="antal orter i topplistan")
    args = p.parse_args()

    try:
        if args.cmd == "login":
            login()
            return 0
        return search(args.behorighet, args.prov, args.fordon, args.fordrojning, args.topp)
    except SessionExpired as e:
        print(f"Fel: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
