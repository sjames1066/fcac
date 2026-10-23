"""FCAC Studio: record or upload the pieces of an airline pack, give them the cabin-PA sound, set them to the
loudness of the original Fenix announcements and export a ready-to-share pack zip.

No GUI code here; fcac.py calls these functions and fcac_ui.py shows them. A project lives in
<studio>/<ICAO>/<voice>/ with raw/ (what was recorded or uploaded, untouched), pack/ (the processed pack, laid out like
Airlines\\<ICAO>) and project.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import zipfile
from pathlib import Path

import fcac_audio

DEFAULT_TARGET = -20.0     # LUFS, used when there are no original announcements to measure
MAX_UPLOAD = 60 * 1024 * 1024
AUDIO_EXT = {".wav", ".ogg", ".mp3", ".m4a", ".flac", ".webm", ".opus", ".aac"}
AIRPORT = re.compile(r"^[A-Z0-9]{4}(_[A-Z0-9]{1,8})?$")
MUSIC_NAME = re.compile(r"^\w[\w .&'()-]{0,59}$")
EXTRA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")

GENERAL = [  # (file name, label, what to say). Names are Fenix's announcement types; every one is optional except the modular pair.
    ("Modular_Welcome", "Welcome aboard - the rest", "Everything in the welcome PA after the destination: seatbelts, safety, enjoy your flight..."),
    ("Modular_Landing", "Landing - the rest", "Everything in the landing PA after the local time: cabin crew seats, seatbelt sign, belongings, thank you..."),
    ("BoardingWelcome[Morning]", "BoardingWelcome - morning (generic)", "Generic welcome aboard, good morning. Played every 2-6 min during boarding."),
    ("BoardingWelcome[Afternoon]", "BoardingWelcome - afternoon (generic)", "Generic welcome aboard, good afternoon."),
    ("BoardingWelcome[Evening]", "BoardingWelcome - evening (generic)", "Generic welcome aboard, good evening."),
    ("BoardingWelcome[Night]", "BoardingWelcome - night (generic)", "Generic welcome aboard for boardings between midnight and 6 am."),
    ("BoardingWelcome[Refueling]", "BoardingWelcome - refuelling (generic)", "Generic welcome aboard while the aircraft is being refuelled."),
    ("BoardingComplete", "BoardingComplete", "The purser announces that boarding has completed."),
    ("ArmDoors", "ArmDoors", "Cabin crew, arm doors and cross-check."),
    ("DisarmDoors", "DisarmDoors", "Cabin crew, disarm doors and cross-check."),
    ("PreSafetyBriefing", "PreSafetyBriefing", "The opening of the safety briefing, before the demonstration."),
    ("SafetyBriefing", "SafetyBriefing", "The safety demonstration."),
    ("CrewTakeSeats", "CrewTakeSeats", "Cabin crew, take your seats for takeoff / landing."),
    ("CabinDim", "CabinDim", "We will dim the cabin lights for takeoff / landing."),
    ("CabinSecured", "CabinSecured", "Interphone to the cockpit: cabin secure for takeoff / landing."),
    ("AfterTakeoff", "AfterTakeoff", "PA after takeoff, once past acceleration altitude."),
    ("SeatbeltSignOn", "SeatbeltSignOn", "The seatbelt signs have been switched on."),
    ("SeatbeltSignOff", "SeatbeltSignOff", "The seatbelt signs have been switched off."),
    ("CabinService", "CabinService", "Cabin service is about to begin."),
    ("DescentStarted", "DescentStarted", "We have started our descent."),
    ("AfterLanding", "AfterLanding (generic)", "Generic 'welcome to our destination', used where an airport has no recording."),
    ("DeboardingStarted", "DeboardingStarted", "Passengers may now disembark."),
    ("DeboardingComplete", "DeboardingComplete", "Deboarding has completed."),
    ("Decompression", "Decompression", "Oxygen masks have deployed - emergency descent instructions."),
]
GREETINGS = [("Welcome_Morning", "Good morning", "Good morning, ladies and gentlemen"),
             ("Welcome_Afternoon", "Good afternoon", "Good afternoon, ladies and gentlemen"),
             ("Welcome_Evening", "Good evening", "Good evening, ladies and gentlemen"),
             ("Welcome_Night", "Good evening (night, optional)", "Used for boardings between midnight and 6 am; without it 'Good evening' is used."),
             ("LocalTime", "Local time lead-in", "...where the local time is")]
HOURS = ["twelve", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven"]
MINUTES = {0: "o'clock", 5: "oh five", 10: "ten", 15: "fifteen", 20: "twenty", 25: "twenty-five", 30: "thirty",
           35: "thirty-five", 40: "forty", 45: "forty-five", 50: "fifty", 55: "fifty-five"}


class StudioError(Exception):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_")[:40] or "Voice"


def project_dir(root: Path, icao: str, voice: str) -> Path:
    return root / icao / slug(voice)


def list_projects(root: Path) -> list[dict]:
    found = []
    for file in sorted(root.glob("*/*/project.json")) if root.is_dir() else []:
        try:
            info = json.loads(file.read_text(encoding="utf-8"))
            found.append({"icao": info["icao"], "voice": info["voice"], "airline": info.get("airline", "")})
        except (OSError, ValueError, KeyError):
            continue
    return found


def load(root: Path, icao: str, voice: str) -> dict:
    path = project_dir(root, icao, voice) / "project.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise StudioError("That project does not exist.")


def save(root: Path, project: dict) -> None:
    folder = project_dir(root, project["icao"], project["voice"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.json").write_text(json.dumps(project, indent=2), encoding="utf-8")


def create(root: Path, icao: str, airline: str, voice: str) -> dict:
    icao, voice, airline = icao.strip().upper(), voice.strip(), airline.strip()
    if not re.fullmatch(r"[A-Z0-9]{3}", icao):
        raise StudioError("The airline code must be 3 letters or digits, like BAW.")
    if not voice or not airline:
        raise StudioError("Give the airline name and a name for this voice.")
    try:
        return load(root, icao, voice)
    except StudioError:
        pass
    project = {"icao": icao, "airline": airline[:80], "voice": voice[:40], "artist": "", "credit_url": "", "donate_url": "",
               "accent": "", "description": "", "version": "1.0.0", "airports": [], "extras": [], "music": [],
               "effect": True, "slots": {}}
    save(root, project)
    return project


# --------------------------------------------------------------------------- the checklist
def slot_list(project: dict) -> list[dict]:
    icao = project["icao"]
    out = []

    def add(group, folder, stem, label, script, required=False, out_name=None):
        out.append({"id": f"{folder}/{stem}", "group": group, "folder": folder, "stem": stem, "label": label,
                    "script": script, "required": required, "file": f"{out_name or stem}.ogg"})

    for stem, label, script in GENERAL:
        add("General announcements", "main", stem, label, script, required=stem.startswith("Modular_"))
    for stem, label, script in GREETINGS:
        add("Greetings and clock", "time", stem, label, script, required=stem != "Welcome_Night")
    for i, word in enumerate(HOURS):
        n = i or 12
        add("Greetings and clock", "time", f"Hour_{n:02d}", f"Hour {n}", word.capitalize(), required=True)
    for minute, word in MINUTES.items():
        add("Greetings and clock", "time", f"Min_{minute:02d}", f"Minute :{minute:02d}", word.capitalize(), required=True)
    add("Greetings and clock", "time", "AM", "AM (optional)", "A.M. - said after the minutes, e.g. 'four thirty-five A.M.'")
    add("Greetings and clock", "time", "PM", "PM (optional)", "P.M. - said after the minutes, e.g. 'four thirty-five P.M.'")
    for code in project.get("airports", []):
        add("Airports", "dep", f"{icao}_{code}", f"Welcome aboard - {code}",
            "Just the airport name, e.g. 'London Heathrow'")
        add("Airports", "dest", f"{icao}_{code}", f"Landing - {code}",
            "'Ladies and gentlemen, welcome to ' + the airport name")
    add("Refuelling", "fuel", "Fuel_Tag", "Refuelling tag-on",
        "Added to the welcome while the aircraft is being refuelled, e.g. 'We are currently refuelling, so please keep your seatbelt unfastened...'")
    for name in project.get("music", []):
        add("Boarding music", "main", f"BoardingMusic~{name}", f"Track: {name}",
            "Upload a track you have the rights to use. It is shown in the cabin crew panel under this name; no PA sound is applied.")
        out[-1]["music"] = True
    for name in project.get("extras", []):
        add("Bolt-ons", "extra", name, f"Bolt-on: {name}", "A short announcement that is sometimes added to the welcome.")
    return out


def hour_order(slots: list[dict]) -> list[dict]:
    hours = sorted((s for s in slots if s["stem"].startswith("Hour_")), key=lambda s: int(s["stem"][5:]))
    it = iter(hours)
    return [next(it) if s["stem"].startswith("Hour_") else s for s in slots]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def parse_script(project: dict, text: str) -> tuple[dict, int]:
    """Read an uploaded script into {slot id: text}. Accepts JSON ({"time/Hour_04": "four"}) or text lines like
    "Hour 4: Four" / "Hour_04 - Four" / "time/Hour_04 = Four", matching a slot's id, file name or label.
    Returns (scripts, number of lines that matched nothing)."""
    slots = slot_list(project)
    lookup = {}
    for slot in slots:
        for name in (slot["id"], slot["stem"], slot["label"]):
            lookup.setdefault(_key(name), slot["id"])
    found: dict[str, str] = {}
    pairs = []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            pairs = [(str(k), str(v)) for k, v in data.items()]
    except ValueError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in re.finditer(r"\s*(?::|=|\t| - | – )\s*", line):  # the first split whose left side names a clip
                if _key(line[:sep.start()]) in lookup and line[sep.end():].strip():
                    pairs.append((line[:sep.start()], line[sep.end():].strip()))
                    break
            else:
                pairs.append(("", line))
    skipped = 0
    for name, words in pairs:
        slot_id = lookup.get(_key(name))
        if slot_id:
            found[slot_id] = words[:1000]
        else:
            skipped += 1
    return found, skipped


def find_slot(project: dict, slot_id: str) -> dict:
    for slot in slot_list(project):
        if slot["id"] == slot_id:
            return slot
    raise StudioError("Unknown recording slot.")


def status(root: Path, project: dict) -> list[dict]:
    folder = project_dir(root, project["icao"], project["voice"])
    done = project.get("slots", {})
    rows = []
    for slot in hour_order(slot_list(project)):
        info = done.get(slot["id"])
        have = info is not None and (folder / "pack" / slot["folder"] / slot["file"]).is_file()
        slot = {**slot, "script": project.get("scripts", {}).get(slot["id"], slot["script"]),
                "custom": slot["id"] in project.get("scripts", {})}
        rows.append({**slot, "music": bool(slot.get("music")), "done": have, "seconds": info.get("seconds") if have else None,
                     "lufs": info.get("lufs_out") if have else None})
    return rows


# --------------------------------------------------------------------------- loudness reference
def reference_loudness(files: list[Path]) -> tuple[float, int]:
    """The loudness of the original Fenix announcements: the median of their integrated loudness -> (LUFS, how many
    were measured). Falls back to DEFAULT_TARGET."""
    values = []
    for path in files[:12]:
        try:
            value = fcac_audio.file_loudness(path)
        except Exception:
            continue
        if value is not None and value > -50:
            values.append(value)
    if not values:
        return DEFAULT_TARGET, 0
    return round(statistics.median(values), 1), len(values)


# --------------------------------------------------------------------------- recording / processing
def add_airport(root: Path, project: dict, code: str) -> None:
    code = code.strip().upper().replace(" ", "_")
    if not AIRPORT.match(code):
        raise StudioError("Use the airport's 4-letter ICAO code, like EGLL. A terminal variant can follow: EGLL_T5.")
    if code not in project["airports"]:
        project["airports"].append(code)
        project["airports"].sort()
    save(root, project)


def remove_airport(root: Path, project: dict, code: str) -> None:
    project["airports"] = [c for c in project["airports"] if c != code]
    save(root, project)


def add_music(root: Path, project: dict, name: str) -> None:
    name = name.strip()
    if not MUSIC_NAME.match(name):
        raise StudioError("Name the track with letters, digits, spaces or . & ' ( ) - (e.g. Departure lounge).")
    if name.lower() not in [n.lower() for n in project.setdefault("music", [])]:
        project["music"].append(name)
    save(root, project)


def remove_music(root: Path, project: dict, name: str) -> None:
    project["music"] = [n for n in project.get("music", []) if n != name]
    save(root, project)


def add_extra(root: Path, project: dict, name: str) -> None:
    name = name.strip()
    if not EXTRA_NAME.match(name):
        raise StudioError("Name the bolt-on with letters, digits, spaces, - or _ (e.g. Nut allergy).")
    name = name.replace(" ", "_")
    if name not in project["extras"]:
        project["extras"].append(name)
    save(root, project)


def remove_extra(root: Path, project: dict, name: str) -> None:
    project["extras"] = [n for n in project["extras"] if n != name]
    save(root, project)


def store_raw(root: Path, project: dict, slot_id: str, body: bytes, filename: str, target: float) -> dict:
    """Keep the uploaded or recorded take and process it. Returns the slot's measurements."""
    slot = find_slot(project, slot_id)
    ext = Path(filename).suffix.lower()
    if ext not in AUDIO_EXT:
        raise StudioError("Use a wav, mp3, ogg, m4a, flac or webm file.")
    if not body or len(body) > MAX_UPLOAD:
        raise StudioError("That file is empty or too large (60 MB limit).")
    folder = project_dir(root, project["icao"], project["voice"])
    raw = folder / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for old in raw.glob(f"{slot['folder']}__{slot['stem']}.*"):
        old.unlink()
    path = raw / f"{slot['folder']}__{slot['stem']}{ext}"
    path.write_bytes(body)
    return process_slot(root, project, slot, target)


