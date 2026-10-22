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
    dep/    <ICAO>_<destination ICAO>.ogg   -> replaces BoardingWelcome
    dest/   <ICAO>_<destination ICAO>.ogg   -> replaces AfterLanding
    fuel/   <ICAO>_<destination ICAO>.ogg   -> replaces BoardingWelcome[Refueling]

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
        if path.suffix.lower() != ".ogg":  # sound files only (not the _put_..._here.txt notes)
            continue
        if airline is not None:
            match = CLIP_NAME.match(path.name)
            if not match or match["airline"].upper() != airline or path.parent != folder:
                print(f"  skipped {path.relative_to(folder)} (expected {airline}_<ICAO>.ogg or {airline}_<ICAO>_<VARIANT>.ogg)")
                continue
        archive.write(path, f"{prefix}/{path.relative_to(folder).as_posix()}")
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--icao", required=True, help="airline ICAO code, e.g. BAW")
    parser.add_argument("--name", required=True, help="airline name shown in the tray menu")
    parser.add_argument("--version", required=True)
    parser.add_argument("--airline-dir", type=Path, help="your Airlines\\<ICAO> folder (main, dep, dest, fuel)")
    parser.add_argument("--main", "--pack", dest="main", type=Path, help="folder of the airline's general PA .ogg files")
    parser.add_argument("--dep", type=Path, help="folder of <ICAO>_<dest>.ogg for the welcome aboard")
    parser.add_argument("--dest", type=Path, help="folder of <ICAO>_<dest>.ogg for the arrival welcome")
    parser.add_argument("--fuel", type=Path, help="folder of <ICAO>_<dest>.ogg for the refuelling welcome")
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--catalog", type=Path, help="catalog.json to create or update (default: <out>/catalog.json)")
    parser.add_argument("--url", help="download URL of the zip (default: <ICAO>.zip next to the catalog)")
    parser.add_argument("--github", metavar="USER/REPO",
                        help="build --url for a GitHub Release tagged <icao>-<version>, e.g. sjames1066/fcac")
    args = parser.parse_args()

    icao = args.icao.upper()
    if not re.fullmatch(r"[A-Z0-9]{3}", icao):
        print("ERROR: --icao must be a 3-character airline code", file=sys.stderr)
        return 1
    if args.airline_dir:  # fill anything not given explicitly from the Airlines\<ICAO> folder
        for label in ("main", "dep", "dest", "fuel"):
            candidate = args.airline_dir / label
            if getattr(args, label) is None and candidate.is_dir():
                setattr(args, label, candidate)
    if args.main is None:
        print("ERROR: no main folder. Give --main, or put the general PAs in <airline-dir>\\main", file=sys.stderr)
        return 1
    for label, folder in (("main", args.main), ("dep", args.dep), ("dest", args.dest), ("fuel", args.fuel)):
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
        for label in ("dep", "dest", "fuel"):
            folder = getattr(args, label)
            if folder:
                print(f"{label}: {add_folder(archive, folder, label, icao)} clips")

    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    entry = {
        "icao": icao,
        "name": args.name,
        "version": args.version,
        "url": args.url or zip_path.name,
        "sha256": sha256,
        "size": zip_path.stat().st_size,
    }

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
