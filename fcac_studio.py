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
import sys
import zipfile
from pathlib import Path

import fcac_audio

DEFAULT_TARGET = -18.9     # LUFS every clip is set to (tested in the sim against the original Fenix announcements)
PACKS_REPO = "sjames1066/fcac-packs"   # where exported packs are published as releases (tag = the zip name without .zip)
EFFECT_VERSION = 3         # bump when the PA sound changes: projects are re-processed once when opened
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
    ("CrewTakeSeats[PreFlight]", "CrewTakeSeats - takeoff", "Cabin crew, take your seats for takeoff. (Tagged [PreFlight]: used before takeoff.)"),
    ("CrewTakeSeats[Descent][Approach]", "CrewTakeSeats - landing", "Cabin crew, take your seats for landing. (Tagged [Descent][Approach]: used before landing.)"),
    ("CabinDim[PreFlight]", "CabinDim - takeoff", "We will dim the cabin lights for takeoff. (Tagged [PreFlight]: used before takeoff.)"),
    ("CabinDim[Descent][Approach]", "CabinDim - landing", "We will dim the cabin lights for landing. (Tagged [Descent][Approach]: used before landing.)"),
    ("CabinSecured[PreFlight]", "CabinSecured - takeoff", "Interphone to the cockpit: cabin secure for takeoff. (Tagged [PreFlight].)"),
    ("CabinSecured[Descent][Approach]", "CabinSecured - landing", "Interphone to the cockpit: cabin secure for landing. (Tagged [Descent][Approach].)"),
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
GREETINGS = [("Welcome_Morning", "Good morning", "Ladies and gentlemen, welcome on board this [airline] flight to..."),
             ("Welcome_Afternoon", "Good afternoon", "Ladies and gentlemen, welcome on board this [airline] flight to..."),
             ("Welcome_Evening", "Good evening", "Ladies and gentlemen, welcome on board this [airline] flight to..."),
             ("Welcome_Night", "Good evening (night, optional)", "Ladies and gentlemen, welcome on board this [airline] flight to... (for boardings between midnight and 6 am; without it the evening greeting is used)"),
             ("WelcomeTo", "Landing lead-in", "Ladies and gentlemen, welcome to... (the airport name is added next)"),
             ("LocalTime", "Local time lead-in", "...where the local time is")]
