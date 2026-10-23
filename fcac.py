r"""
FCAC - Fenix Cabin Announcements Configurator: system tray app for custom MSFS 2024 cabin announcements.

* Reads the airline and destination of your latest SimBrief flight plan.
* Takes the matching recording  <AIRLINE>_<ICAO>.ogg  from  Airlines\<AIRLINE>\dep | dest | fuel  and
  puts it in your Fenix airline pack, replacing the generic announcement files.
* Airlines\<AIRLINE>\main holds the airline's general PAs (SafetyBriefing, ArmDoors...); installing a pack puts them there.
* Downloads and installs airline packs from an online catalog into the same Airlines folder, with one-click uninstall.

Command line (handy for testing; with no arguments the tray app starts):
    python fcac.py --community              show the detected MSFS 2024 Community folder
    python fcac.py --add-airline BAW AAL    create Airlines\BAW\dep|dest|fuel (and the same for AAL)
    python fcac.py --list-packs             list packs in the catalog
    python fcac.py --install BAW            download + install a pack
    python fcac.py --uninstall BAW          remove an installed pack (restores any pack that was there before)
    python fcac.py --restore BAW            put the airline's original generic announcements back
    python fcac.py --once                   one update from SimBrief
    python fcac.py --once --airline BAW --dest BIKF     skip SimBrief, force a flight
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import url2pathname

import requests

try:
    import fcac_audio  # joins announcement pieces (needs numpy + soundfile)
except Exception:  # missing libraries must not stop the rest of the app
    fcac_audio = None
import fcac_studio  # Pack Studio (record / upload, PA sound, loudness, export); imports fcac_audio itself

APP_NAME = "FCAC - Fenix Cabin Announcements Configurator"
VERSION = "1.0.1"
BASE_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
)
CONFIG_PATH = Path(os.environ.get("FCAC_CONFIG", BASE_DIR / "config.json"))
ALIASES_PATH = CONFIG_PATH.with_name("aliases.json")
DEFAULT_ALIASES = {"SHT": "BAW", "CFE": "BAW", "EFW": "BAW"}  # BA Shuttle, BA CityFlyer, BA EuroFlyer -> the BAW pack


def _data_dir() -> Path:
    env = os.environ.get("FCAC_DATA")
    if env:
        return Path(env)
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "FCAC"
    return Path.home() / ".fcac"


DATA_DIR = _data_dir()
GENERIC_DIR = DATA_DIR / "generic"    # untouched copy of an airline's generic announcements (saved before FCAC changes them)
BACKUPS_DIR = DATA_DIR / "backups"    # packs that were already in the Fenix folder before we installed ours
STATE_PATH = DATA_DIR / "state.json"
STUDIO_DIR = DATA_DIR / "studio"        # Pack Studio projects (recordings, processed clips, exports)
LOG_PATH = DATA_DIR / "announcer.log"
for _d in (DATA_DIR, GENERIC_DIR, BACKUPS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SIMBRIEF_URL = "https://www.simbrief.com/api/xml.fetcher.php"
STORE_PACKAGE = "Microsoft.Limitless_8wekyb3d8bbwe"   # MSFS 2024, Microsoft Store / Xbox app
STEAM_APPID = "2537590"                               # MSFS 2024 on Steam
KOFI_URL = "https://ko-fi.com/sjames1066"
MAIN_FOLDER = "main"                                  # Airlines\<ICAO>\main: the airline's general PAs (SafetyBriefing...)
PARTS_FOLDER = "time"                                 # Airlines\<ICAO>\time: greetings + local-time clips joined into the PAs
LEGACY_PARTS = "parts"                                # the old name of the time folder, still read
EXTRA_FOLDER = "extra"                                # Airlines\<ICAO>\extra: bolt-on announcements (nut allergy, Wi-Fi down...)
VOICES_FOLDER = "voices"                              # Airlines\<ICAO>\voices\<Name>: another voice artist's set of recordings
DEFAULT_VOICE = "Default"                             # the recordings directly in Airlines\<ICAO>
ZIP_FOLDERS = (MAIN_FOLDER, PARTS_FOLDER, LEGACY_PARTS, EXTRA_FOLDER, VOICES_FOLDER, "dep", "dest", "fuel")    # the only top-level folders read from a pack zip
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024

DEFAULT_CONFIG = {
    "simbrief_username": "",
    "simbrief_userid": "",
    "catalog_url": "https://raw.githubusercontent.com/sjames1066/fcac/main/catalog.json",
    "community_dir": "",              # leave empty to auto-detect
    "airlines_dir": "",               # your recordings library; empty = an 'Airlines' folder next to the app
    "announcements_folder": "fnx-airline-packs",
    "airline_override": "",           # force an airline ICAO instead of using the SimBrief plan
    # Airlines that share another airline's recordings. Edit aliases.json (next to the app) instead of this.
    "airline_aliases": {},
    "auto_poll": True,
    "poll_interval_seconds": 60,
    "notifications": True,
    "minimize_to_tray": False,        # minimizing the FCAC window hides it in the tray instead of the taskbar
    "on_close": "tray",               # closing the FCAC window: "tray" (keep running) or "quit"
    "extra_chance": 25,               # percent of flights that get one random bolt-on from the extra folder (0 = never)
    "announce_local_time": True,      # say "where the local time is ..." in the landing PA (needs the parts pieces)
    "landing_speed_knots": 60,        # the landing PA is built once you have landed and slowed to this ground speed
    "segment_pause_ms": 60,           # pause between the pieces of a joined PA (see the parts folder)
    "quit_with_msfs": True,           # when started with MSFS: close FCAC when the sim closes
    # Which pack files each folder replaces. Variants (e.g. BoardingWelcome[Morning].ogg) are found in the pack.
    "mappings": [
        {"folder": "dep", "base": "BoardingWelcome", "exclude_tags": ["Refueling"], "default_name": "BoardingWelcome"},
        {"folder": "dest", "base": "AfterLanding", "default_name": "AfterLanding"},
        {"folder": "fuel", "base": "BoardingWelcome", "require_tags": ["Refueling"],
         "default_name": "BoardingWelcome[Refueling]"},
    ],
}

log = logging.getLogger("fcac")

DEBUG = True  # shows a red debug panel in the window (what was built, live landing events). Set False for a public release.


class _DebugBuffer(logging.Handler):
    """Keeps the last few log lines for the red debug panel."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{time.strftime('%H:%M:%S')}  {record.getMessage()}")
        del self.lines[:-60]


DEBUG_BUFFER = _DebugBuffer()
log.addHandler(DEBUG_BUFFER)
log.setLevel(logging.INFO)


class AppError(Exception):
    """An error with a message that is safe to show the user."""


# --------------------------------------------------------------------------- config / state
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        raise AppError(f"Created {CONFIG_PATH.name} - please edit it (SimBrief name, catalog_url).")
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AppError(f"config.json is not valid JSON: {exc}") from exc
    return {**DEFAULT_CONFIG, **user}


def set_config_value(key: str, value) -> None:
    """Change one setting in config.json, keeping everything else the user has there."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else dict(DEFAULT_CONFIG)
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(f"config.json could not be read: {exc}") from exc
    data[key] = value
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_aliases(cfg: dict) -> dict[str, str]:
    """Airlines that use another airline's recordings, e.g. {"SHT": "BAW"}: a flight with airline SHT uses
    Airlines\\BAW and fills the Fenix pack folder named SHT. Edit aliases.json next to the app (created on first run)."""
    if not ALIASES_PATH.exists():
        try:
            ALIASES_PATH.write_text(json.dumps(DEFAULT_ALIASES, indent=2) + "\n", encoding="utf-8")
        except OSError:
            log.warning("Could not create %s", ALIASES_PATH)
    try:
        data = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        raise AppError(f"aliases.json could not be read ({exc}) - it should look like {{\"SHT\": \"BAW\"}}.") from exc
    merged = {**(cfg.get("airline_aliases") or {}), **data}
    return {str(k).strip().upper(): str(v).strip().upper() for k, v in merged.items() if str(k).strip() and str(v).strip()}


# --------------------------------------------------------------------------- starting with Windows / with MSFS
EXE_XML_NAME = "FCAC"                       # name of our entry in MSFS's EXE.xml
MSFS_PROCESS = "FlightSimulator2024.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> tuple[str, str]:
    """(program, arguments) that start this app: the .exe itself, or pythonw + fcac.py."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    program = windowless if exe.name.lower() == "python.exe" and windowless.exists() else exe
    return str(program), f'"{Path(__file__).resolve()}"'


def msfs_config_dirs() -> list[Path]:
    """Folders that hold MSFS 2024's EXE.xml (next to UserCfg.opt): Microsoft Store and/or Steam edition."""
    found = []
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if local:
        found.append(Path(local) / "Packages" / STORE_PACKAGE / "LocalCache")
    if roaming:
        found.append(Path(roaming) / "Microsoft Flight Simulator 2024")
    return [p for p in found if p.is_dir()]


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ADDON_BLOCK = re.compile(r"[ \t]*<Launch\.Addon>.*?</Launch\.Addon>[ \t]*\r?\n?", re.DOTALL | re.IGNORECASE)
_EXE_XML_NEW = (
    '<?xml version="1.0" encoding="Windows-1252"?>\r\n<SimBase.Document Type="Launch" version="1,0">\r\n'
    "\t<Descr>Launch</Descr>\r\n\t<Filename>EXE.xml</Filename>\r\n\t<Disabled>False</Disabled>\r\n"
    "\t<Launch.ManualLoad>False</Launch.ManualLoad>\r\n</SimBase.Document>\r\n"
)


def _is_ours(block: str) -> bool:
    return re.search(rf"<Name>\s*{EXE_XML_NAME}\s*</Name>", block, re.IGNORECASE) is not None


def exe_xml_without_ours(text: str) -> str:
    return _ADDON_BLOCK.sub(lambda m: "" if _is_ours(m.group(0)) else m.group(0), text)


def exe_xml_with_ours(text: str, program: str, args: str) -> str:
    """EXE.xml text with our <Launch.Addon> entry added (any older copy of it replaced). Other entries are kept."""
    text = exe_xml_without_ours(text)
    block = (
        "\t<Launch.Addon>\r\n\t\t<Disabled>False</Disabled>\r\n\t\t<ManualLoad>False</ManualLoad>\r\n"
        f"\t\t<Name>{EXE_XML_NAME}</Name>\r\n\t\t<Path>{_xml_escape(program)}</Path>\r\n"
        + (f"\t\t<CommandLine>{_xml_escape(args)}</CommandLine>\r\n" if args else "")
        + "\t\t<NewConsole>False</NewConsole>\r\n\t</Launch.Addon>\r\n"
    )
    close = re.search(r"[ \t]*</SimBase\.Document>", text, re.IGNORECASE)
    if not close:
        raise AppError("EXE.xml has an unexpected layout - FCAC left it alone.")
    return text[: close.start()] + block + text[close.start():]


def _read_exe_xml(path: Path) -> str:
    return path.read_bytes().decode("cp1252", errors="replace") if path.exists() else ""


def _write_exe_xml(path: Path, text: str) -> None:
    if path.exists():  # keep a copy of what was there before we first touched it
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, BACKUPS_DIR / f"EXE.xml.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    path.write_bytes(text.encode("cp1252", errors="xmlcharrefreplace"))


