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

APP_NAME = "FCAC - Fenix Cabin Announcements Configurator"
BASE_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
)
CONFIG_PATH = Path(os.environ.get("FCAC_CONFIG", BASE_DIR / "config.json"))


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
LOG_PATH = DATA_DIR / "announcer.log"
for _d in (DATA_DIR, GENERIC_DIR, BACKUPS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SIMBRIEF_URL = "https://www.simbrief.com/api/xml.fetcher.php"
STORE_PACKAGE = "Microsoft.Limitless_8wekyb3d8bbwe"   # MSFS 2024, Microsoft Store / Xbox app
STEAM_APPID = "2537590"                               # MSFS 2024 on Steam
MAIN_FOLDER = "main"                                  # Airlines\<ICAO>\main: the airline's general PAs (SafetyBriefing...)
ZIP_FOLDERS = (MAIN_FOLDER, "dep", "dest", "fuel")    # the only top-level folders read from a pack zip
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024

DEFAULT_CONFIG = {
    "simbrief_username": "",
    "simbrief_userid": "",
    "catalog_url": "https://raw.githubusercontent.com/sjames1066/fcac/main/catalog.json",
    "community_dir": "",              # leave empty to auto-detect
    "airlines_dir": "",               # your recordings library; empty = an 'Airlines' folder next to the app
    "announcements_folder": "fnx-airline-packs",
    "airline_override": "",           # force an airline ICAO instead of using the SimBrief plan
    "auto_poll": True,
    "poll_interval_seconds": 60,
    "notifications": True,
    # Which pack files each folder replaces. Variants (e.g. BoardingWelcome[Morning].ogg) are found in the pack.
    "mappings": [
        {"folder": "dep", "base": "BoardingWelcome", "exclude_tags": ["Refueling"], "default_name": "BoardingWelcome"},
        {"folder": "dest", "base": "AfterLanding", "default_name": "AfterLanding"},
        {"folder": "fuel", "base": "BoardingWelcome", "require_tags": ["Refueling"],
         "default_name": "BoardingWelcome[Refueling]"},
    ],
}

log = logging.getLogger("fcac")


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


def _helper_command(*args: str) -> list[str]:
    """Command that runs this program again with `args` (works as a script and as a packaged .exe)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def ask_text(title: str, prompt: str, default: str = "") -> str | None:
    """Ask for a line of text in a small window (own process, so Tk never fights the tray icon). None = cancelled."""
    try:
        out = subprocess.run(_helper_command("--ask", title, prompt, default), capture_output=True, text=True,
                             timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return json.loads(out.strip().splitlines()[-1]).get("text")
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        log.exception("Text dialog failed")
        return None


def run_ask_dialog(title: str, prompt: str, default: str) -> int:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    result: dict = {"text": None}
    value = tk.StringVar(value=default)
    frame = ttk.Frame(root, padding=16)
    frame.pack()
    ttk.Label(frame, text=title, font=("Segoe UI", 12, "bold")).pack(anchor="w")
    ttk.Label(frame, text=prompt, wraplength=360).pack(anchor="w", pady=(4, 10))
    entry = ttk.Entry(frame, textvariable=value, width=40)
    entry.pack(anchor="w")
    entry.focus_set()

    def done(_event=None) -> None:
        result["text"] = value.get().strip()
        root.destroy()

    root.bind("<Return>", done)
    ttk.Button(frame, text="Save", command=done).pack(anchor="e", pady=(12, 0))
    root.attributes("-topmost", True)
    root.mainloop()
    print(json.dumps(result))
    return 0


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
    }


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
                if info.is_dir() or not parts or parts[0] not in ZIP_FOLDERS:
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
    """Create Airlines\\<ICAO>\\main, dep, dest, fuel if they are missing. Returns (folder, newly_created)."""
    icao = check_airline_code(icao)
    base = airline_folder(cfg, icao)
    created = not base.is_dir()
    try:
        for folder in (MAIN_FOLDER, *(m["folder"] for m in cfg["mappings"])):
            (base / folder).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppError(f"Could not create {base}: {exc}") from exc
    return base, created


def has_ogg(folder: Path) -> bool:
    return folder.is_dir() and any(p.suffix.lower() == ".ogg" for p in folder.iterdir() if p.is_file())


def install_pack(cfg: dict, entry: dict, progress=None) -> str:
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

    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        zip_path = Path(tmp) / f"{icao}.zip"
        download_pack(entry, zip_path, progress)
        staging = Path(tmp) / "extract"
        extract_pack(zip_path, staging)

        try:
            # 1. Library: add the pack's files. Files you put there yourself are never overwritten.
            added, skipped = [], 0
            for src in sorted(p for p in staging.rglob("*") if p.is_file()):
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
            shutil.copytree(generic if has_ogg(generic) else staging / MAIN_FOLDER, fenix_dir)
        except OSError as exc:
            raise AppError(f"Could not install {icao} (is MSFS using the files?): {exc}") from exc

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
        if path.stem.split("[", 1)[0] != mapping["base"]:
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


def choose_gui(title: str, prompt: str, options: list[str]) -> tuple[str, bool]:
    """Ask which option to use. Runs the dialog in a separate process so Tk never fights the tray icon's loop.
    Returns (chosen option, remember). Closing the window picks the first option."""
    cmd = _helper_command("--pick", title, prompt, *options)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=flags).stdout
        data = json.loads(out.strip().splitlines()[-1])
        if data["choice"] in options:
            return data["choice"], bool(data.get("remember"))
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
        log.exception("Choice dialog failed")
    return options[0], False


def run_pick_dialog(title: str, prompt: str, options: list[str]) -> int:
    """Small Tk window: radio buttons + 'remember'. Prints {"choice":..., "remember":...} as JSON."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    result = {"choice": options[0], "remember": False}
    picked, remember = tk.StringVar(value=options[0]), tk.BooleanVar(value=False)
    frame = ttk.Frame(root, padding=16)
    frame.pack()
    ttk.Label(frame, text=title, font=("Segoe UI", 12, "bold")).pack(anchor="w")
    ttk.Label(frame, text=prompt, wraplength=340).pack(anchor="w", pady=(4, 10))
    for option in options:
        ttk.Radiobutton(frame, text=option, value=option, variable=picked).pack(anchor="w", pady=1)
    ttk.Checkbutton(frame, text="Remember my choice for this airport", variable=remember).pack(anchor="w", pady=(10, 8))

    def done() -> None:
        result.update(choice=picked.get(), remember=remember.get())
        root.destroy()

    ttk.Button(frame, text="Use this one", command=done).pack(anchor="e")
    root.protocol("WM_DELETE_WINDOW", root.destroy)  # closing = the first option, not remembered
    root.attributes("-topmost", True)
    root.mainloop()
    print(json.dumps(result))
    return 0


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


def apply_destination(cfg: dict, airline: str, dest_icao: str, choose=None, variant: str | None = None) -> str:
    """Apply a destination. If several recordings exist for it (terminals etc.) `choose` is asked which to use
    (`variant` skips the question)."""
    airline = check_airline_code(airline)
    lib_dir, created = ensure_airline_folders(cfg, airline)
    fenix_dir = airline_packs_dir(cfg) / airline
    pristine = generic_source(cfg, airline, fenix_dir)
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
            if path.is_file() and path.suffix.lower() == ".ogg":
                shutil.copy2(path, fenix_dir / path.name)

    # Your general PAs in Airlines\<ICAO>\main are the source of truth for everything FCAC doesn't swap per
    # destination (SafetyBriefing, ArmDoors...): copy any that are new or changed into the Fenix pack.
    synced = 0
    if pristine == lib_dir / MAIN_FOLDER:
        managed = {name for m in cfg["mappings"] for name in target_names(pristine, m)}
        for path in sorted(pristine.iterdir()):
            if path.is_file() and path.suffix.lower() == ".ogg" and path.name not in managed:
                dst = fenix_dir / path.name
                if not dst.is_file() or not filecmp.cmp(path, dst, shallow=False):
                    install_file(path, dst)
                    synced += 1

    all_clips = {m["folder"]: find_clips(lib_dir, m["folder"], airline, dest_icao) for m in cfg["mappings"]}
    options = variant_options(all_clips)
    chosen = ""
    if variant is not None:
        chosen = variant.upper().strip()
        if chosen == STANDARD.upper():
            chosen = ""
    elif len(options) == 1:
        chosen = options[0]
    elif len(options) > 1:
        chosen = pick_variant(airline, dest_icao, options, choose or choose_gui)

    summary, custom = [], 0
    for mapping in cfg["mappings"]:
        clips = all_clips[mapping["folder"]]
        clip = clips.get(chosen) or clips.get("")  # the chosen variant, else the plain file, else generic
        for name in target_names(pristine, mapping):
            dst, generic = fenix_dir / name, (pristine / name if pristine else None)
            if clip:
                install_file(clip, dst)
            elif generic and generic.is_file():
                install_file(generic, dst)  # no recording for this airport: back to the pack's generic file
            else:
                dst.unlink(missing_ok=True)
        custom += bool(clip)
        summary.append(f"{mapping['folder']}: {'custom' if clip else 'generic'}")

    shown = f"{dest_icao} {variant_name(chosen)}" if len(options) > 1 or chosen else dest_icao
    message = f"{airline} to {shown} ({', '.join(summary)})"
    if synced:
        message += f" - updated {synced} general PA file(s) from main"
    if created:
        message += f" - created {lib_dir}, add your recordings there"
    elif not custom:
        message += f" - no recordings for {airline}_{dest_icao} yet"
    return message


def restore_generic(cfg: dict, icao: str) -> str:
    """Put an airline's original generic announcements back in the Fenix folder."""
    icao = check_airline_code(icao)
    fenix_dir = airline_packs_dir(cfg) / icao
    source = generic_source(cfg, icao, fenix_dir)
    if not source:
        raise AppError(f"No saved original {icao} announcements yet (FCAC saves them the first time it changes a pack).")
    fenix_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        if path.is_file() and path.suffix.lower() == ".ogg":
            install_file(path, fenix_dir / path.name)
    state = load_state()
    state.pop("last", None)
    save_state(state)
    return f"Restored the original {icao} announcements"


def update(cfg: dict, force: bool = False, airline: str | None = None, dest: str | None = None,
           variant: str | None = None) -> tuple[bool, str]:
    """Look up the flight (or use the given one) and apply it. Returns (changed, message)."""
    flight = {"icao": dest.upper(), "airline": ""} if dest else fetch_flight(cfg)
    code = (airline or cfg.get("airline_override") or flight["airline"] or "").strip().upper()
    if not code:
        raise AppError("The SimBrief plan has no airline ICAO - set airline_override in config.json.")
    code = check_airline_code(code)
    if not re.fullmatch(r"[A-Z0-9]{3,4}", flight["icao"]):
        raise AppError(f"'{flight['icao']}' is not a valid airport code.")
    key = f"{code}_{flight['icao']}"
    state = load_state()
    if not force and state.get("last") == key:
        return False, f"Unchanged: {code} to {flight['icao']}"
    message = apply_destination(cfg, code, flight["icao"], variant=variant)
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


class TrayApp:
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
        self.icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME, self.build_menu())

    # menu ---------------------------------------------------------------
    def build_menu(self):
        item, menu = self.pystray.MenuItem, self.pystray.Menu
        return menu(
            item(lambda _: self.status, None, enabled=False),
            menu.SEPARATOR,
            item("Update from SimBrief now", lambda: self.run_task("Updating", self.do_update)),
            item("Auto-update", self.toggle_auto, checked=lambda _: self.auto),
            menu.SEPARATOR,
            item("Install airline pack", menu(self.install_items)),
            item("Uninstall airline pack", menu(self.uninstall_items)),
            item("Refresh pack list", lambda: self.refresh_catalog(notify=True)),
            item("Restore original announcements", menu(self.restore_items)),
            item("Set SimBrief username...", lambda: threading.Thread(target=self.ask_simbrief, daemon=True).start()),
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

    def ask_simbrief(self) -> None:
        """Ask for the SimBrief username and save it to config.json."""
        try:
            cfg = load_config()
        except AppError as exc:
            cfg = dict(DEFAULT_CONFIG)
            log.info("%s", exc)
        name = ask_text("SimBrief username", "Your SimBrief username (the name you log in with, not the numeric ID). "
                        "FCAC reads your latest flight plan from it.", str(cfg.get("simbrief_username", "")))
        if not name:
            self.set_status("SimBrief username not set - right-click the tray icon -> Set SimBrief username")
            return
        try:
            set_config_value("simbrief_username", name)
            set_config_value("simbrief_userid", "")  # an old numeric ID would otherwise win over the new name
        except (AppError, OSError) as exc:
            self.set_status(f"Could not save config.json: {exc}")
            return
        self.set_status(f"SimBrief username set to {name}", notify=True)
        self.wake.set()

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
        return lambda: self.run_task(f"Installing {entry['icao']}", lambda: self.do_install(entry), then_update=True)

    def _uninstall_action(self, icao: str):
        return lambda: self.run_task(f"Uninstalling {icao}", lambda: uninstall_pack(load_config(), icao))

    def open_packs_folder(self) -> None:
        try:
            folder = airline_packs_dir(load_config())
            folder.mkdir(parents=True, exist_ok=True)
            open_path(folder)
        except AppError as exc:
            self.set_status(str(exc), notify=True)

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
    def do_install(self, entry: dict) -> str:
        last = {"pct": -10}

        def progress(done: int, total: int) -> None:
            pct = int(done * 100 / total)
            if pct - last["pct"] >= 5:
                last["pct"] = pct
                self.set_status(f"Downloading {entry['icao']}... {pct}%")

        return install_pack(load_config(), entry, progress)

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
        if needs_name:
            threading.Thread(target=self.ask_simbrief, daemon=True).start()
        threading.Thread(target=self.poll_loop, daemon=True).start()
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
    parser.add_argument("--pick", nargs="+", help=argparse.SUPPRESS)  # internal: choice dialog
    parser.add_argument("--ask", nargs=3, help=argparse.SUPPRESS)     # internal: text entry dialog
    parser.add_argument("--dest", help="with --once: destination ICAO instead of asking SimBrief")
    args = parser.parse_args()
    if args.pick:
        return run_pick_dialog(args.pick[0], args.pick[1], args.pick[2:])
    if args.ask:
        return run_ask_dialog(*args.ask)

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