def process_slot(root: Path, project: dict, slot: dict, target: float) -> dict:
    folder = project_dir(root, project["icao"], project["voice"])
    raws = list((folder / "raw").glob(f"{slot['folder']}__{slot['stem']}.*"))
    if not raws:
        raise StudioError("Nothing recorded for that yet.")
    try:
        if slot.get("music"):
            result = fcac_audio.convert_music(raws[0], folder / "pack" / slot["folder"] / slot["file"])
        else:
            result = fcac_audio.process_clip(raws[0], folder / "pack" / slot["folder"] / slot["file"], target,
                                             bool(project.get("effect", True)))
    except fcac_audio.AudioError as exc:
        raise StudioError(str(exc))
    result["target"] = target
    project.setdefault("slots", {})[slot["id"]] = result
    save(root, project)
    return result


def reprocess_all(root: Path, project: dict, target: float) -> int:
    count = 0
    for slot in slot_list(project):
        if slot["id"] in project.get("slots", {}) and not slot.get("music"):
            try:
                process_slot(root, project, slot, target)
                count += 1
            except StudioError:
                pass
    return count


def delete_slot(root: Path, project: dict, slot_id: str) -> None:
    slot = find_slot(project, slot_id)
    folder = project_dir(root, project["icao"], project["voice"])
    for path in (folder / "raw").glob(f"{slot['folder']}__{slot['stem']}.*"):
        path.unlink()
    (folder / "pack" / slot["folder"] / slot["file"]).unlink(missing_ok=True)
    project.get("slots", {}).pop(slot_id, None)
    save(root, project)