def set_msfs_autostart(enabled: bool) -> list[Path]:
    """Add or remove FCAC in MSFS's EXE.xml, so the sim starts it. Returns the files that were changed."""
    dirs = msfs_config_dirs()
    if enabled and not dirs:
        raise AppError("Could not find MSFS 2024's settings folder (run the sim once, then try again).")
    program, args = launch_command()
    changed = []
    for folder in dirs:
        path = folder / "EXE.xml"
        old = _read_exe_xml(path)
        if enabled:
            new = exe_xml_with_ours(old or _EXE_XML_NEW, program, args)
        elif _is_ours(old):
            new = exe_xml_without_ours(old)
        else:
            continue
        if new != old:
            try:
                _write_exe_xml(path, new)
            except OSError as exc:
                raise AppError(f"Could not write {path}: {exc}") from exc
            changed.append(path)
    return changed


def msfs_autostart_enabled() -> bool:
    return any(_is_ours(_read_exe_xml(d / "EXE.xml")) for d in msfs_config_dirs())


def set_windows_autostart(enabled: bool) -> None:
    try:
        import winreg
    except ImportError as exc:
        raise AppError("Starting with Windows only works on Windows.") from exc
    program, args = launch_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, "FCAC", 0, winreg.REG_SZ, f'"{program}" {args}'.strip())
            else:
                try:
                    winreg.DeleteValue(key, "FCAC")
                except FileNotFoundError:
                    pass
    except OSError as exc:
        raise AppError(f"Could not change the Windows startup entry: {exc}") from exc


def windows_autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "FCAC")
            return True
    except (ImportError, OSError):
        return False


def current_autostart() -> str:
    """What is actually set up right now: "msfs", "windows" or "off"."""
    if msfs_autostart_enabled():
        return "msfs"
    return "windows" if windows_autostart_enabled() else "off"


def apply_autostart(mode: str) -> str:
    """Make `mode` ("off", "windows", "msfs") the only way FCAC starts by itself."""
    if mode not in ("off", "windows", "msfs"):
        raise AppError("Unknown startup option.")
    if mode != "windows" and sys.platform.startswith("win"):
        set_windows_autostart(False)
    if mode != "msfs":
        set_msfs_autostart(False)
    if mode == "windows":
        set_windows_autostart(True)
    elif mode == "msfs":
        set_msfs_autostart(True)
    return mode


def msfs_running() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {MSFS_PROCESS}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return MSFS_PROCESS.lower() in out.lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def save_aliases(aliases: dict[str, str]) -> None:
    ALIASES_PATH.write_text(json.dumps(dict(sorted(aliases.items())), indent=2) + "\n", encoding="utf-8")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        log.exception("Could not save state")


# --------------------------------------------------------------------------- MSFS Community folder
def registry_hints() -> dict[str, bool]:
    """Best effort: which MSFS 2024 editions does the Windows registry say are installed for this user?"""
    hints = {"store": False, "steam": False}
    if not sys.platform.startswith("win"):
        return hints
    try:
        import winreg
    except ImportError:
        return hints
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Valve\Steam\Apps\{STEAM_APPID}") as key:
            try:
                hints["steam"] = bool(winreg.QueryValueEx(key, "Installed")[0])
            except OSError:
                hints["steam"] = True
    except OSError:
        pass
    packages = (r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
                r"\AppModel\Repository\Packages")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, packages) as key:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                if name.startswith("Microsoft.Limitless_"):
                    hints["store"] = True
                    break
                index += 1
    except OSError:
        pass
    return hints


def community_from_usercfg(usercfg: Path) -> Path | None:
    """UserCfg.opt's InstalledPackagesPath is where MSFS keeps its package tree (and so Community)."""
    try:
        text = usercfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(r'^\s*InstalledPackagesPath\s+"([^"]+)"', text, re.MULTILINE)
    if not match:
        return None
    root = Path(match.group(1))
    for candidate in (root / "Community", root / "Packages" / "Community"):
        if candidate.is_dir():
            return candidate
    return None


def find_community_dir(cfg: dict) -> Path:
    override = str(cfg.get("community_dir", "")).strip()
    if override:
        path = Path(override)
        if path.is_dir():
            return path
        raise AppError(f"community_dir not found: {path}")

    editions = []  # (edition, UserCfg.opt, default Community folder)
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if local:
        store = Path(local) / "Packages" / STORE_PACKAGE / "LocalCache"
        editions.append(("store", store / "UserCfg.opt", store / "Packages" / "Community"))
    if roaming:
        steam = Path(roaming) / "Microsoft Flight Simulator 2024"
        editions.append(("steam", steam / "UserCfg.opt", steam / "Packages" / "Community"))
    hints = registry_hints()
    editions.sort(key=lambda e: not hints.get(e[0], False))  # editions the registry knows about go first

    for _edition, usercfg, default in editions:
        found = community_from_usercfg(usercfg)
        if found:
            return found
        if default.is_dir():
            return default
    raise AppError("Could not find the MSFS 2024 Community folder. Set community_dir in config.json.")


def airline_packs_dir(cfg: dict) -> Path:
    return find_community_dir(cfg) / str(cfg["announcements_folder"]) / "AirlinePacks"