HOURS = ["twelve", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven"]
MINUTES = {0: "o'clock", 5: "oh five", 10: "ten", 15: "fifteen", 20: "twenty", 25: "twenty-five", 30: "thirty",
           35: "thirty-five", 40: "forty", 45: "forty-five", 50: "fifty", 55: "fifty-five"}


MODE_SLOTS = ("CabinService", "AfterTakeoff", "SafetyBriefing", "Decompression")  # Prerecorded by default; "Manual" adds [Manual] to the file name
MODES = ("Prerecorded", "Manual")


# Fenix's old announcement names -> the new type and the flight-phase tags that keep the takeoff / landing split.
# The mapping is inferred from the names and the trigger table in the Fenix developer guide (it publishes no list).
BEFORE_TAKEOFF = ["PreFlight"]
BEFORE_LANDING = ["Descent", "Approach"]
LEGACY = {
    "CrewSeatsTakeoff": ("CrewTakeSeats", BEFORE_TAKEOFF), "CrewSeatsLanding": ("CrewTakeSeats", BEFORE_LANDING),
    "CallCabinSecureTakeoff": ("CabinSecured", BEFORE_TAKEOFF), "CallCabinSecureLanding": ("CabinSecured", BEFORE_LANDING),
    "CabinDimTakeoff": ("CabinDim", BEFORE_TAKEOFF),
    "FastenSeatbelt": ("SeatbeltSignOn", []), "DescentSeatbelts": ("DescentStarted", []),
    "DisembarkStarted": ("DeboardingStarted", []),
}


LOGO_MAX = 5 * 1024 * 1024


def logo_path(root: Path, project: dict) -> Path:
    return project_dir(root, project["icao"], project["voice"]) / "logo.png"


def set_logo(root: Path, project: dict, body: bytes) -> None:
    """Keep the pack's logo as a PNG (at most 512 px). FCAC shows it in the pack list and the window header."""
    import io
    from PIL import Image
    if not body or len(body) > LOGO_MAX:
        raise StudioError("That image is empty or too large (5 MB limit).")
    try:
        image = Image.open(io.BytesIO(body))
        image.load()
    except Exception:
        raise StudioError("That is not an image FCAC can read. Use a png, jpg or webp.")
    image = image.convert("RGBA")
    image.thumbnail((512, 512))
    path = logo_path(root, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def remove_logo(root: Path, project: dict) -> None:
    logo_path(root, project).unlink(missing_ok=True)


def raw_takes(raw_dir: Path, slot: dict) -> list[Path]:
    """The raw recordings of a slot. Compared by name, not by glob: stems like CrewTakeSeats[PreFlight] contain [ ]."""
    prefix = f"{slot['folder']}__{slot['stem']}"
    return [p for p in raw_dir.iterdir() if p.is_file() and p.stem == prefix] if raw_dir.is_dir() else []


def base_of(stem: str) -> str:
    return re.split(r"[\[~]", stem, maxsplit=1)[0]


def legacy_renames(folder: Path) -> list[tuple[str, str]]:
    """(old file name, new file name) for every legacy-named clip in a folder; skips ones whose new name is taken."""
    if not folder.is_dir():
        return []
    taken = {p.name.lower() for p in folder.iterdir()}
    out = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".ogg":
            continue
        base = base_of(path.stem)
        if base not in LEGACY:
            continue
        new_base, tags = LEGACY[base]
        rest = path.stem[len(base):]                       # existing [tags] and ~custom name
        head, tilde, name = rest.partition("~")
        extra = "".join(f"[{t}]" for t in tags if f"[{t.lower()}]" not in head.lower())
        new = f"{new_base}{head}{extra}{tilde}{name}.ogg"
        if new.lower() not in taken:
            out.append((path.name, new))
    return out


class StudioError(Exception):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_")[:40] or "Voice"


def project_dir(root: Path, icao: str, voice: str) -> Path:
    return root / icao / slug(voice)


def discard(root: Path, icao: str, voice: str) -> None:
    """Delete a project (recordings, processed clips, details). Never touches the Airlines library."""
    folder = project_dir(root, icao, voice)
    if not (folder / "project.json").is_file() or root.resolve() not in folder.resolve().parents:
        raise StudioError("That project does not exist.")
    shutil.rmtree(folder)
    parent = folder.parent
    if parent != root and not any(parent.iterdir()):
        parent.rmdir()


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


# --------------------------------------------------------------------------- airport names
_NAMES = None


def airport_name(code: str) -> str:
    """'London Heathrow' for EGLL (or EGLL_T5); '' when the airport is not in airport_names.json. Data: OurAirports (public domain)."""
    global _NAMES
    if _NAMES is None:
        _NAMES = {}
        for base in (getattr(sys, "_MEIPASS", None), Path(__file__).parent):
            f = Path(base) / "airport_names.json" if base else None
            if f and f.is_file():
                try:
                    _NAMES = json.loads(f.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    pass
    return _NAMES.get(code.split("_")[0].upper(), "")


# --------------------------------------------------------------------------- the checklist
def slot_list(project: dict) -> list[dict]:
    icao = project["icao"]
    out = []

    def add(group, folder, stem, label, script, required=False, out_name=None, part=None):
        slot_id = f"{folder}/{stem}"
        modes = list(MODES) if folder == "main" and stem in MODE_SLOTS else None
        mode = project.get("modes", {}).get(slot_id, MODES[0]) if modes else None
        tag = "[Manual]" if mode == "Manual" else ""
        out.append({"id": slot_id, "group": group, "folder": folder, "stem": stem, "label": label,
                    "script": script, "required": required, "file": f"{out_name or stem}{tag}.ogg",
                    "modes": modes, "mode": mode, "part": part})

    W1, W2 = "Part 1 - the greeting (ends on the start of the destination; the airport name follows)", "Part 2 - the rest of the welcome (after the airport name)"
    L1, L2 = "Part 1 - the arrival (welcome to, airport name, local time)", "Part 2 - the rest of the landing (after the time)"
    for stem, label, script in GREETINGS:
        if stem.startswith("Welcome_"):
            add("Welcome aboard", "time", stem, label, script, required=stem != "Welcome_Night", part=W1)
    add("Welcome aboard", "main", "Modular_Welcome", GENERAL[0][1], GENERAL[0][2], required=True, part=W2)
    for stem, label, script in GREETINGS:
        if not stem.startswith("Welcome_"):
            add("Landing", "time", stem, label, script, required=True, part=L1)
    for i, word in enumerate(HOURS):
        n = i or 12
        add("Landing", "time", f"Hour_{n:02d}", f"Hour {n}", word.capitalize(), required=True, part=L1)
    for minute, word in MINUTES.items():
        add("Landing", "time", f"Min_{minute:02d}", f"Minute :{minute:02d}", word.capitalize(), required=True, part=L1)
    add("Landing", "time", "AM", "AM (optional)", "A.M. - said after the minutes, e.g. 'four thirty-five A.M.'", part=L1)
    add("Landing", "time", "PM", "PM (optional)", "P.M. - said after the minutes, e.g. 'four thirty-five P.M.'", part=L1)
    add("Landing", "main", "Modular_Landing", GENERAL[1][1], GENERAL[1][2], required=True, part=L2)
    for stem, label, script in GENERAL[2:]:
        add("Other announcements", "main", stem, label, script)
    for code in project.get("airports", []):
        name = airport_name(code)
        add("Airports", "airports", f"{icao}_{code}", f"{code}: {name}" if name else f"Airport name - {code}",
            "Say airport name", required=False)
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


def variants_of(pack: Path, slot: dict, known: set[str] = frozenset()) -> list[str]:
    """Tagged copies of an announcement in the pack, e.g. CrewTakeSeats[PreFlight].ogg for the CrewTakeSeats slot."""
    folder = pack / slot["folder"]
    if slot["folder"] != "main" or not folder.is_dir():
        return []
    return [p.name for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() == ".ogg" and p.name != slot["file"] and p.name not in known
            and base_of(p.stem) == base_of(slot["stem"])
            and {t.lower() for t in re.findall(r"\[(.*?)\]", slot["stem"])} <= {t.lower() for t in re.findall(r"\[(.*?)\]", p.stem)}
            and not slot["stem"].startswith("BoardingWelcome")]


def status(root: Path, project: dict) -> list[dict]:
    folder = project_dir(root, project["icao"], project["voice"])
    done = project.get("slots", {})
    rows = []
    all_slots = hour_order(slot_list(project))
    known = {s["file"] for s in all_slots}
    for slot in all_slots:
        info = done.get(slot["id"])
        have = (folder / "pack" / slot["folder"] / slot["file"]).is_file()
        if have and info is None:
            info = {"imported": True}   # a clip that arrived by renaming
        have = have and info is not None
        variants = variants_of(folder / "pack", slot, known)
        if not have and variants:
            have, info = True, {"imported": True}
        slot = {**slot, "script": project.get("scripts", {}).get(slot["id"], slot["script"]),
                "custom": slot["id"] in project.get("scripts", {})}
        rows.append({**slot, "music": bool(slot.get("music")), "skipped": bool(not have and slot["id"] in project.get("skipped", [])), "done": have, "seconds": info.get("seconds") if have else None,
                     "lufs": info.get("lufs_out") if have else None, "imported": bool(have and info.get("imported")), "variants": variants})
    alphabetical = {"Other announcements", "Boarding music", "Bolt-ons"}  # the clock stays in clock order
    first = {}
    for i, r in enumerate(rows):
        first.setdefault(r["group"], i)
    def order(r):
        if r["group"] in alphabetical:
            return r["label"].lower()
        if r["group"] == "Airports":   # by airport, welcome aboard before landing
            return r["stem"].split("_", 1)[-1]
        return 0
    rows.sort(key=lambda r: (first[r["group"]], order(r)))
    return rows


# --------------------------------------------------------------------------- loudness reference
def reference_loudness(files: list[Path], progress=None) -> tuple[float, int]:
    """The loudness of the original Fenix announcements: the median of their integrated loudness -> (LUFS, how many
    were measured). Falls back to DEFAULT_TARGET."""
    values = []
    for i, path in enumerate(files[:12]):
        if progress:
            progress(i / max(1, min(len(files), 12)))
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
    for old in raw_takes(raw, slot):
        old.unlink()
    path = raw / f"{slot['folder']}__{slot['stem']}{ext}"
    path.write_bytes(body)
    return process_slot(root, project, slot, target)


def process_slot(root: Path, project: dict, slot: dict, target: float) -> dict:
    folder = project_dir(root, project["icao"], project["voice"])
    raws = raw_takes(folder / "raw", slot)
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
    project["skipped"] = [i for i in project.get("skipped", []) if i != slot["id"]]
    project.setdefault("slots", {})[slot["id"]] = result
    save(root, project)
    return result


def unprocessed(root: Path, project: dict) -> list[dict]:
    """Slots whose recording was saved but never turned into a clip (for example after a crash or an older bug)."""
    raw = project_dir(root, project["icao"], project["voice"]) / "raw"
    pack = project_dir(root, project["icao"], project["voice"]) / "pack"
    return [sl for sl in slot_list(project)
            if sl["id"] not in project.get("slots", {}) and not (pack / sl["folder"] / sl["file"]).is_file() and raw_takes(raw, sl)]


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


def set_mode(root: Path, project: dict, slot_id: str, mode: str) -> None:
    """Prerecorded (plain file name) or Manual (the file gets [Manual]). Renames a clip that is already processed."""
    slot = find_slot(project, slot_id)
    if not slot["modes"] or mode not in MODES:
        raise StudioError("That announcement has no mode choice.")
    pack = project_dir(root, project["icao"], project["voice"]) / "pack" / slot["folder"]
    old = pack / slot["file"]
    project.setdefault("modes", {})[slot_id] = mode
    new = pack / find_slot(project, slot_id)["file"]
    if old.is_file() and old != new:
        new.unlink(missing_ok=True)
        old.rename(new)
    save(root, project)


def delete_slot(root: Path, project: dict, slot_id: str) -> None:
    slot = find_slot(project, slot_id)
    folder = project_dir(root, project["icao"], project["voice"])
    for name in variants_of(folder / "pack", slot, {s["file"] for s in slot_list(project)}):
        (folder / "pack" / slot["folder"] / name).unlink(missing_ok=True)
    for path in raw_takes(folder / "raw", slot):
        path.unlink()
    (folder / "pack" / slot["folder"] / slot["file"]).unlink(missing_ok=True)
    project.get("slots", {}).pop(slot_id, None)
    save(root, project)


def processed_file(root: Path, project: dict, slot_id: str) -> Path | None:
    slot = find_slot(project, slot_id)
    path = project_dir(root, project["icao"], project["voice"]) / "pack" / slot["folder"] / slot["file"]
    return path if path.is_file() else None


def legacy_moves(pack: Path) -> list[tuple[str, str]]:
    """Old dep clips (just the airport's name, when the pack has the modular welcome) -> the airports folder."""
    dep, airports = pack / "dep", pack / "airports"
    modular = [pack / "main" / "Modular_Welcome.ogg", pack / "time" / "Welcome_Rest.ogg", pack / "parts" / "Welcome_Rest.ogg"]
    if not dep.is_dir() or not any(p.is_file() for p in modular):
        return []
    have = {p.name.lower() for p in airports.iterdir()} if airports.is_dir() else set()
    return [(f"dep/{p.name}", f"airports/{p.name}") for p in sorted(dep.iterdir())
            if p.is_file() and p.suffix.lower() == ".ogg" and p.name.lower() not in have]


def upgrade_plan(root: Path, project: dict) -> list[tuple[str, str]]:
    pack = project_dir(root, project["icao"], project["voice"]) / "pack"
    return [(f"main/{a}", f"main/{b}") for a, b in legacy_renames(pack / "main")] + legacy_moves(pack)


def upgrade_preview(root: Path, project: dict) -> list[dict]:
    return [{"old": a, "new": b} for a, b in upgrade_plan(root, project)]


def upgrade_names(root: Path, project: dict) -> int:
    """Rename / move the legacy clips in the project's copy (never the library) to the new Fenix names and folders."""
    pack = project_dir(root, project["icao"], project["voice"]) / "pack"
    done = 0
    for old, new in upgrade_plan(root, project):
        (pack / new).parent.mkdir(parents=True, exist_ok=True)
        (pack / old).rename(pack / new)
        project.setdefault("replaced", []).append(old)
        done += 1
    airports = pack / "airports"
    for path in sorted(airports.iterdir()) if airports.is_dir() else []:  # moved clips become airports in the checklist
        match = re.match(rf"^{re.escape(project['icao'])}_([A-Z0-9]{{4}}(?:_[A-Z0-9]{{1,8}})?)$", path.stem, re.I)
        if match and match[1].upper() not in project["airports"]:
            project["airports"].append(match[1].upper())
    project["airports"].sort()
    save(root, project)
    return done


# --------------------------------------------------------------------------- export
def voice_json(project: dict) -> dict:
    data = {"icao": project.get("icao", ""), "airline": project.get("airline", ""), "name": project["voice"], "artist": project.get("artist", ""), "credit_url": project.get("credit_url", ""),
            "donate_url": project.get("donate_url", ""), "accent": project.get("accent", ""),
            "description": project.get("description", "")}
    return {k: v for k, v in data.items() if v}


def set_skip(root: Path, project: dict, slot_id: str, skip: bool) -> None:
    """Mark an announcement as skipped (you are not recording it): it counts as done for the checklist and the export."""
    find_slot(project, slot_id)
    skipped = [i for i in project.get("skipped", []) if i != slot_id]
    if skip:
        skipped.append(slot_id)
    project["skipped"] = skipped
    save(root, project)


def missing(root: Path, project: dict) -> list[str]:
    return [s["label"] for s in status(root, project) if s["required"] and not s["done"] and not s["skipped"]]


def export(root: Path, project: dict, out_dir: Path) -> dict:
    """Write <ICAO>_<Voice>.zip (laid out like Airlines\\<ICAO>, ready for the library or for make_pack/catalog) and
    a catalog entry. Raises StudioError if required recordings are missing."""
    todo = missing(root, project)
    if todo:
        raise StudioError("Still to record: " + ", ".join(todo[:6]) + (f" and {len(todo) - 6} more" if len(todo) > 6 else ""))
    if not any(s["folder"] == "airports" and s["done"] for s in status(root, project)):
        raise StudioError("Add at least one airport and record its name.")
    folder = project_dir(root, project["icao"], project["voice"]) / "pack"
    out_dir.mkdir(parents=True, exist_ok=True)
    version = str(project.get("version", "1.0.0")).strip() or "1.0.0"
    pack_id = f"{project['icao'].lower()}-{re.sub(r'[^a-z0-9]+', '-', project['voice'].lower()).strip('-') or 'voice'}"
    tag = f"{pack_id}-{version}"                  # the GitHub release tag, and the zip's name: ezy-sam-james-1.0.0
    zip_path = out_dir / f"{tag}.zip"
    inside = f"voices/{slug(project['voice'])}"   # the voice lives in its own folder, so several voices of one airline merge
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*.ogg")):
            archive.write(path, f"{inside}/{path.relative_to(folder).as_posix()}")
        archive.writestr(f"{inside}/voice.json", json.dumps(voice_json(project), indent=2))
        if project.get("scripts"):
            archive.writestr(f"{inside}/scripts.json", json.dumps(project["scripts"], indent=2, ensure_ascii=False))
        archive.writestr("voice.json", json.dumps({"icao": project["icao"], "airline": project.get("airline", "")}, indent=2))
        if logo_path(root, project).is_file():
            archive.write(logo_path(root, project), "logo.png")
    entry = {"id": pack_id, "icao": project["icao"], "name": project["airline"], "voice": project["voice"], "version": version,
             "url": f"https://github.com/{PACKS_REPO}/releases/download/{tag}/{zip_path.name}",
             "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
             "size": zip_path.stat().st_size,
             "voices": [{k: v for k, v in voice_json(project).items() if k in ("name", "artist", "accent", "description")}]}
    entry_path = zip_path.with_suffix(".catalog.json")
    entry_path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    return {"zip": str(zip_path), "entry": str(entry_path), "size": entry["size"], "tag": tag}


IMPORT_FOLDERS = ("main", "time", "parts", "extra", "airports", "dep", "dest", "fuel")


def import_pack(root: Path, icao: str, airline: str, src: Path, fallback_name: str, progress=None) -> dict:
    """Open an existing voice folder (Airlines\\<ICAO> itself or voices\\<Name>) for editing: its clips are copied
    into a new project, untouched, and show as done. Opens the existing project if there already is one."""
    info = {}
    try:
        info = json.loads((src / "voice.json").read_text(encoding="utf-8-sig"))
        if not isinstance(info, dict):
            info = {}
    except (OSError, ValueError):
        pass
    voice = str(info.get("name") or fallback_name).strip()[:40] or fallback_name
    try:
        return load(root, icao, voice)
    except StudioError:
        pass
    files = []
    for folder in IMPORT_FOLDERS:
        base = src / folder
        if base.is_dir():
            files += [(p, "time" if folder == "parts" else folder) for p in sorted(base.iterdir())
                      if p.is_file() and p.suffix.lower() == ".ogg"]
    if not files:
        raise StudioError("That pack has no sound files to edit.")
    project = create(root, icao, airline or icao, voice)
    for key, limit in (("artist", 80), ("accent", 40), ("description", 200)):
        project[key] = str(info.get(key, "")).strip()[:limit]
    for key in ("credit_url", "donate_url"):
        value = str(info.get(key, "")).strip()[:300]
        project[key] = value if re.match(r"https?://", value, re.I) else ""
    for path, folder in files:  # work out the airports, bolt-ons and music tracks it contains
        stem = path.stem
        match = re.match(rf"^{re.escape(icao)}_([A-Z0-9]{{4}}(?:_[A-Z0-9]{{1,8}})?)$", stem, re.I)
        if folder == "airports" and match and match[1].upper() not in project["airports"]:
            project["airports"].append(match[1].upper())
        elif folder == "extra" and EXTRA_NAME.match(stem) and stem not in project["extras"]:
            project["extras"].append(stem)
        elif folder == "main" and stem.startswith("BoardingMusic~") and MUSIC_NAME.match(stem[14:]):
            project["music"].append(stem[14:])
    project["airports"].sort()
    pack = project_dir(root, icao, voice) / "pack"
    for i, (path, folder) in enumerate(files):
        if progress:
            progress(i / len(files))
        (pack / folder).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, pack / folder / path.name)
    for stem in MODE_SLOTS:  # a clip already called SafetyBriefing[Manual].ogg keeps that mode
        if (pack / "main" / f"{stem}[Manual].ogg").is_file() and not (pack / "main" / f"{stem}.ogg").is_file():
            project.setdefault("modes", {})[f"main/{stem}"] = "Manual"
    for slot in slot_list(project):
        if (pack / slot["folder"] / slot["file"]).is_file():
            project["slots"][slot["id"]] = {"imported": True}
    try:  # the scripts that were saved with the pack
        scripts = json.loads((src / "scripts.json").read_text(encoding="utf-8-sig"))
        if isinstance(scripts, dict):
            known = {slot["id"] for slot in slot_list(project)}
            project["scripts"] = {k: v[:4000] for k, v in scripts.items() if k in known and isinstance(v, str) and v.strip()}
    except (OSError, ValueError):
        pass
    project["origin"] = str(src)
    save(root, project)
    return project


def install_to_library(root: Path, project: dict, airline_dir: Path) -> Path:
    """Copy the project into Airlines\\<ICAO>\\voices\\<Voice> (or back over the pack it was opened from) so FCAC can
    use it straight away."""
    origin = Path(project["origin"]) if project.get("origin") else None
    dest = origin if origin and origin.is_dir() else airline_dir / "voices" / slug(project["voice"])
    src = project_dir(root, project["icao"], project["voice"]) / "pack"
    if not src.is_dir() or not any(src.rglob("*.ogg")):
        raise StudioError("Record something first.")
    shutil.copytree(src, dest, dirs_exist_ok=True)
    if origin and dest == origin:  # the old-named copies of upgraded clips are replaced, not left beside the new ones
        for old in project.get("replaced", []):
            (dest / (old if "/" in old else f"main/{old}")).unlink(missing_ok=True)
        project["replaced"] = []
        save(root, project)
    music = sorted((src / "main").glob("BoardingMusic*.ogg"))
    if music:  # boarding music is the airline's, not a voice's: it goes where FCAC copies the general PAs from
        (airline_dir / "main").mkdir(parents=True, exist_ok=True)
        for path in music:
            shutil.copyfile(path, airline_dir / "main" / path.name)
    (dest / "voice.json").write_text(json.dumps(voice_json(project), indent=2), encoding="utf-8")
    if project.get("scripts"):
        (dest / "scripts.json").write_text(json.dumps(project["scripts"], indent=2, ensure_ascii=False), encoding="utf-8")
    if logo_path(root, project).is_file():   # the logo belongs to the airline, so it goes beside its folders
        airline_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(logo_path(root, project), airline_dir / "logo.png")
    return dest