def processed_file(root: Path, project: dict, slot_id: str) -> Path | None:
    slot = find_slot(project, slot_id)
    path = project_dir(root, project["icao"], project["voice"]) / "pack" / slot["folder"] / slot["file"]
    return path if path.is_file() else None


# --------------------------------------------------------------------------- export
def voice_json(project: dict) -> dict:
    data = {"name": project["voice"], "artist": project.get("artist", ""), "credit_url": project.get("credit_url", ""),
            "donate_url": project.get("donate_url", ""), "accent": project.get("accent", ""),
            "description": project.get("description", "")}
    return {k: v for k, v in data.items() if v}


def missing(root: Path, project: dict) -> list[str]:
    return [s["label"] for s in status(root, project) if s["required"] and not s["done"]]


def export(root: Path, project: dict, out_dir: Path) -> dict:
    """Write <ICAO>_<Voice>.zip (laid out like Airlines\\<ICAO>, ready for the library or for make_pack/catalog) and
    a catalog entry. Raises StudioError if required recordings are missing."""
    todo = missing(root, project)
    if todo:
        raise StudioError("Still to record: " + ", ".join(todo[:6]) + (f" and {len(todo) - 6} more" if len(todo) > 6 else ""))
    if not any(s["folder"] in ("dep", "dest") and s["done"] for s in status(root, project)):
        raise StudioError("Add at least one airport and record its welcome and landing clips.")
    folder = project_dir(root, project["icao"], project["voice"]) / "pack"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{project['icao']}_{slug(project['voice'])}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*.ogg")):
            archive.write(path, path.relative_to(folder).as_posix())
        archive.writestr("voice.json", json.dumps(voice_json(project), indent=2))
    entry = {"icao": project["icao"], "name": project["airline"], "version": project.get("version", "1.0.0"),
             "url": zip_path.name, "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
             "size": zip_path.stat().st_size,
             "voices": [{k: v for k, v in voice_json(project).items() if k in ("name", "artist", "accent", "description")}]}
    entry_path = zip_path.with_suffix(".catalog.json")
    entry_path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    return {"zip": str(zip_path), "entry": str(entry_path), "size": entry["size"]}


def install_to_library(root: Path, project: dict, airline_dir: Path) -> Path:
    """Copy the project into Airlines\\<ICAO>\\voices\\<Voice> so FCAC can use it straight away."""
    dest = airline_dir / "voices" / slug(project["voice"])
    src = project_dir(root, project["icao"], project["voice"]) / "pack"
    if not src.is_dir() or not any(src.rglob("*.ogg")):
        raise StudioError("Record something first.")
    shutil.copytree(src, dest, dirs_exist_ok=True)
    music = sorted((src / "main").glob("BoardingMusic*.ogg"))
    if music:  # boarding music is the airline's, not a voice's: it goes where FCAC copies the general PAs from
        (airline_dir / "main").mkdir(parents=True, exist_ok=True)
        for path in music:
            shutil.copyfile(path, airline_dir / "main" / path.name)
    (dest / "voice.json").write_text(json.dumps(voice_json(project), indent=2), encoding="utf-8")
    return dest
