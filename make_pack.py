"""
Build an airline pack zip for FCAC and add it to your catalog.json.

    python make_pack.py --icao BAW --name "British Airways" --version 1.0.0 ^
        --airline-dir "C:\\FCAC\\Airlines\\BAW" ^
        --out dist ^
        --url https://github.com/YOU/REPO/releases/download/baw-1.0.0/BAW.zip

--airline-dir points at your Airlines\\<ICAO> folder and supplies main, dep, dest and fuel from it.
--main, --dep, --dest and --fuel can still be given to override any of them.

The zip contains:
    main/   the airline's general PAs (copied to ...\\AirlinePacks\\<ICAO> and kept in Airlines\\<ICAO>\\main)
    time/   greetings and local-time clips FCAC joins into the welcome and landing PAs (Welcome_Morning.ogg, Hour_04.ogg ...)
    extra/  bolt-on announcements, one of which is sometimes added to the welcome PA
    voices/ optional extra voice artists, each laid out like the airline folder
    airports/ <ICAO>_<destination ICAO>.ogg  just the airport's name; FCAC mixes it into the welcome aboard and landing PAs
    dep/, dest/  older packs: a finished welcome / "welcome to <airport>" clip per airport (still read)
    fuel/   the refuelling tag-on (and, in older packs, <ICAO>_<destination ICAO>.ogg)

--url is where the zip will be downloadable. Leave it out to store just "<ICAO>.zip", which works when the zip
sits next to catalog.json (and for testing with a local catalog).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

CLIP_NAME = re.compile(r"^(?P<airline>[A-Z0-9]{3})_(?P<dest>[A-Z0-9]{4})(?:_[A-Z0-9]+)?\.ogg$", re.IGNORECASE)


def add_folder(archive: zipfile.ZipFile, folder: Path, prefix: str, airline: str | None) -> int:
    count = 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() != ".ogg" and not (airline is None and path.name.lower() == "voice.json"):
            continue  # sound files only (and voice.json), not the _put_..._here.txt notes
        if airline is not None:
            match = CLIP_NAME.match(path.name)
            if not match or match["airline"].upper() != airline or path.parent != folder:
                print(f"  skipped {path.relative_to(folder)} (expected {airline}_<ICAO>.ogg or {airline}_<ICAO>_<VARIANT>.ogg)")
                continue
        archive.write(path, f"{prefix}/{path.relative_to(folder).as_posix()}")
        count += 1
    return count


def read_voices(airline_dir: Path | None) -> list[dict]:
    """The voices of an airline folder for the catalog: its own voice.json ("Default") and voices/<Name>/voice.json."""
    if airline_dir is None:
        return []
    found = []

    def one(folder: Path, fallback: str) -> dict:
        info = {"name": fallback, "artist": "", "accent": "", "description": ""}
        try:
            data = json.loads((folder / "voice.json").read_text(encoding="utf-8-sig"))
            for key in info:
                if str(data.get(key, "")).strip():
                    info[key] = str(data[key]).strip()
        except (OSError, ValueError, AttributeError):
            pass
        return info

    if (airline_dir / "voice.json").is_file():
        found.append(one(airline_dir, "Default"))
    root = airline_dir / "voices"
    if root.is_dir():
        found += [one(p, p.name) for p in sorted(root.iterdir(), key=lambda p: p.name.lower())
                  if p.is_dir() and any(p.rglob("*.ogg"))]
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--icao", required=True, help="airline ICAO code, e.g. BAW")
    parser.add_argument("--name", required=True, help="airline name shown in the tray menu")
    parser.add_argument("--version", required=True)
    parser.add_argument("--airline-dir", type=Path, help="your Airlines\\<ICAO> folder (main, dep, dest, fuel)")
    parser.add_argument("--main", "--pack", dest="main", type=Path, help="folder of the airline's general PA .ogg files")
    parser.add_argument("--time", dest="time_dir", type=Path, help="folder of greetings and local-time clips (Welcome_Morning, Hour_01, Min_35 ...)")
    parser.add_argument("--extra", type=Path, help="folder of bolt-on announcements (nut allergy, Wi-Fi unavailable ...)")
    parser.add_argument("--voices", type=Path, help="folder with one sub-folder per extra voice artist (each like the airline folder)")
    parser.add_argument("--airports", type=Path, help="folder of <ICAO>_<dest>.ogg: just the airport's name")
    parser.add_argument("--dep", type=Path, help="folder of <ICAO>_<dest>.ogg for the welcome aboard")
    parser.add_argument("--dest", type=Path, help="folder of <ICAO>_<dest>.ogg for the arrival welcome")
    parser.add_argument("--fuel", type=Path, help="folder of <ICAO>_<dest>.ogg for the refuelling welcome")
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--catalog", type=Path, help="catalog.json to create or update (default: <out>/catalog.json)")
    parser.add_argument("--url", help="download URL of the zip (default: <ICAO>.zip next to the catalog)")
    parser.add_argument("--logo-url", help="https:// link to the airline's logo image (shown in the pack list); you supply the artwork")
    parser.add_argument("--github", metavar="USER/REPO",
                        help="build --url for a GitHub Release tagged <icao>-<version>, e.g. sjames1066/fcac")
    args = parser.parse_args()

    icao = args.icao.upper()
    if not re.fullmatch(r"[A-Z0-9]{3}", icao):
        print("ERROR: --icao must be a 3-character airline code", file=sys.stderr)
        return 1
    if args.airline_dir:  # fill anything not given explicitly from the Airlines\<ICAO> folder
        for label, attr in (("main", "main"), ("time", "time_dir"), ("parts", "time_dir"), ("extra", "extra"),
                            ("voices", "voices"), ("airports", "airports"), ("dep", "dep"), ("dest", "dest"), ("fuel", "fuel")):
            candidate = args.airline_dir / label
            if getattr(args, attr) is None and candidate.is_dir():
                setattr(args, attr, candidate)
    if args.main is None:
        print("ERROR: no main folder. Give --main, or put the general PAs in <airline-dir>\\main", file=sys.stderr)
        return 1
    for label, folder in (("main", args.main), ("time", args.time_dir), ("extra", args.extra), ("voices", args.voices), ("airports", args.airports), ("dep", args.dep), ("dest", args.dest), ("fuel", args.fuel)):
        if folder is not None and not folder.is_dir():
            print(f"ERROR: --{label} folder not found: {folder}", file=sys.stderr)
            return 1

    tag = f"{icao.lower()}-{args.version}"
    if args.github and not args.url:
        args.url = f"https://github.com/{args.github}/releases/download/{tag}/{icao}.zip"

    args.out.mkdir(parents=True, exist_ok=True)
    zip_path = args.out / f"{icao}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        print(f"main: {add_folder(archive, args.main, 'main', None)} files")
        if args.time_dir:
            print(f"time: {add_folder(archive, args.time_dir, 'time', None)} pieces")
        if args.extra:
            print(f"extra: {add_folder(archive, args.extra, 'extra', None)} bolt-ons")
        if args.airline_dir and (args.airline_dir / "voice.json").is_file():
            archive.write(args.airline_dir / "voice.json", "voice.json")
        if args.voices:
            print(f"voices: {add_folder(archive, args.voices, 'voices', None)} files")
        for label in ("airports", "dep", "dest", "fuel"):
            folder = getattr(args, label)
            if folder:
                # fuel holds tag-on clips with any name; dep and dest hold <ICAO>_<airport>.ogg clips
                print(f"{label}: {add_folder(archive, folder, label, None if label == 'fuel' else icao)} clips")

    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    entry = {
        "icao": icao,
        "name": args.name,
        "version": args.version,
        "url": args.url or zip_path.name,
        "sha256": sha256,
        "size": zip_path.stat().st_size,
    }
    if args.logo_url:
        entry["logo"] = args.logo_url
    voices = read_voices(args.airline_dir)
    if voices:
        entry["voices"] = voices
        print("voices: " + ", ".join(v["name"] for v in voices))

    catalog_path = args.catalog or args.out / "catalog.json"
    catalog = {"packs": []}
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["packs"] = [p for p in catalog.get("packs", []) if str(p.get("icao", "")).upper() != icao] + [entry]
    catalog["packs"].sort(key=lambda p: p["icao"])
    catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    print(f"\nWrote {zip_path} ({entry['size']:,} bytes) and updated {catalog_path}")
    if args.github:
        print(f"Next: make a GitHub Release tagged  {tag}  at https://github.com/{args.github}/releases/new")
        print(f"      attach {zip_path.name}, then upload {catalog_path.name} to the repo root.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