# --------------------------------------------------------------------------- SimBrief
def fetch_flight(cfg: dict) -> dict:
    """Return {'airline','icao','name'} (airline ICAO, destination ICAO) from the latest SimBrief plan."""
    params = {"json": "1"}
    if str(cfg.get("simbrief_userid", "")).strip():
        params["userid"] = str(cfg["simbrief_userid"]).strip()
    elif str(cfg.get("simbrief_username", "")).strip():
        params["username"] = str(cfg["simbrief_username"]).strip()
    else:
        raise AppError("Enter your SimBrief username: right-click the tray icon -> Set SimBrief username.")

    try:
        resp = requests.get(SIMBRIEF_URL, params=params, timeout=15)
    except requests.RequestException as exc:
        raise AppError(f"Could not reach SimBrief: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    status = str(data.get("fetch", {}).get("status", ""))
    if resp.status_code != 200 or (status and status.lower() != "success"):
        raise AppError(f"SimBrief: {status or resp.status_code} (check your username and that a plan exists).")

    dest = data.get("destination") or {}
    icao = str(dest.get("icao_code", "")).strip().upper()
    if not icao:
        raise AppError("SimBrief plan has no destination.")
    return {
        "icao": icao,
        "name": str(dest.get("name", "")).strip(),
        "airline": str((data.get("general") or {}).get("icao_airline", "")).strip().upper(),
        "arrival": arrival_clock(data),
        **flight_labels(data),
    }


# airline ICAO -> IATA, for showing "BA1336" when SimBrief only gives the ICAO code (a common few; SimBrief's own wins)
IATA = {"BAW": "BA", "SHT": "BA", "CFE": "BA", "EFW": "BA", "AAL": "AA", "DAL": "DL", "UAL": "UA", "SWA": "WN",
        "DLH": "LH", "AFR": "AF", "KLM": "KL", "RYR": "FR", "EZY": "U2", "UAE": "EK", "QTR": "QR", "ETD": "EY",
        "VIR": "VS", "IBE": "IB", "SAS": "SK", "FIN": "AY", "SWR": "LX", "AUA": "OS", "THY": "TK", "QFA": "QF",
        "ANZ": "NZ", "SIA": "SQ", "CPA": "CX", "JAL": "JL", "ANA": "NH", "KAL": "KE", "ACA": "AC", "WJA": "WS",
        "JBU": "B6", "ASA": "AS", "NKS": "NK", "FFT": "F9", "WZZ": "W6", "VLG": "VY", "TAP": "TP", "EIN": "EI",
        "NAX": "DY", "TVF": "TO", "EWG": "EW", "AZA": "AZ", "ITY": "AZ", "LOT": "LO", "CSA": "OK", "ELY": "LY"}


def flight_labels(data: dict) -> dict:
    """{"number": "BA1336", "callsign": "SHT12Q"} for the flight card (either may be empty)."""
    general, atc = data.get("general") or {}, data.get("atc") or {}
    icao = str(general.get("icao_airline", "")).strip().upper()
    digits = str(general.get("flight_number", "")).strip().upper()
    iata = str(general.get("iata_airline") or general.get("airline_iata") or IATA.get(icao, "")).strip().upper()
    callsign = str(atc.get("callsign") or general.get("callsign") or "").strip().upper()
    number = f"{iata or icao}{digits}" if digits else ""
    return {"number": number, "callsign": callsign}


def _number(value) -> float | None:
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def arrival_clock(data: dict) -> str | None:
    """The local time at the destination when the plan has you landing, as "HH:MM" rounded to 5 minutes.
    Landing time (UTC, SimBrief times.sched_on) + the destination's UTC offset. None when SimBrief doesn't say."""
    times, dest = data.get("times") or {}, data.get("destination") or {}
    moment = next((n for n in (_number(times.get(k)) for k in ("sched_on", "est_on", "sched_in", "est_in")) if n), None)
    offset = next((n for n in (_number(dest.get(k)) for k in ("timezone", "utc_offset", "tz_offset", "gmt_offset"))
                   if n is not None), None)
    if moment is None or offset is None:
        log.info("No arrival local time: SimBrief gave landing=%s, destination offset=%s", moment, offset)
        return None
    if abs(offset) > 24:  # seconds rather than hours
        offset /= 3600
    minutes = int(round((moment / 60 + offset * 60) / 5.0)) * 5  # nearest 5 minutes
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --------------------------------------------------------------------------- pack catalog
def _is_http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _local_path(url: str) -> Path:
    return Path(url2pathname(urlparse(url).path)) if url.lower().startswith("file:") else Path(url)


def load_catalog(cfg: dict) -> list[dict]:
    """Catalog JSON: {"packs": [{"icao","name","version","url","sha256"?,"size"?}]}. URL may be relative."""
    url = str(cfg.get("catalog_url", "")).strip()
    if not url or "YOUR-" in url:
        raise AppError("Set catalog_url in config.json to your pack catalog (see README).")
    try:
        if _is_http(url):
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        else:
            data = json.loads(_local_path(url).read_text(encoding="utf-8"))
    except (requests.RequestException, OSError, ValueError) as exc:
        raise AppError(f"Could not load the pack catalog: {exc}") from exc

    packs = []
    for entry in data.get("packs", []):
        icao = str(entry.get("icao", "")).strip().upper()
        raw_url = str(entry.get("url", "")).strip()
        if not re.fullmatch(r"[A-Z0-9]{3}", icao) or not raw_url:
            continue
        if _is_http(raw_url):
            resolved = raw_url
        elif _is_http(url):
            resolved = urljoin(url, raw_url)
        else:
            raw = _local_path(raw_url)
            resolved = str(raw if raw.is_absolute() else _local_path(url).parent / raw)
        packs.append({
            "icao": icao,
            "name": str(entry.get("name", icao)),
            "version": str(entry.get("version", "")),
            "sha256": str(entry.get("sha256", "")).lower(),
            "url": resolved,
            "size": int(entry["size"]) if str(entry.get("size", "")).isdigit() else 0,
            "logo": str(entry.get("logo", "")).strip() if _is_http(str(entry.get("logo", "")).strip()) else "",
            "voices": [
                {k: str(v.get(k, "")).strip()[:200] for k in ("name", "artist", "accent", "description")}
                for v in entry.get("voices", []) if isinstance(v, dict) and str(v.get("name", "")).strip()
            ][:12],
        })
    return sorted(packs, key=lambda p: p["icao"])


def download_pack(entry: dict, dest_file: Path, progress=None) -> None:
    digest = hashlib.sha256()
    url = entry["url"]
    try:
        if _is_http(url):
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                with open(dest_file, "wb") as out:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        done += len(chunk)
                        if done > MAX_DOWNLOAD_BYTES:
                            raise AppError("Pack is larger than 1 GB - refusing to download.")
                        out.write(chunk)
                        digest.update(chunk)
                        if progress and total:
                            progress(done, total)
        else:
            shutil.copyfile(_local_path(url), dest_file)
            digest.update(dest_file.read_bytes())
    except (requests.RequestException, OSError) as exc:
        raise AppError(f"Download failed: {exc}") from exc
    expected = entry.get("sha256", "")
    if expected and digest.hexdigest() != expected:
        raise AppError("Download failed its checksum - the file is corrupt or has been changed.")


def extract_pack(zip_path: Path, out_dir: Path) -> None:
    """Extract only main/, dep/, dest/, fuel/ from the zip, refusing anything that escapes out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                if info.is_dir() or not parts or (parts[0] not in ZIP_FOLDERS and parts != ["voice.json"]):
                    continue
                if any(p == ".." or ":" in p for p in parts):
                    raise AppError("Pack zip contains an unsafe path - refusing to install it.")
                target = out_dir.joinpath(*parts)
                if root not in target.resolve().parents:
                    raise AppError("Pack zip contains an unsafe path - refusing to install it.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as exc:
        raise AppError("The downloaded pack is not a valid zip file.") from exc
    main = out_dir / MAIN_FOLDER
    if not main.is_dir() or not any(p.is_file() for p in main.iterdir()):
        raise AppError(f"The pack zip has no '{MAIN_FOLDER}' folder with announcement files in it.")


# --------------------------------------------------------------------------- the Airlines library
def airlines_root(cfg: dict) -> Path:
    configured = str(cfg.get("airlines_dir", "")).strip()
    return Path(configured) if configured else BASE_DIR / "Airlines"


def airline_folder(cfg: dict, icao: str) -> Path:
    return airlines_root(cfg) / icao.upper()


def check_airline_code(icao: str) -> str:
    icao = icao.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3}", icao):
        raise AppError(f"'{icao}' is not a 3-character airline code (like BAW).")
    return icao


def ensure_airline_folders(cfg: dict, icao: str) -> tuple[Path, bool]:
    """Create Airlines\\<ICAO>\\main, parts, dep, dest, fuel if they are missing. Returns (folder, newly_created)."""
    icao = check_airline_code(icao)
    base = airline_folder(cfg, icao)
    created = not base.is_dir()
    try:
        for folder in (MAIN_FOLDER, PARTS_FOLDER, EXTRA_FOLDER, *(m["folder"] for m in cfg["mappings"])):
            (base / folder).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppError(f"Could not create {base}: {exc}") from exc
    return base, created


def has_ogg(folder: Path) -> bool:
    return folder.is_dir() and any(p.suffix.lower() == ".ogg" for p in folder.iterdir() if p.is_file())


def install_pack(cfg: dict, entry: dict, progress=None) -> str:
    """progress(text, fraction) is called as it goes; fraction is 0..1 (or None when unknown)."""
    """Install a catalog pack: its files go into Airlines\\<ICAO> (never over your own recordings) and its generic
    files into the Fenix AirlinePacks folder. A pack that was already in the Fenix folder is set aside and restored
    when you uninstall."""
    icao = entry["icao"]
    fenix_dir = airline_packs_dir(cfg) / icao
    lib_dir = airline_folder(cfg, icao)
    state = load_state()
    installed = state.setdefault("installed", {})
    record = installed.get(icao) or {}
    ours = set(record.get("files", []))

    def report(text: str, fraction: float | None = None) -> None:
        if progress:
            progress(text, fraction)

    def downloading(done: int, total: int) -> None:
        report(f"Downloading  {done / 1e6:.0f} of {total / 1e6:.0f} MB", 0.8 * done / total)

    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        zip_path = Path(tmp) / f"{icao}.zip"
        report("Starting download", 0.0)
        download_pack(entry, zip_path, downloading)
        report("Verified - unpacking", 0.82)
        staging = Path(tmp) / "extract"
        extract_pack(zip_path, staging)
        report("Installing files", 0.88)

        try:
            # 1. Library: add the pack's files. Files you put there yourself are never overwritten.
            added, skipped = [], 0
            files = sorted(p for p in staging.rglob("*") if p.is_file())
            for number, src in enumerate(files, 1):
                if number % 20 == 0:
                    report("Installing files", 0.88 + 0.1 * number / len(files))
                rel = src.relative_to(staging).as_posix()
                dst = lib_dir / rel
                if dst.exists() and rel not in ours:
                    skipped += 1
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                added.append(rel)
            ensure_airline_folders(cfg, icao)

            # 2. Fenix folder: the pack's generic announcements.
            backup = record.get("backup")
            if fenix_dir.exists():
                if record:
                    shutil.rmtree(fenix_dir)  # upgrade of our own install
                else:  # a pack we didn't install is there: set it aside so uninstall can put it back
                    backup_path = BACKUPS_DIR / f"{icao}-{time.strftime('%Y%m%d-%H%M%S')}"
                    shutil.move(str(fenix_dir), str(backup_path))
                    backup = str(backup_path)
            fenix_dir.parent.mkdir(parents=True, exist_ok=True)
            generic = lib_dir / MAIN_FOLDER
            shutil.copytree(generic if has_ogg(generic) else staging / MAIN_FOLDER, fenix_dir,
                            ignore=shutil.ignore_patterns("Modular_*", "modular_*"))
        except OSError as exc:
            raise AppError(f"Could not install {icao} (is MSFS using the files?): {exc}") from exc

    report("Finishing", 0.99)
    installed[icao] = {
        "name": entry.get("name", icao),
        "version": entry.get("version", ""),
        "backup": backup,
        "files": sorted(set(added) | {rel for rel in ours if (lib_dir / rel).is_file()}),  # what uninstall may remove
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state.pop("last", None)  # force the next update to apply the destination
    save_state(state)
    log.info("Installed pack %s v%s (%d files, %d of yours kept)", icao, entry.get("version", ""), len(added), skipped)
    version = f" v{entry['version']}" if entry.get("version") else ""
    note = f" ({skipped} of your own files kept)" if skipped else ""
    return f"Installed {icao} - {entry.get('name', icao)}{version}{note}"


def uninstall_pack(cfg: dict, icao: str) -> str:
    """Remove what install_pack added (your own recordings stay) and restore any pack that was there before."""
    icao = check_airline_code(icao)
    state = load_state()
    record = state.get("installed", {}).get(icao)
    if not record:
        raise AppError(f"The {icao} pack is not installed.")
    try:
        fenix_dir = airline_packs_dir(cfg) / icao
        if fenix_dir.exists():
            shutil.rmtree(fenix_dir)
        backup = record.get("backup")
        restored = False
        if backup and Path(backup).is_dir():
            shutil.move(backup, str(fenix_dir))
            restored = True

        lib_dir = airline_folder(cfg, icao)
        for rel in record.get("files", []):
            (lib_dir / rel).unlink(missing_ok=True)
        for folder in sorted((p for p in lib_dir.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            try:
                folder.rmdir()  # only removes folders that are now empty
            except OSError:
                pass
        try:
            lib_dir.rmdir()
        except OSError:
            pass
        shutil.rmtree(GENERIC_DIR / icao, ignore_errors=True)
    except OSError as exc:
        raise AppError(f"Could not uninstall {icao} (is MSFS using the files?): {exc}") from exc
    del state["installed"][icao]
    state.pop("last", None)
    save_state(state)
    log.info("Uninstalled pack %s", icao)
    return f"Uninstalled {icao}" + (" (your previous pack was restored)" if restored else "")


# --------------------------------------------------------------------------- applying a destination
def pack_tags(filename: str) -> set[str]:
    return {t.lower() for t in re.findall(r"\[([^\]]+)\]", filename)}


def target_names(pristine: Path | None, mapping: dict) -> list[str]:
    """Pack files this mapping replaces: every variant of the base name that passes the tag rules."""
    require = {t.lower() for t in mapping.get("require_tags", [])}
    exclude = {t.lower() for t in mapping.get("exclude_tags", [])}
    names = []
    for path in sorted(pristine.iterdir()) if pristine else []:
        if not path.is_file() or path.suffix.lower() != ".ogg":
            continue
        if re.split(r"[\[~]", path.stem, maxsplit=1)[0] != mapping["base"]:
            continue
        tags = pack_tags(path.name)
        if require <= tags and not (exclude & tags):
            names.append(path.name)
    return names or [f"{mapping['default_name']}.ogg"]


def install_file(src: Path, dst: Path) -> None:
    """Copy via a temp file + atomic replace. Retries because the sim may briefly lock the file."""
    tmp = dst.with_name(dst.name + ".tmp")
    last: Exception | None = None
    for _ in range(6):
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(1)
    tmp.unlink(missing_ok=True)
    raise AppError(f"{dst.name} is locked (is the sim playing it?): {last}")


def generic_source(cfg: dict, airline: str, fenix_dir: Path) -> Path | None:
    """Where the airline's ORIGINAL announcements are kept, so an airport without a recording can go back to them.

    Order: Airlines\\<ICAO>\\main (the airline's general PAs; a catalog pack installs them there), then FCAC's saved
    snapshot. The snapshot is taken from the Fenix pack the first time FCAC is about to change it.
    """
    library_main = airline_folder(cfg, airline) / MAIN_FOLDER
    if has_ogg(library_main):
        return library_main
    snapshot = GENERIC_DIR / airline
    if not has_ogg(snapshot) and has_ogg(fenix_dir):
        snapshot.mkdir(parents=True, exist_ok=True)
        for path in fenix_dir.iterdir():
            if path.is_file() and path.suffix.lower() == ".ogg":
                shutil.copy2(path, snapshot / path.name)
        log.info("Saved a copy of the original %s announcements", airline)
    return snapshot if has_ogg(snapshot) else None


STANDARD = "Standard"  # label for the plain <AIRLINE>_<ICAO>.ogg recording (no terminal/variant suffix)


def find_clips(lib_dir: Path, folder: str, airline: str, dest_icao: str) -> dict[str, Path]:
    """Recordings for a destination in one folder, keyed by variant label (upper case; "" = the plain file).
    BAW_EGLL.ogg -> "",  BAW_EGLL_T5.ogg -> "T5",  BAW_EGLL_Gen.ogg -> "GEN". Matching ignores case."""
    pattern = re.compile(rf"{re.escape(airline)}_{re.escape(dest_icao)}(?:_([A-Z0-9]+))?\.ogg", re.IGNORECASE)
    clips: dict[str, Path] = {}
    folder_path = lib_dir / folder
    if folder_path.is_dir():
        for path in sorted(folder_path.iterdir()):
            match = pattern.fullmatch(path.name)
            if match and path.is_file():
                clips[(match.group(1) or "").upper()] = path
    return clips


def variant_options(all_clips: dict[str, dict[str, Path]]) -> list[str]:
    """Every variant label offered for a destination across dep/dest/fuel, plain first then alphabetical."""
    return sorted({label for clips in all_clips.values() for label in clips}, key=lambda label: (label != "", label))


def variant_name(label: str) -> str:
    return label or STANDARD


CHOOSER = None  # set by the tray app: asks in the FCAC window which recording to use


def choose_default(title: str, prompt: str, options: list[str]) -> tuple[str, bool]:
    """No window available (command line): take the first option."""
    return options[0], False


def pick_variant(airline: str, dest_icao: str, options: list[str], choose) -> str:
    """Return the variant label to use: a remembered choice, or the user's answer to a prompt."""
    key = f"{airline}_{dest_icao}"
    state = load_state()
    remembered = state.get("choices", {}).get(key)
    if remembered is not None and remembered in options:
        return remembered
    names = {variant_name(label): label for label in options}
    answer, remember = choose(
        f"{airline} to {dest_icao}",
        f"There is more than one recording for {dest_icao}. Which would you like?",
        list(names),
    )
    label = names.get(answer, options[0])
    if remember:
        state = load_state()
        state.setdefault("choices", {})[key] = label
        save_state(state)
    return label


TIME_OF_DAY = ("morning", "afternoon", "evening", "night")   # Fenix tags: night 00-06, morning 06-12, afternoon 12-18, evening 18-24


def list_voices(lib_dir: Path) -> dict[str, Path]:
    """Voices of an airline: "Default" (the recordings in Airlines\\<ICAO> itself, if any) and one per folder in
    Airlines\\<ICAO>\\voices that has sound files. Each voice has its own parts / dep / dest / fuel."""
    voices: dict[str, Path] = {}
    if any(has_ogg(lib_dir / folder) for folder in (PARTS_FOLDER, LEGACY_PARTS, EXTRA_FOLDER, "dep", "dest", "fuel")):
        voices[DEFAULT_VOICE] = lib_dir
    root = lib_dir / VOICES_FOLDER
    if root.is_dir():
        for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if path.is_dir() and any(path.rglob("*.ogg")):
                voices[path.name] = path
    return voices


def voice_info(path: Path, key: str) -> dict:
    """What the window shows for a voice: voice.json in its folder ({"name","artist","credit_url","accent",
    "description"}, all optional). Without it, the folder name is the name."""
    info = {"id": key, "name": key, "artist": "", "credit_url": "", "donate_url": "", "accent": "", "description": ""}
    try:
        data = json.loads((path / "voice.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return info
    if isinstance(data, dict):
        for field in ("name", "artist", "credit_url", "donate_url", "accent", "description"):
            value = str(data.get(field, "")).strip()[:200]
            if value:
                info[field] = value
    if not _is_http(info["credit_url"]):
        info["credit_url"] = ""  # "TBC" and the like: nothing to link to
    if not _is_http(info["donate_url"]):
        info["donate_url"] = ""
    return info


def choose_voice(source: str, lib_dir: Path, voice: str | None = None) -> tuple[Path, str, int]:
    """Which voice's recordings to use: (folder, name, how many voices there are). `voice` forces one; otherwise the
    one picked in the window (a name, or "random" for a different one each flight), else the first."""
    voices = list_voices(lib_dir)
    if not voices:
        return lib_dir, "", 0
    if voice in voices:
        name = voice
    else:
        wanted = load_state().get("voices", {}).get(source)
        name = random.choice(list(voices)) if wanted == "random" else (wanted if wanted in voices else next(iter(voices)))
    return voices[name], name, len(voices)


def _find_piece(base: Path, names: list[str], subs: tuple[str, ...]) -> Path | None:
    for sub in subs:
        folder = base / sub
        if folder.is_dir():
            files = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
            for name in names:
                if f"{name}.ogg".lower() in files:
                    return files[f"{name}.ogg".lower()]
    return None


def part_file(base: Path, name: str) -> Path | None:
    """A greeting or time piece, e.g. Hour_04.ogg, from Airlines\\<ICAO>\\time (case-insensitive)."""
    return _find_piece(base, [name], (PARTS_FOLDER, LEGACY_PARTS))


def modular_piece(base: Path, kind: str) -> Path | None:
    """The rest of a modular PA: Modular_Welcome.ogg or Modular_Landing.ogg (in main, next to the generic PAs)."""
    return _find_piece(base, [f"Modular_{kind}", f"{kind}_Rest"], (MAIN_FOLDER, PARTS_FOLDER, LEGACY_PARTS))


def is_modular(name: str) -> bool:
    return name.lower().startswith("modular_")


def fuel_tag(base: Path) -> Path | None:
    """The tag-on said while refuelling: the first sound file in fuel that isn't an airport clip (BAW_EGLL.ogg)."""
    folder = base / "fuel"
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix.lower() == ".ogg" and not re.match(r"[A-Za-z0-9]{3}_[A-Za-z0-9]{4}", path.name):
                return path
    return None


def pick_extra(cfg: dict, base: Path, key: str) -> Path | None:
    """Now and then a random bolt-on from the extra folder (nut allergy, Wi-Fi unavailable...), chosen once per
    flight so rebuilding the PA (voice change, refuelling) keeps the same one."""
    folder = base / EXTRA_FOLDER
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".ogg") if folder.is_dir() else []
    state = load_state()
    used = state.setdefault("extra_used", {})
    if key not in used:
        chance = max(0, min(100, int(cfg.get("extra_chance", 25))))
        used[key] = random.choice(files).name if files and random.random() * 100 < chance else ""
        save_state(state)
    return next((p for p in files if p.name == used[key]), None)


def clock_tag() -> str:
    hour = time.localtime().tm_hour
    return "night" if hour < 6 else "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"


def welcome_pieces(lib_dir: Path, clip: Path, tag: str, tail: list[Path] | None = None) -> list[Path] | None:
    """Good morning/afternoon/evening + destination + rest of the welcome aboard (+ fuel tag-on, + a bolt-on), or None
    if a piece is missing."""
    greeting = part_file(lib_dir, f"Welcome_{tag.capitalize()}") or (part_file(lib_dir, "Welcome_Evening") if tag == "night" else None)
    pieces = [greeting, clip, modular_piece(lib_dir, "Welcome")]
    return [*pieces, *(tail or [])] if all(pieces) else None


def landing_pieces(lib_dir: Path, clip: Path, arrival: str | None, want_time: bool = True) -> tuple[list[Path] | None, str]:
    """Welcome to <destination> + "local time is HH:MM" + rest of the landing PA.
    Returns (pieces or None, a note about the time that could not be added)."""
    rest = modular_piece(lib_dir, "Landing")
    if not rest:
        return None, ""
    pieces, note = [clip], ""
    if not want_time:
        pass  # switched off in Settings
    elif arrival:
        hour, minute = arrival.split(":")
        twelve = int(hour) % 12 or 12  # PAs use the 12-hour clock: 14:35 -> "two thirty-five"
        needed = [part_file(lib_dir, f"Hour_{twelve:02d}") or part_file(lib_dir, f"Hour_{hour}")]
        if minute != "00" or part_file(lib_dir, "Min_00"):
            needed.append(part_file(lib_dir, f"Min_{minute}"))
        if all(needed):
            meridiem = part_file(lib_dir, "PM" if int(hour) >= 12 else "AM")  # optional "AM" / "PM" clip after the minutes
            pieces += [p for p in (part_file(lib_dir, "LocalTime"), *needed, meridiem) if p]
        else:
            note = f"local time {arrival} skipped (a Hour_{twelve:02d} / Min_{minute} piece is missing)"
    else:
        note = "local time skipped (SimBrief gave no arrival time)"
    return pieces + [rest], note


def apply_destination(cfg: dict, airline: str, dest_icao: str, choose=None, variant: str | None = None,
                      arrival: str | None = None, only: set[str] | None = None, voice: str | None = None) -> str:
    """Apply a destination. If several recordings exist for it (terminals etc.) `choose` is asked which to use
    (`variant` skips the question)."""
    airline = check_airline_code(airline)  # the airline in the Fenix livery, e.g. SHT
    aliases = load_aliases(cfg)
    source = check_airline_code(aliases.get(airline, airline))  # whose recordings to use, e.g. BAW
    lib_dir, created = ensure_airline_folders(cfg, source)
    voice_dir, voice_name, voice_count = choose_voice(source, lib_dir, voice)  # clips and parts come from the voice
    log.info("Applying %s to %s: recordings %s, voice %s, planned landing time %s%s", airline, dest_icao, source,
             voice_name or "-", arrival or "none", f", only {sorted(only)}" if only else "")
    fenix_dir = airline_packs_dir(cfg) / airline
    if source == airline or not has_ogg(lib_dir / MAIN_FOLDER):
        pristine = generic_source(cfg, airline, fenix_dir)
    else:
        pristine = lib_dir / MAIN_FOLDER  # SHT's pack is built from BAW's general PAs
    if not pristine:
        # Never create an empty/partial pack: Fenix would pick it over its default pack and lose announcements.
        extra = f" Created {lib_dir} for your recordings." if created else ""
        raise AppError(
            f"No Fenix pack for {airline} found in {fenix_dir.parent} - install one first "
            f"(or put its general PA .ogg files in {lib_dir / MAIN_FOLDER}).{extra}"
        )
    if not fenix_dir.is_dir():  # e.g. the folder was deleted: put the complete generic pack back first
        fenix_dir.mkdir(parents=True)
        for path in pristine.iterdir():
            if path.is_file() and path.suffix.lower() == ".ogg" and not is_modular(path.name):
                shutil.copy2(path, fenix_dir / path.name)

    # Your general PAs in Airlines\<ICAO>\main are the source of truth for everything FCAC doesn't swap per
    # destination (SafetyBriefing, ArmDoors...): copy any that are new or changed into the Fenix pack.
    synced = 0
    if pristine == lib_dir / MAIN_FOLDER and not only:
        managed = {name for m in cfg["mappings"] for name in target_names(pristine, m)}
        for path in sorted(pristine.iterdir()):
            if path.is_file() and path.suffix.lower() == ".ogg" and path.name not in managed and not is_modular(path.name):
                dst = fenix_dir / path.name
                if not dst.is_file() or not filecmp.cmp(path, dst, shallow=False):
                    install_file(path, dst)
                    synced += 1

    all_clips = {m["folder"]: find_clips(voice_dir, m["folder"], source, dest_icao) for m in cfg["mappings"]}
    options = variant_options(all_clips)
    chosen = ""
    if variant is not None:
        chosen = variant.upper().strip()
        if chosen == STANDARD.upper():
            chosen = ""
    elif len(options) == 1:
        chosen = options[0]
    elif len(options) > 1:
        chosen = pick_variant(airline, dest_icao, options, choose or CHOOSER or choose_default)

    state = load_state()
    state.setdefault("voice_used", {})[f"{airline}_{dest_icao}"] = voice_name
    save_state(state)
    if len(options) > 1 and variant is None:  # remember which one, so a later rebuild (landing) needn't ask
        state = load_state()
        state.setdefault("variants", {})[f"{airline}_{dest_icao}"] = chosen
        save_state(state)

    summary, custom, notes = [], 0, []
    pause = int(cfg.get("segment_pause_ms", 60))
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as work:
        for mapping in cfg["mappings"]:
            if only and mapping["folder"] not in only:
                continue
            folder = mapping["folder"]
            welcome_mode = bool(modular_piece(voice_dir, "Welcome"))
            # Modular welcome: the dep clip is only the destination name, and the same clip (+ the fuel tag-on) builds
            # the refuelling variant. Modular landing: the dest clip is "welcome to <destination>".
            clips = all_clips["dep"] if welcome_mode and folder == "fuel" else all_clips[folder]
            clip = clips.get(chosen) or clips.get("")  # the chosen variant, else the plain file, else generic
            joined = (folder in ("dep", "fuel") and welcome_mode) or (folder == "dest" and modular_piece(voice_dir, "Landing"))
            extra = pick_extra(cfg, voice_dir, f"{airline}_{dest_icao}") if joined and folder != "dest" else None
            if extra and f"extra: {extra.stem}" not in notes:
                notes.append(f"extra: {extra.stem}")
            built = 0
            for name in target_names(pristine, mapping):
                dst, generic = fenix_dir / name, (pristine / name if pristine else None)
                pieces = None
                if joined and clip:
                    if folder == "dest":
                        pieces, note = landing_pieces(voice_dir, clip, arrival, bool(cfg.get("announce_local_time", True)))
                        if note and note not in notes:
                            notes.append(note)
                    else:
                        tags = pack_tags(name) & set(TIME_OF_DAY)
                        tag = next(iter(tags)) if tags else (clock_tag() if folder == "fuel" else None)
                        tail = ([fuel_tag(voice_dir)] if folder == "fuel" else []) + [extra]
                        pieces = welcome_pieces(voice_dir, clip, tag, [t for t in tail if t]) if tag else None
                if pieces:
                    out = Path(work) / name
                    if fcac_audio is None or not fcac_audio.have_backend():
                        raise AppError("Joining PA pieces needs the numpy and soundfile libraries "
                                       "(run Setup FCAC.bat again, or use the latest FCAC.exe).")
                    try:
                        fcac_audio.join(pieces, out, pause)
                    except (fcac_audio.AudioError, OSError) as exc:
                        raise AppError(f"Could not join the pieces for {name}: {exc}") from exc
                    install_file(out, dst)
                    log.info("Built %s = %s", name, " + ".join(p.stem for p in pieces))
                    built += 1
                elif clip and not joined:
                    install_file(clip, dst)
                    built += 1
                elif generic and generic.is_file():
                    install_file(generic, dst)  # no recording (or no pieces) for this airport: back to the generic file
                else:
                    dst.unlink(missing_ok=True)
            custom += bool(built)
            summary.append(f"{mapping['folder']}: {'custom' if built else 'generic'}")

    shown = f"{dest_icao} {variant_name(chosen)}" if len(options) > 1 or chosen else dest_icao
    message = f"{airline} to {shown} ({', '.join(summary)})" + (f" using {source} recordings" if source != airline else "")
    if voice_count > 1:
        message += f" - voice: {voice_info(voice_dir, voice_name)['name']}"
    if notes:
        message += " - " + "; ".join(notes)
    if synced:
        message += f" - updated {synced} general PA file(s) from main"
    if created:
        message += f" - created {lib_dir}, add your recordings there"
    elif not custom:
        message += f" - no recordings for {source}_{dest_icao} yet"
    return message


def rebuild_landing(cfg: dict, arrival: str | None) -> str:
    """You have landed and slowed down: rebuild the landing PA with the local time the sim reported (HH:MM)."""
    state = load_state()
    flight = state.get("flight") or {}
    airline, dest = flight.get("airline"), flight.get("dest")
    if not airline or not dest:
        raise AppError("No flight is set up, so there is no landing PA to build.")
    if not cfg.get("announce_local_time", True):
        return f"Landed at {dest}"
    if not arrival:
        return f"Landed at {dest} - no local time from the sim, kept the PA as it was"
    stored = state.get("variants", {}).get(f"{airline}_{dest}")
    message = apply_destination(cfg, airline, dest, choose=choose_default, variant=stored, arrival=arrival, only={"dest"},
                                voice=state.get("voice_used", {}).get(f"{airline}_{dest}") or None)
    state = load_state()
    state["live"] = f"{airline}_{dest}"  # the next SimBrief check must not undo this
    save_state(state)
    return f"Landed - {message}"


def restore_generic(cfg: dict, icao: str) -> str:
    """Put an airline's original generic announcements back in the Fenix folder."""
    icao = check_airline_code(icao)
    fenix_dir = airline_packs_dir(cfg) / icao
    source = generic_source(cfg, icao, fenix_dir)
    if not source:
        raise AppError(f"No saved original {icao} announcements yet (FCAC saves them the first time it changes a pack).")
    fenix_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        if path.is_file() and path.suffix.lower() == ".ogg" and not is_modular(path.name):
            install_file(path, fenix_dir / path.name)
    state = load_state()
    state.pop("last", None)
    save_state(state)
    return f"Restored the original {icao} announcements"


def update(cfg: dict, force: bool = False, airline: str | None = None, dest: str | None = None,
           variant: str | None = None) -> tuple[bool, str]:
    """Look up the flight (or use the given one) and apply it. Returns (changed, message)."""
    flight = {"icao": dest.upper(), "airline": "", "arrival": None, "number": "", "callsign": ""} if dest \
        else fetch_flight(cfg)
    code = (airline or cfg.get("airline_override") or flight["airline"] or "").strip().upper()
    if not code:
        raise AppError("The SimBrief plan has no airline ICAO - set airline_override in config.json.")
    code = check_airline_code(code)
    if not re.fullmatch(r"[A-Z0-9]{3,4}", flight["icao"]):
        raise AppError(f"'{flight['icao']}' is not a valid airport code.")
    key = f"{code}_{flight['icao']}_{flight.get('arrival') or ''}"  # a new landing time rebuilds the landing PA
    state = load_state()
    shown = {"airline": code, "dest": flight["icao"], "name": flight.get("name", ""),
             "number": flight.get("number", ""), "callsign": flight.get("callsign", "")}
    if state.get("flight") != shown:
        state["flight"] = shown
        save_state(state)
    if not force and state.get("live") == f"{code}_{flight['icao']}":
        return False, f"{code} to {flight['icao']}: landing PA built from the sim's local time"
    if not force and state.get("last") == key:
        return False, f"Unchanged: {code} to {flight['icao']}"
    message = apply_destination(cfg, code, flight["icao"], variant=variant, arrival=flight.get("arrival"))
    state = load_state()
    state["last"] = key
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return True, message


# --------------------------------------------------------------------------- tray app
def make_icon_image():
    from fcac_icon import make_icon

    return make_icon(64)


def open_path(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        log.exception("Could not open %s", path)


# --------------------------------------------------------------------------- airline logos
LOGO_DIR = DATA_DIR / "logos"
_logo_cache: dict = {}
_PALETTES = [((107, 47, 214), (206, 58, 128)), ((206, 58, 128), (255, 146, 24)), ((60, 90, 220), (107, 47, 214)),
             ((20, 150, 160), (60, 90, 220)), ((255, 146, 24), (214, 60, 60)), ((120, 60, 200), (40, 130, 210))]


def _png_bytes(img) -> bytes:
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _font(size: int):
    import base64
    import io

    from PIL import ImageFont

    try:
        from fcac_fonts import INTER

        return ImageFont.truetype(io.BytesIO(base64.b64decode(INTER["Bold"])), size)
    except Exception:
        pass
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def logo_tile(source) -> "Image.Image":
    """Any logo image -> a 256px rounded white tile with the logo centred (logos are drawn for light backgrounds)."""
    from PIL import Image, ImageDraw

    size = 256
    logo = source.convert("RGBA")
    logo.thumbnail((int(size * 0.74), int(size * 0.74)), Image.LANCZOS)
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.23), fill=255)
    tile.paste((255, 255, 255, 255), (0, 0), mask)
    tile.alpha_composite(logo, ((size - logo.width) // 2, (size - logo.height) // 2))
    return tile


def monogram(icao: str) -> bytes:
    """Stand-in 'logo' when no image was supplied: the ICAO code on a gradient tile (colour picked from the code)."""
    from PIL import Image, ImageChops, ImageDraw

    size = 256
    first, second = _PALETTES[sum(ord(c) * (i + 3) for i, c in enumerate(icao)) % len(_PALETTES)]
    vertical = Image.linear_gradient("L").resize((size, size))
    t = ImageChops.add(vertical, vertical.rotate(90), scale=2)
    tile = Image.composite(Image.new("RGB", (size, size), second), Image.new("RGB", (size, size), first), t).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.23), fill=255)
    tile.putalpha(mask)
    draw = ImageDraw.Draw(tile)
    font = _font(92 if len(icao) <= 3 else 72)
    box = draw.textbbox((0, 0), icao, font=font)
    draw.text(((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1] - 2), icao,
              font=font, fill=(255, 255, 255, 255))
    return _png_bytes(tile)


def cache_remote_logo(entry: dict) -> bool:
    """Download the catalog's logo image for a pack (once). Returns True if a new logo was stored."""
    from PIL import Image
    import io

    url, icao = entry.get("logo", ""), entry["icao"]
    target = LOGO_DIR / f"{icao}.png"
    if not url or target.exists():
        return False
    try:
        resp = requests.get(url, timeout=20, stream=True)
        resp.raise_for_status()
        data = resp.raw.read(3 * 1024 * 1024 + 1, decode_content=True)
        if len(data) > 3 * 1024 * 1024:
            return False
        image = Image.open(io.BytesIO(data))
        image.load()
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_png_bytes(logo_tile(image)))
        return True
    except Exception:
        log.warning("Could not fetch the logo for %s", icao)
        return False


def logo_png(cfg: dict, icao: str) -> bytes:
    """Logo for an airline: your own Airlines/<ICAO>/logo.png, else the pack's logo, else a monogram tile."""
    from PIL import Image

    try:
        icao = load_aliases(cfg).get(icao, icao)
    except AppError:
        pass
    try:
        folder = airline_folder(cfg, icao)
        for name in ("logo.png", "logo.jpg", "logo.jpeg", "logo.webp"):
            path = folder / name
            if path.is_file():
                key = (str(path), path.stat().st_mtime_ns)
                if key not in _logo_cache:
                    _logo_cache[key] = _png_bytes(logo_tile(Image.open(path)))
                return _logo_cache[key]
    except Exception:
        log.warning("Could not read your logo for %s", icao)
    cached = LOGO_DIR / f"{icao}.png"
    if cached.is_file():
        return cached.read_bytes()
    if ("mono", icao) not in _logo_cache:
        _logo_cache[("mono", icao)] = monogram(icao)
    return _logo_cache[("mono", icao)]


# --------------------------------------------------------------------------- the FCAC window (see fcac_ui.py)
class UIMixin:
    """What the window shows and does. Needs: lock, catalog, catalog_error, status, auto, wake, set_status(),
    refresh_catalog(), refresh_menu(), poll_once(), run_task()."""

    def _ui_init(self) -> None:
        import fcac_ui

        self.tasks: dict[str, dict] = {}
        self.prompt: dict | None = None
        self.logo_version = 0
        self.ui = fcac_ui.UIServer(self, DATA_DIR / "ui-profile")

    def open_window(self) -> None:
        self.ui.open_window()

    # data for the page
    def ui_snapshot(self) -> dict:
        try:
            cfg = load_config()
        except AppError:
            cfg = dict(DEFAULT_CONFIG)
        state = load_state()
        installed = state.get("installed", {})
        packs, seen = [], set()
        for entry in self.catalog:
            seen.add(entry["icao"])
            have = installed.get(entry["icao"])
            packs.append({
                "icao": entry["icao"], "name": entry["name"], "version": entry["version"],
                "voices": [v["name"] for v in entry.get("voices", [])],
                "size_mb": round(entry["size"] / 1048576) if entry.get("size") else 0,
                "installed": bool(have), "update": bool(have and have.get("version") != entry["version"]),
                "task": self.tasks.get(entry["icao"]),
            })
        for icao, have in sorted(installed.items()):  # installed packs stay listed even if the catalog is down
            if icao not in seen:
                packs.append({"icao": icao, "name": have.get("name", icao), "version": have.get("version", ""),
                              "size_mb": 0, "installed": True, "update": False, "task": self.tasks.get(icao)})
        prompt = self.prompt
        try:
            aliases = load_aliases(cfg)
        except AppError:
            aliases = {}
        sources = {p["icao"] for p in packs}
        try:
            root = airlines_root(cfg)
            sources |= {p.name.upper() for p in root.iterdir() if p.is_dir() and re.fullmatch(r"[A-Za-z0-9]{3}", p.name)}
        except OSError:
            pass
        flight = state.get("flight") or None
        voices = None
        if flight and flight.get("airline"):
            source = aliases.get(flight["airline"], flight["airline"])
            try:
                names = list(list_voices(airline_folder(cfg, source)))
            except (OSError, AppError):
                names = []
            if names:
                found = list_voices(airline_folder(cfg, source))
                voices = {"source": source, "options": [voice_info(found[n], n) for n in names], "selected": state.get("voices", {}).get(source) or names[0],
                          "used": state.get("voice_used", {}).get(f"{flight['airline']}_{flight.get('dest', '')}", "")}
        company = ""
        if flight and flight.get("airline"):  # "British Airways" for BAW, and for SHT which uses the BAW pack
            names = {p["icao"]: p["name"] for p in packs}
            company = names.get(flight["airline"]) or names.get(aliases.get(flight["airline"], ""), "")
        return {
            "version": VERSION,
            "company": company,
            "debug": DEBUG_BUFFER.lines[-14:] if DEBUG else None,
            "voices": voices,
            "welcome": self._welcome_info(cfg, state),
            "studio": self.studio_snapshot(cfg),
            "settings": {
                "on_close": cfg.get("on_close", "tray"),
                "minimize_to_tray": bool(cfg.get("minimize_to_tray", False)),
                "local_time": bool(cfg.get("announce_local_time", True)),
                "extra_chance": int(cfg.get("extra_chance", 25)),
                "autostart": current_autostart(),
                "quit_with_msfs": bool(cfg.get("quit_with_msfs", True)),
                "windows": sys.platform.startswith("win"),
            },
            "aliases": aliases,
            "alias_sources": sorted(set(sources)),
            "library": str(airlines_root(cfg)),
            "setup_needed": not (str(cfg.get("simbrief_username", "")).strip() or str(cfg.get("simbrief_userid", "")).strip()),
            "username": cfg.get("simbrief_username", ""),
            "flight": state.get("flight"),
            "message": self.status,
            "updated": state.get("updated", ""),
            "auto": self.auto,
            "busy": self.lock.locked() or bool(self.tasks),
            "packs": packs,
            "catalog_error": self.catalog_error,
            "logo_v": self.logo_version,
            "prompt": {k: prompt[k] for k in ("id", "title", "text", "options")} if prompt else None,
        }

    def _welcome_info(self, cfg: dict, state: dict) -> dict | None:
        """First run only: what FCAC is going to change, so the user isn't surprised."""
        if state.get("welcomed"):
            return None
        try:
            fenix = str(airline_packs_dir(cfg))
        except AppError:
            fenix = ""
        return {"fenix": fenix, "originals": str(GENERIC_DIR), "recordings": str(airlines_root(cfg))}

    def ui_logo(self, icao: str) -> tuple[bytes, str]:
        try:
            return logo_png(load_config(), check_airline_code(icao)), "image/png"
        except (AppError, OSError):
            return monogram(icao[:4] or "?"), "image/png"

    def ui_icon(self) -> bytes:
        from fcac_icon import make_icon

        return _png_bytes(make_icon(128))

    def window_minimized(self) -> None:
        """'Hide to the tray' on minimize: close the window (the app keeps running; the tray icon opens it again)."""
        try:
            if not load_config().get("minimize_to_tray"):
                return
        except AppError:
            return
        window = self.ui.window
        if window is not None and window.poll() is None:
            window.terminate()
            self.set_status("FCAC is in the tray - double-click the icon to open the window")

    def window_closed(self) -> None:
        """If 'quit when I close the window' is on, quit after a short grace period (a page reload is not a close)."""
        try:
            if load_config().get("on_close") != "quit":
                return
        except AppError:
            return
        closed_at = time.monotonic()

        def later() -> None:
            time.sleep(4)
            if self.ui.last_poll > closed_at or self.tasks or self.lock.locked():
                return  # the window came back, or something is installing
            log.info("Window closed - quitting as chosen in Settings")
            self.quit()

        threading.Thread(target=later, daemon=True).start()

    # things the page can ask for
    def ui_action(self, name: str, data: dict) -> dict:
        if name == "install":
            entry = next((e for e in self.catalog if e["icao"] == str(data.get("icao", "")).upper()), None)
            if not entry:
                return {"error": "That pack is not in the catalog - refresh the list."}
            self.start_pack_task("install", entry["icao"], entry)
        elif name == "uninstall":
            self.start_pack_task("uninstall", check_airline_code(str(data.get("icao", ""))))
        elif name == "update":
            self.run_task("Updating", self.do_update)
        elif name == "voice":
            chosen = str(data.get("voice", ""))
            flight = load_state().get("flight") or {}
            try:
                cfg = load_config()
                source = load_aliases(cfg).get(flight.get("airline", ""), flight.get("airline", ""))
                names = list(list_voices(airline_folder(cfg, source)))
            except (AppError, OSError):
                return {"error": "No flight set up yet."}
            if chosen != "random" and chosen not in names:
                return {"error": "Unknown voice."}
            state = load_state()
            state.setdefault("voices", {})[source] = chosen
            state.pop("live", None)
            save_state(state)
            self.run_task("Switching voice", self.do_update)
        elif name == "auto":
            self.auto = bool(data.get("on"))
            self.refresh_menu()
            self.wake.set()
        elif name == "username":
            who = str(data.get("name", "")).strip()
            if not re.fullmatch(r"[\w.\- ]{1,60}", who):
                return {"error": "That doesn't look like a SimBrief username."}
            set_config_value("simbrief_username", who)
            set_config_value("simbrief_userid", "")  # an old numeric ID would otherwise win over the new name
            state = load_state()
            state.pop("last", None)
            save_state(state)
            self.set_status(f"SimBrief username set to {who}")
            self.poll_once(force=True) if not self.lock.locked() else self.wake.set()
        elif name in ("alias_add", "alias_remove"):
            alias = str(data.get("alias", "")).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{3}", alias):
                return {"error": "An airline code is 3 letters or digits, like SHT."}
            cfg = load_config()
            try:
                aliases = load_aliases(cfg)
            except AppError as exc:
                return {"error": str(exc)}
            if name == "alias_add":
                source = str(data.get("source", "")).strip().upper()
                if not re.fullmatch(r"[A-Z0-9]{3}", source):
                    return {"error": "Pick the airline whose recordings it should use (3 letters, like BAW)."}
                if source == alias:
                    return {"error": "An airline can't be an alias of itself."}
                if aliases.get(source):
                    return {"error": f"{source} is itself an alias of {aliases[source]} - pick {aliases[source]} instead."}
                aliases[alias] = source
            else:
                aliases.pop(alias, None)
            try:
                save_aliases(aliases)
            except OSError as exc:
                return {"error": f"Could not save aliases.json: {exc}"}
            state = load_state()
            state.pop("last", None)
            save_state(state)
            self.set_status(f"{alias} {'now uses ' + aliases[alias] + ' recordings' if name == 'alias_add' else 'alias removed'}")
            self.wake.set()
        elif name == "settings":
            on_close = data.get("on_close")
            mode = data.get("autostart")
            if on_close not in ("tray", "quit") or mode not in ("off", "windows", "msfs"):
                return {"error": "Unknown setting."}
            try:
                if mode != current_autostart():
                    apply_autostart(mode)
                set_config_value("on_close", on_close)
                set_config_value("minimize_to_tray", bool(data.get("minimize_to_tray")))
                set_config_value("quit_with_msfs", bool(data.get("quit_with_msfs", True)))
                chance = int(data.get("extra_chance", 25))
                if chance in (0, 25, 50) and chance != int(load_config().get("extra_chance", 25)):
                    set_config_value("extra_chance", chance)
                    state = load_state()
                    state.pop("last", None)
                    state.pop("extra_used", None)  # roll again for this flight
                    save_state(state)
                    self.wake.set()
                wanted = bool(data.get("local_time", True))
                if wanted != bool(load_config().get("announce_local_time", True)):
                    set_config_value("announce_local_time", wanted)
                    state = load_state()  # rebuild the landing PA with / without the time on the next check
                    state.pop("last", None)
                    state.pop("live", None)
                    save_state(state)
                    self.wake.set()
            except (AppError, OSError) as exc:
                return {"error": str(exc)}
            self.set_status("Settings saved")
        elif name == "welcomed":
            state = load_state()
            state["welcomed"] = True
            save_state(state)
        elif name == "minimized":
            self.window_minimized()
        elif name == "closing":  # the window was closed: quit if that is what the user chose
            self.window_closed()
        elif name == "refresh":
            self.refresh_catalog()
        elif name == "forget":
            self.forget_choices()
        elif name == "answer":
            prompt = self.prompt
            if prompt and prompt["id"] == data.get("id") and data.get("choice") in prompt["options"]:
                prompt["answer"] = (data["choice"], bool(data.get("remember")))
                prompt["event"].set()
        elif name.startswith("studio_"):
            return self.studio_action(name[7:], data)
        elif name == "open":
            self._open_folder(str(data.get("what", "")))
        else:
            return {"error": "unknown action"}
        return {"ok": True}

    # Pack Studio ---------------------------------------------------------
    def studio_target(self, cfg: dict, icao: str) -> tuple[float, int, str]:
        """Loudness (LUFS) to set recordings to = that of the original announcements -> (LUFS, clips measured, source)."""
        cache = self.__dict__.setdefault("_studio_targets", {})
        if icao in cache:
            return cache[icao]
        self.studio_job("Measuring the original announcements", 0.0)
        try:
            return self._measure_target(cfg, icao, cache)
        finally:
            self.studio_job(None)

    def studio_job(self, label: str | None, fraction: float | None = None) -> None:
        """What the Studio is busy with, shown as a loading bar in the window."""
        self._studio_job = {"label": label, "fraction": fraction} if label else None

    def _measure_target(self, cfg: dict, icao: str, cache: dict) -> tuple[float, int, str]:
        sources = [("this airline's own announcements", airline_folder(cfg, icao) / MAIN_FOLDER), ("saved original announcements", GENERIC_DIR / icao)]
        try:
            sources.append(("your Fenix airline packs", airline_packs_dir(cfg)))
        except (AppError, OSError):
            pass
        result = (fcac_studio.DEFAULT_TARGET, 0, "")
        for label, folder in sources:
            try:
                files = sorted(p for p in folder.rglob("*.ogg") if not is_modular(p.name)) if folder.is_dir() else []
            except OSError:
                files = []
            if files:
                step = max(1, len(files) // 10)
                value, count = fcac_studio.reference_loudness(files[::step], lambda f: self.studio_job("Measuring the original announcements", f))
                if count:
                    result = (value, count, label)
                    break
        cache[icao] = result
        return result

    def studio_snapshot(self, cfg: dict) -> dict:
        current = getattr(self, "studio_current", None)
        out = {"projects": fcac_studio.list_projects(STUDIO_DIR), "current": None, "folder": str(STUDIO_DIR),
               "job": getattr(self, "_studio_job", None)}
        if not current:  # packs already in the library that can be opened for editing
            out["library"] = []
            try:
                for folder in sorted(airlines_root(cfg).iterdir(), key=lambda p: p.name.lower()):
                    if folder.is_dir() and re.fullmatch(r"[A-Za-z0-9]{3}", folder.name):
                        for key, path in list_voices(folder).items():
                            info = voice_info(path, key)
                            out["library"].append({"icao": folder.name.upper(), "voice": key, "name": info["name"], "artist": info["artist"]})
            except OSError:
                pass
        if current:
            try:
                project = fcac_studio.load(STUDIO_DIR, *current)
            except fcac_studio.StudioError:
                return out
            if project["icao"] not in self.__dict__.setdefault("_studio_targets", {}):
                if not getattr(self, "_studio_job", None):  # measure in the background so the window stays responsive
                    threading.Thread(target=self.studio_target, args=(cfg, project["icao"]), daemon=True).start()
                target, count, label = fcac_studio.DEFAULT_TARGET, 0, "measuring..."
            else:
                target, count, label = self.studio_target(cfg, project["icao"])
            rows = fcac_studio.status(STUDIO_DIR, project)
            out["current"] = {
                "icao": project["icao"], "airline": project["airline"], "voice": project["voice"],
                "artist": project.get("artist", ""), "credit_url": project.get("credit_url", ""),
                "donate_url": project.get("donate_url", ""), "accent": project.get("accent", ""),
                "description": project.get("description", ""), "version": project.get("version", "1.0.0"),
                "effect": bool(project.get("effect", True)), "airports": project["airports"], "extras": project["extras"], "music": project.get("music", []),
                "has_script": bool(project.get("scripts")), "origin": bool(project.get("origin")), "target_pending": label == "measuring...", "rows": rows, "missing": len(fcac_studio.missing(STUDIO_DIR, project)),
                "target": target, "target_count": count, "target_source": label,
                "done": sum(1 for r in rows if r["done"]), "total": len(rows)}
        return out

    def studio_project(self, data: dict) -> dict:
        return fcac_studio.load(STUDIO_DIR, check_airline_code(str(data.get("icao", ""))), str(data.get("voice", "")))

    def studio_upload(self, icao: str, voice: str, slot: str, filename: str, body: bytes) -> dict:
        try:
            project = fcac_studio.load(STUDIO_DIR, check_airline_code(icao), voice)
            target = self.studio_target(load_config(), project["icao"])[0]
            return {"ok": True, **fcac_studio.store_raw(STUDIO_DIR, project, slot, body, filename, target)}
        except (fcac_studio.StudioError, AppError) as exc:
            return {"error": str(exc)}

    def studio_audio(self, icao: str, voice: str, slot: str) -> bytes | None:
        try:
            path = fcac_studio.processed_file(STUDIO_DIR, fcac_studio.load(STUDIO_DIR, check_airline_code(icao), voice), slot)
            return path.read_bytes() if path else None
        except (fcac_studio.StudioError, AppError, OSError):
            return None

    def studio_action(self, name: str, data: dict) -> dict:
        try:
            if name == "create":
                project = fcac_studio.create(STUDIO_DIR, str(data.get("icao", "")), str(data.get("airline", "")), str(data.get("voice", "")))
                self.studio_current = (project["icao"], project["voice"])
                return {"ok": True}
            if name == "import":
                icao = check_airline_code(str(data.get("icao", "")))
                cfg = load_config()
                found = list_voices(airline_folder(cfg, icao))
                key = str(data.get("voice", ""))
                if key not in found:
                    return {"error": "That pack is no longer in your library."}
                names = {e["icao"]: e["name"] for e in self.catalog}
                if getattr(self, "_studio_job", None):
                    return {"error": "Still busy - wait for the loading bar to finish."}
                self.studio_job("Opening the pack - copying its clips", 0.0)

                def work() -> None:
                    try:
                        project = fcac_studio.import_pack(STUDIO_DIR, icao, names.get(icao, icao), found[key], key,
                                                          lambda f: self.studio_job("Opening the pack - copying its clips", f))
                        self.studio_current = (project["icao"], project["voice"])
                    except (fcac_studio.StudioError, OSError) as exc:
                        log.warning("Could not open pack for editing: %s", exc)
                        self.set_status(f"Could not open that pack: {exc}", notify=False)
                    finally:
                        self.studio_job(None)

                threading.Thread(target=work, daemon=True).start()
                return {"ok": True}
            if name == "open":
                project = self.studio_project(data)
                self.studio_current = (project["icao"], project["voice"])
                return {"ok": True}
            if name == "discard":
                project = self.studio_project(data)
                fcac_studio.discard(STUDIO_DIR, project["icao"], project["voice"])
                if getattr(self, "studio_current", None) == (project["icao"], project["voice"]):
                    self.studio_current = None
                return {"ok": True}
            if name == "close":
                self.studio_current = None
                return {"ok": True}
            project = self.studio_project(data)
            cfg = load_config()
            if name == "details":
                for key, limit in (("artist", 80), ("accent", 40), ("description", 200), ("version", 20)):
                    if key in data:
                        project[key] = str(data[key]).strip()[:limit]
                for key in ("credit_url", "donate_url"):
                    if key in data:
                        value = str(data[key]).strip()[:300]
                        if value and not _is_http(value):
                            return {"error": "Links must start with http:// or https://"}
                        project[key] = value
                effect = bool(data.get("effect", project.get("effect", True)))
                changed = effect != bool(project.get("effect", True))
                project["effect"] = effect
                fcac_studio.save(STUDIO_DIR, project)
                if changed:
                    fcac_studio.reprocess_all(STUDIO_DIR, project, self.studio_target(cfg, project["icao"])[0])
            elif name == "mode":
                fcac_studio.set_mode(STUDIO_DIR, project, str(data.get("slot", "")), str(data.get("mode", "")))
            elif name == "script":
                if data.get("clear"):
                    project.pop("scripts", None)
                    skipped = 0
                else:
                    found, skipped = fcac_studio.parse_script(project, str(data.get("text", ""))[:60000])
                    if not found:
                        return {"error": "No lines in that script matched a clip. Use lines like  Hour 4: Four  or  time/Hour_04: Four."}
                    project.setdefault("scripts", {}).update(found)
                fcac_studio.save(STUDIO_DIR, project)
                return {"ok": True, "matched": len(project.get("scripts", {})), "skipped": skipped}
            elif name == "airport":
                (fcac_studio.remove_airport if data.get("remove") else fcac_studio.add_airport)(STUDIO_DIR, project, str(data.get("code", "")))
            elif name == "music":
                (fcac_studio.remove_music if data.get("remove") else fcac_studio.add_music)(STUDIO_DIR, project, str(data.get("name", "")))
            elif name == "extra":
                (fcac_studio.remove_extra if data.get("remove") else fcac_studio.add_extra)(STUDIO_DIR, project, str(data.get("name", "")))
            elif name == "delete":
                fcac_studio.delete_slot(STUDIO_DIR, project, str(data.get("slot", "")))
            elif name == "export":
                result = fcac_studio.export(STUDIO_DIR, project, STUDIO_DIR / "exports")
                open_path(Path(result["zip"]).parent)
                return {"ok": True, **result}
            elif name == "install":
                dest = fcac_studio.install_to_library(STUDIO_DIR, project, airline_folder(cfg, project["icao"]))
                state = load_state()
                state.pop("last", None)
                save_state(state)
                self.wake.set()
                return {"ok": True, "path": str(dest)}
            elif name == "folder":
                folder = fcac_studio.project_dir(STUDIO_DIR, project["icao"], project["voice"])
                open_path(folder)
            else:
                return {"error": "unknown action"}
        except (fcac_studio.StudioError, AppError, OSError) as exc:
            return {"error": str(exc)}
        return {"ok": True}

    def _open_folder(self, what: str) -> None:
        try:
            cfg = load_config()
            if what == "airlines":
                target = airlines_root(cfg)
            elif what == "fenix":
                target = airline_packs_dir(cfg)
            elif what == "log":
                open_path(LOG_PATH)
                return
            elif what == "aliases":
                load_aliases(cfg)  # creates the file if it isn't there yet
                open_path(ALIASES_PATH)
                return
            elif what == "kofi":
                import webbrowser

                webbrowser.open(KOFI_URL)
                return
            else:
                return
            target.mkdir(parents=True, exist_ok=True)
            open_path(target)
        except (AppError, OSError) as exc:
            self.set_status(str(exc))

    # install / uninstall with progress
    def start_pack_task(self, kind: str, icao: str, entry: dict | None = None) -> None:
        if icao in self.tasks:
            return
        task = {"kind": kind, "text": "Starting", "frac": 0.0 if kind == "install" else None}
        self.tasks[icao] = task

        last_step = [-1]

        def progress(text: str, fraction: float | None) -> None:
            task["text"], task["frac"] = text, fraction
            step = -1 if fraction is None else int(fraction * 10)  # status line only changes every 10%
            if step != last_step[0]:
                last_step[0] = step
                self.set_status(f"{icao}: {text}")

        def work() -> None:
            ran = False
            try:
                if not self.lock.acquire(blocking=False):
                    self.set_status("Busy - try again in a moment")
                    return
                ran = True
                try:
                    if kind == "install":
                        message = install_pack(load_config(), entry, progress)
                    else:
                        task.update(text="Removing files", frac=None)
                        message = uninstall_pack(load_config(), icao)
                    self.set_status(message, notify=True)
                except AppError as exc:
                    self.set_status(str(exc), notify=True)
                except Exception as exc:
                    log.exception("Unexpected error")
                    self.set_status(f"Error: {exc}", notify=True)
                finally:
                    self.lock.release()
            finally:
                self.tasks.pop(icao, None)
            if ran and kind == "install":
                self.poll_once(force=True)  # apply the current flight with the new files
            self.refresh_menu()

        threading.Thread(target=work, daemon=True).start()

    def ask_choice(self, title: str, text: str, options: list[str]) -> tuple[str, bool]:
        """Called while applying a flight whose airport has several recordings. Waits for the window's answer."""
        import secrets

        event = threading.Event()
        self.prompt = {"id": secrets.token_hex(4), "title": title, "text": text, "options": options,
                       "event": event, "answer": None}
        self.set_status(f"{title}: choose a recording in the FCAC window", notify=True)
        self.open_window()
        event.wait(timeout=600)
        prompt, self.prompt = self.prompt, None
        return (prompt or {}).get("answer") or (options[0], False)


class TrayApp(UIMixin):
    def __init__(self) -> None:
        import pystray

        self.pystray = pystray
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.status = "Starting..."
        self.auto = True
        self.catalog: list[dict] = []
        self.catalog_error = ""
        self._ui_init()
        global CHOOSER
        CHOOSER = self.ask_choice
        self.icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME, self.build_menu())

    # menu ---------------------------------------------------------------
    def build_menu(self):
        item, menu = self.pystray.MenuItem, self.pystray.Menu
        return menu(
            item("Open FCAC window", self.open_window, default=True),
            item(lambda _: self.status, None, enabled=False),
            menu.SEPARATOR,
            item("Update from SimBrief now", lambda: self.run_task("Updating", self.do_update)),
            item("Auto-update", self.toggle_auto, checked=lambda _: self.auto),
            menu.SEPARATOR,
            item("Install airline pack", menu(self.install_items)),
            item("Uninstall airline pack", menu(self.uninstall_items)),
            item("Refresh pack list", lambda: self.refresh_catalog(notify=True)),
            item("Restore original announcements", menu(self.restore_items)),
            item("Forget remembered terminal choices", self.forget_choices),
            menu.SEPARATOR,
            item("Open Airlines folder (your recordings)", self.open_airlines_folder),
            item("Open Fenix airline packs folder", self.open_packs_folder),
            item("Edit config.json", lambda: open_path(CONFIG_PATH)),
            item("View log", lambda: open_path(LOG_PATH)),
            menu.SEPARATOR,
            item("Quit", self.quit),
        )

    def install_items(self):
        item = self.pystray.MenuItem
        installed = load_state().get("installed", {})
        rows = []
        for entry in self.catalog:
            current = installed.get(entry["icao"])
            if current and current.get("version") == entry["version"]:
                continue
            label = f"{entry['icao']} - {entry['name']} v{entry['version']}" + (" (update)" if current else "")
            rows.append(item(label, self._install_action(entry)))
        if rows:
            return rows
        text = self.catalog_error or ("All available packs are installed" if self.catalog else "Pack list not loaded")
        return [item(text[:90], None, enabled=False)]

    def uninstall_items(self):
        item = self.pystray.MenuItem
        installed = load_state().get("installed", {})
        rows = [
            item(f"{icao} - {rec.get('name', icao)} v{rec.get('version', '')}", self._uninstall_action(icao))
            for icao, rec in sorted(installed.items())
        ]
        return rows or [item("No packs installed", None, enabled=False)]

    def forget_choices(self) -> None:
        state = load_state()
        count = len(state.pop("choices", {}))
        state.pop("last", None)  # so the next update asks again
        save_state(state)
        self.set_status(f"Forgot {count} remembered choice(s)", notify=True)
        self.wake.set()

    def restore_items(self):
        """Airlines FCAC has saved original announcements for (or that have a 'main' folder of their own)."""
        item = self.pystray.MenuItem
        try:
            cfg = load_config()
            root = airlines_root(cfg)
            codes = {p.name for p in GENERIC_DIR.iterdir() if p.is_dir() and has_ogg(p)}
            if root.is_dir():
                codes |= {p.name.upper() for p in root.iterdir() if p.is_dir() and has_ogg(p / MAIN_FOLDER)}
        except (AppError, OSError):
            codes = set()
        rows = [item(code, self._restore_action(code)) for code in sorted(codes)]
        return rows or [item("Nothing to restore yet", None, enabled=False)]

    def _restore_action(self, icao: str):
        return lambda: self.run_task(f"Restoring {icao}", lambda: restore_generic(load_config(), icao))

    def open_airlines_folder(self) -> None:
        try:
            folder = airlines_root(load_config())
            folder.mkdir(parents=True, exist_ok=True)
            open_path(folder)
        except (AppError, OSError) as exc:
            self.set_status(str(exc), notify=True)

    def _install_action(self, entry: dict):
        return lambda: self.start_pack_task("install", entry["icao"], entry)

    def _uninstall_action(self, icao: str):
        return lambda: self.start_pack_task("uninstall", icao)

    def open_packs_folder(self) -> None:
        try:
            folder = airline_packs_dir(load_config())
            folder.mkdir(parents=True, exist_ok=True)
            open_path(folder)
        except AppError as exc:
            self.set_status(str(exc), notify=True)

    def msfs_watch(self) -> None:
        """Started by MSFS (Settings): close FCAC after the sim has been running and then closes."""
        seen, missing = False, 0
        while not self.stop.wait(10):
            try:
                if not (load_config().get("quit_with_msfs", True) and msfs_autostart_enabled()):
                    seen, missing = False, 0
                    continue
                if msfs_running():
                    seen, missing = True, 0
                elif seen:
                    missing += 1
                    if missing >= 2 and not (self.tasks or self.lock.locked()):
                        log.info("MSFS closed - quitting FCAC")
                        self.quit()
                        return
            except Exception:
                log.exception("MSFS watcher error")

    def sim_event(self, event: str, clock: str | None) -> None:
        """From the SimConnect watcher: "takeoff" (a new flight) or "landed" with the local time at the airport."""
        if event == "takeoff":
            log.info("Takeoff detected - new flight: landing time and extras start again")
            state = load_state()  # a new flight: the landing PA starts from the plan again and the extras roll again
            state.pop("live", None)
            state.pop("extra_used", None)
            save_state(state)
            return
        with self.lock:  # wait for any update in progress
            try:
                self.set_status(rebuild_landing(load_config(), clock), notify=True)
            except AppError as exc:
                self.set_status(str(exc), notify=True)
            except Exception as exc:
                log.exception("Landing PA error")
                self.set_status(f"Error: {exc}", notify=True)

    def toggle_auto(self) -> None:
        self.auto = not self.auto
        self.wake.set()

    def quit(self) -> None:
        self.stop.set()
        self.wake.set()
        self.icon.stop()

    # status -------------------------------------------------------------
    def set_status(self, text: str, notify: bool = False) -> None:
        self.status = text[:120]
        log.info(text)
        try:
            self.icon.title = f"{APP_NAME}: {self.status}"[:127]
            self.icon.update_menu()
            if notify:
                self.icon.notify(text, APP_NAME)
        except Exception:  # tray backends can be flaky; never let that kill the worker
            log.exception("Tray update failed")

    # work ---------------------------------------------------------------
    def do_update(self) -> str:
        return update(load_config(), force=True)[1]

    def run_task(self, label: str, fn, then_update: bool = False) -> None:
        """Run fn in a background thread, one task at a time, and report the result in the tray."""
        def work() -> None:
            if not self.lock.acquire(blocking=False):
                self.set_status("Busy - try again in a moment")
                return
            try:
                self.set_status(f"{label}...")
                message = fn()
                self.set_status(message, notify=True)
            except AppError as exc:
                self.set_status(str(exc), notify=True)
            except Exception as exc:
                log.exception("Unexpected error")
                self.set_status(f"Error: {exc}", notify=True)
            finally:
                self.lock.release()
            if then_update:
                self.poll_once(force=True)
            self.refresh_menu()

        threading.Thread(target=work, daemon=True).start()

    def refresh_menu(self) -> None:
        try:
            self.icon.update_menu()
        except Exception:
            log.exception("Menu refresh failed")

    def refresh_catalog(self, notify: bool = False) -> None:
        def work() -> None:
            try:
                self.catalog = load_catalog(load_config())
                self.catalog_error = ""
                if any([cache_remote_logo(e) for e in self.catalog]):
                    self.logo_version += 1
                if notify:
                    self.set_status(f"Pack list refreshed ({len(self.catalog)} available)", notify=True)
            except AppError as exc:
                self.catalog, self.catalog_error = [], str(exc)
                if notify:
                    self.set_status(str(exc), notify=True)
            self.refresh_menu()

        threading.Thread(target=work, daemon=True).start()

    def poll_once(self, force: bool = False) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            cfg = load_config()
            changed, message = update(cfg, force=force)
            notify = changed and bool(cfg.get("notifications", True))
            self.set_status(message, notify=notify)
        except AppError as exc:
            self.set_status(str(exc))
        except Exception as exc:
            log.exception("Unexpected error")
            self.set_status(f"Error: {exc}")
        finally:
            self.lock.release()

    def poll_loop(self) -> None:
        while not self.stop.is_set():
            interval = 60
            try:
                cfg = load_config()
                interval = max(15, int(cfg.get("poll_interval_seconds", 60)))
                if self.auto and cfg.get("auto_poll", True):
                    self.poll_once()
            except AppError as exc:
                self.set_status(str(exc))
            except Exception:
                log.exception("Poll loop error")
            self.wake.wait(interval)
            self.wake.clear()

    def run(self) -> None:
        try:
            airlines_root(load_config()).mkdir(parents=True, exist_ok=True)
        except (AppError, OSError):
            pass  # reported later when the first update runs
        self.refresh_catalog()
        try:
            cfg = load_config()
            needs_name = not (str(cfg.get("simbrief_username", "")).strip() or str(cfg.get("simbrief_userid", "")).strip())
        except AppError:
            needs_name = True  # first run: config.json was just created
        if needs_name or not load_state().get("welcomed"):
            self.open_window()  # first run: explain what FCAC changes, ask for the SimBrief username
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.msfs_watch, daemon=True).start()
        try:
            import fcac_sim

            fcac_sim.SimWatcher(self.sim_event, msfs_running, self.stop,
                                float(load_config().get("landing_speed_knots", 60))).start()
        except Exception:
            log.exception("Could not start the landing watcher")
        self.icon.run()


