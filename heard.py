"""Every aircraft heard, and how: 1090 MHz ADS-B, 978 MHz UAT, or no ADS-B
at all.

Two sources fill it:
- each receiver reports every aircraft it heard every few minutes
  (POST /api/ground/census, receiver 4.2+), with the decoder's label for
  each one. The receivers listen on 1090 only: a 978 aircraft reaches them
  only when an FAA ground station rebroadcasts it on 1090 (ADS-R), which the
  decoder labels adsr_*. Those are the 978 aircraft here.
- the tracker, for every tracked aircraft, from the label adsb.lol gives it
  (owner "network").

One row per (owner, aircraft). Kinds are counts of how often each label was
seen, so one odd message does not decide what an aircraft is.
"""
from datetime import datetime, timedelta
from typing import Dict, Optional

NETWORK = "network"
FLUSH_SECONDS = 300
KEEP_DAYS = 365
MAX_REPORT = 3000

LINK_NAMES = {
    "1090": "1090 MHz ADS-B",
    "978": "978 MHz UAT",
    "not1090": "Not on 1090 (likely 978)",
    "none": "No ADS-B out",
    "uncaught": "No ADS-B caught",
    "unknown": "Unknown",
}
# What the aircraft list may show: a label that says something about the aircraft.
TELLING = ("1090", "978", "not1090", "none")


def link_of(kinds: Dict[str, int]) -> str:
    """What the aircraft broadcasts, from the labels it was heard with,
    strongest evidence first.

    - adsb_*: 1090 ADS-B, whatever else it was heard as.
    - adsr_*: a 978 UAT aircraft the FAA rebroadcast on 1090.
    - tisb_*: an FAA radar track rebroadcast for an aircraft sending no ADS-B.
    - mlat: its position had to be worked out from its 1090 transponder
      replies by several receivers, which would have caught any 1090 ADS-B.
      So it is not on 1090: inside a Mode C veil, where ADS-B Out is
      required, that means 978 UAT; elsewhere it may have none.
    - mode_s alone: a transponder reply was caught but no ADS-B message,
      which is what a far-off or weak 1090 aircraft looks like too (seen
      2026-11-19: airliners at 35,000 ft). Says nothing about the aircraft."""
    have = {k for k, v in (kinds or {}).items() if v}
    if any(k.startswith("adsb_") for k in have):
        return "1090"
    if any(k.startswith("adsr_") for k in have):
        return "978"
    if any(k.startswith("tisb_") for k in have):
        return "none"
    if "mlat" in have:
        return "not1090"
    if have & {"mode_s", "mode_ac"}:
        return "uncaught"
    return "unknown"


def links_for(db, icaos) -> Dict[str, str]:
    """What each of these aircraft broadcasts on, from everything that has
    heard it (adsb.lol and every receiver), for the app's aircraft list.
    Aircraft not heard yet, or heard with no telling label, are left out."""
    from models import HeardAircraft
    wanted = {str(i).lower() for i in icaos if i}
    if not wanted:
        return {}
    merged: Dict[str, Dict[str, int]] = {}
    for r in db.query(HeardAircraft).filter(HeardAircraft.icao24.in_(list(wanted))).all():
        kinds = merged.setdefault(r.icao24, {})
        for k, v in (r.kinds or {}).items():
            if isinstance(v, (int, float)):
                kinds[k] = kinds.get(k, 0) + int(v)
    out = {}
    for icao, kinds in merged.items():
        link = link_of(kinds)
        if link in TELLING:
            out[icao] = link
    return out


# ── US registration from the ICAO address ────────────────────────────────────
# US addresses A00001 to ADF7C7 are assigned to N-numbers in order, so the
# N-number can be worked out from the address.
_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"          # no I or O
_DIGITS = "0123456789"
_SUFFIX = 1 + len(_LETTERS) * (1 + len(_LETTERS))       # 601
_B4 = 1 + len(_LETTERS) + len(_DIGITS)                  # 35
_B3 = len(_DIGITS) * _B4 + _SUFFIX                      # 951
_B2 = len(_DIGITS) * _B3 + _SUFFIX                      # 10111
_B1 = len(_DIGITS) * _B2 + _SUFFIX                      # 101711


def _suffix(offset: int) -> str:
    if offset == 0:
        return ""
    first = _LETTERS[(offset - 1) // (len(_LETTERS) + 1)]
    rest = (offset - 1) % (len(_LETTERS) + 1)
    return first if rest == 0 else first + _LETTERS[rest - 1]


def n_number(icao24: str) -> Optional[str]:
    try:
        i = int(str(icao24).strip().lower().lstrip("~"), 16) - 0xA00001
    except ValueError:
        return None
    if str(icao24).startswith("~") or i < 0 or i >= 9 * _B1:
        return None
    out = "N" + str(i // _B1 + 1)
    rem = i % _B1
    for bucket in (_B2, _B3):
        if rem < _SUFFIX:
            return out + _suffix(rem)
        rem -= _SUFFIX
        out += str(rem // bucket)
        rem %= bucket
    if rem < _SUFFIX:
        return out + _suffix(rem)
    rem -= _SUFFIX
    out += str(rem // _B4)
    rem %= _B4
    return out if rem == 0 else out + (_LETTERS + _DIGITS)[rem - 1]


# ── recording ────────────────────────────────────────────────────────────────

def _clean(icao: str) -> Optional[str]:
    icao = str(icao or "").strip().lower()
    if not icao or len(icao) > 7 or not all(c in "0123456789abcdef~" for c in icao):
        return None
    return icao


def record(db, owner: str, report: Dict[str, dict], now: Optional[datetime] = None) -> int:
    """Adds one report ({icao24: {types, flight, r, min_alt, min_nm, ground}})
    to the owner's rows. Returns how many aircraft it held."""
    from models import HeardAircraft
    now = now or datetime.utcnow()
    clean = {}
    for icao, info in list((report or {}).items())[:MAX_REPORT]:
        icao = _clean(icao)
        if icao is None or not isinstance(info, dict):
            continue
        kinds = info.get("types") if isinstance(info.get("types"), dict) else {}
        kinds = {str(k)[:16]: int(v) for k, v in kinds.items() if isinstance(v, (int, float)) and v > 0}
        clean[icao] = dict(info, types=kinds)
    if not clean:
        return 0
    rows = {r.icao24: r for r in db.query(HeardAircraft).filter(
        HeardAircraft.owner == owner, HeardAircraft.icao24.in_(list(clean))).all()}
    for icao, info in clean.items():
        row = rows.get(icao)
        if row is None:
            row = HeardAircraft(owner=owner, icao24=icao, first_seen=now, kinds={}, reports=0)
            db.add(row)
        kinds = dict(row.kinds or {})
        for k, v in info["types"].items():
            kinds[k] = kinds.get(k, 0) + v
        row.kinds = kinds                       # a new dict, so the change is saved
        row.link = link_of(kinds)
        row.last_seen = now
        row.reports = (row.reports or 0) + 1
        flight = str(info.get("flight") or "").strip()[:10]
        if flight:
            row.callsign = flight
        reg = str(info.get("r") or "").strip()[:10] or n_number(icao)
        if reg:
            row.registration = reg
        alt = info.get("min_alt")
        if isinstance(alt, (int, float)):
            row.min_alt = int(alt) if row.min_alt is None else min(row.min_alt, int(alt))
        nm = info.get("min_nm")
        if isinstance(nm, (int, float)) and 0 <= nm <= 400:
            row.min_nm = round(float(nm), 1) if row.min_nm is None else min(row.min_nm, round(float(nm), 1))
        if info.get("ground"):
            row.ground = True
    db.commit()
    return len(clean)


# The tracker's view of tracked aircraft, gathered between flushes.
_network: Dict[str, dict] = {}
_last_flush: Optional[datetime] = None
_last_prune: Optional[datetime] = None


def note_network(icao24: str, ac: dict) -> None:
    """One tracker cycle's adsb.lol answer for one tracked aircraft."""
    kind = ac.get("type")
    if not kind:
        return
    e = _network.setdefault(icao24, {"types": {}})
    k = str(kind)[:16]
    e["types"][k] = e["types"].get(k, 0) + 1
    if ac.get("flight"):
        e["flight"] = str(ac["flight"]).strip()
    if ac.get("r"):
        e["r"] = str(ac["r"]).strip()
    alt = ac.get("alt_baro")
    if isinstance(alt, (int, float)):
        e["min_alt"] = min(e.get("min_alt", alt), alt)
    elif alt == "ground":
        e["ground"] = True


def flush(db_factory, now: Optional[datetime] = None) -> None:
    """Every few minutes: saves the tracker's view, and once a day drops
    aircraft not heard for a year. Never raises."""
    global _last_flush, _last_prune
    now = now or datetime.utcnow()
    if _last_flush is not None and (now - _last_flush).total_seconds() < FLUSH_SECONDS:
        return
    _last_flush = now
    try:
        db = db_factory()
        try:
            if _network:
                record(db, NETWORK, dict(_network), now)
                _network.clear()
            if _last_prune is None or now - _last_prune >= timedelta(days=1):
                from models import HeardAircraft
                db.query(HeardAircraft).filter(HeardAircraft.last_seen < now - timedelta(days=KEEP_DAYS)).delete()
                db.commit()
                _last_prune = now
        finally:
            db.close()
    except Exception as e:
        print(f"Heard aircraft not saved: {type(e).__name__}: {e}")
        _network.clear()