# --------------------------------------------------------------------------- entry point
def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--community", action="store_true", help="show the detected Community folder")
    parser.add_argument("--add-airline", nargs="+", metavar="ICAO", help="create Airlines\\<ICAO>\\dep|dest|fuel folders")
    parser.add_argument("--list-packs", action="store_true", help="list packs in the catalog")
    parser.add_argument("--install", metavar="ICAO", help="download and install an airline pack")
    parser.add_argument("--uninstall", metavar="ICAO", help="uninstall an airline pack")
    parser.add_argument("--restore", metavar="ICAO", help="put an airline's original generic announcements back")
    parser.add_argument("--once", action="store_true", help="update once and exit (no tray icon)")
    parser.add_argument("--airline", help="with --once: airline ICAO instead of the SimBrief plan's")
    parser.add_argument("--variant", help="with --once: which recording to use when a destination has several (e.g. T5, or Standard)")
    parser.add_argument("--dest", help="with --once: destination ICAO instead of asking SimBrief")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )

    cli = (args.community or args.add_airline or args.list_packs or args.install or args.uninstall
           or args.restore or args.once)
    if not cli:
        TrayApp().run()
        return 0
    try:
        cfg = load_config()
        if args.community:
            print(find_community_dir(cfg))
        for code in args.add_airline or []:
            folder, created = ensure_airline_folders(cfg, code)
            print(f"{'Created' if created else 'Already there'}: {folder}")
        if args.list_packs:
            installed = load_state().get("installed", {})
            for p in load_catalog(cfg):
                mark = "installed" if p["icao"] in installed else ""
                print(f"{p['icao']}  {p['name']}  v{p['version']}  {mark}")
        if args.install:
            wanted = args.install.upper()
            entry = next((p for p in load_catalog(cfg) if p["icao"] == wanted), None)
            if not entry:
                raise AppError(f"{wanted} is not in the catalog.")
            print(install_pack(cfg, entry))
        if args.uninstall:
            print(uninstall_pack(cfg, args.uninstall))
        if args.restore:
            print(restore_generic(cfg, args.restore))
        if args.once:
            print(update(cfg, force=True, airline=args.airline, dest=args.dest, variant=args.variant)[1])
        return 0
    except AppError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
