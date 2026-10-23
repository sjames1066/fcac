# FCAC - Fenix Cabin Announcements Configurator

FCAC is a system tray app that gives your Fenix cabin announcements the right **destination** for every flight.

It reads the airline and destination of your latest **SimBrief** plan, picks the matching recording from your
**Airlines** folder (`BAW_BIKF.ogg` for British Airways to Reykjavik), and puts it in your Fenix airline pack in place
of the generic file. Ready-made airline packs can also be **downloaded and installed from the tray menu with one
click**, and removed the same way.

## The Airlines folder

Your recordings live in an `Airlines` folder next to the app (for example `C:\FCAC\Airlines`), one folder per airline
ICAO code, each with `main`, `dep`, `dest` and `fuel` inside:

```
C:\FCAC\
  fcac.py
  config.json
  Airlines\
    BAW\
      main\   SafetyBriefing.ogg  ArmDoors.ogg ...   <- general PAs
      dep\    BAW_BIKF.ogg  BAW_EGLL.ogg  ...     <- welcome aboard
      dest\   BAW_BIKF.ogg  ...                    <- welcome to your destination
      fuel\   BAW_BIKF.ogg  ...                    <- boarding while refuelling
    AAL\
      main\  dep\  dest\  fuel\
```

Name each recording `<AIRLINE>_<destination ICAO>.ogg`. The destination is the ICAO code of where the flight is going.

**Several options for one airport?** Add a suffix: `BAW_EGLL.ogg`, `BAW_EGLL_T5.ogg`, `BAW_EGLL_GEN.ogg`, `BAW_UUDD_ALT.ogg`
(upper or lower case, it doesn't matter). When a flight's destination has more than one, FCAC pops up a small window
asking which you prefer (the plain file is listed as "Standard"). Tick **Remember my choice for this airport** to stop
being asked; **Forget remembered terminal choices** in the tray menu clears that. If `dep` has a `_T5` file but `dest`
doesn't, `dest` uses its plain file (or the original) for that flight. Closing the window picks the first option.
From the command line: `--once --airline BAW --dest EGLL --variant T5`.

| Folder | Replaces in the Fenix pack | Used for |
|--------|----------------------------|----------|
| `dep`  | `BoardingWelcome` (every Morning / Afternoon / Evening variant) | welcome aboard |
| `dest` | `AfterLanding` (every variant) | welcome to your destination |
| `fuel` | `BoardingWelcome[Refueling]` | boarding while refuelling |

- **No recording for an airport?** That announcement goes back to the airline's original generic file, so a previous
  destination never lingers.
- **`main`** holds the airline's general PAs (and the original generic BoardingWelcome/AfterLanding, used when a destination has no recording). Any other file there (SafetyBriefing, ArmDoors...) is copied into the Fenix pack when it is new or changed.
- **New airline?** Fly it once and FCAC creates its folders for you, or run `python fcac.py --add-airline DLH`.
- **Sub-brands sharing a pack.** Use the **Airline aliases** button at the bottom of the FCAC window (or edit
  `aliases.json` next to the app) to make one airline use another's recordings. The default is
  `{"SHT": "BAW", "CFE": "BAW", "EFW": "BAW"}` (BA Shuttle, CityFlyer and EuroFlyer use the BAW pack): a flight with
  airline SHT uses `Airlines\BAW` and fills the Fenix pack folder `AirlinePacks\SHT`, which Fenix needs because it
  matches the livery's airline code. Changes apply on the next update.
- The mapping is the `mappings` list in `config.json` if you ever want to change it. `airlines_dir` in `config.json`
  moves the Airlines folder somewhere else.

## Joined announcements (optional): the `parts` folder

Instead of recording a whole PA for every airport, record the airport-specific bit once and share the rest. Put these
shared pieces in `Airlines\BAW\parts` (names are not case-sensitive). When they are there, FCAC joins the pieces into
the full announcement and puts that in the Fenix pack.

**Welcome aboard** = greeting + destination + rest:

| Piece | Where | Example |
|-------|-------|---------|
| `Welcome_Morning.ogg`, `Welcome_Afternoon.ogg`, `Welcome_Evening.ogg` | `parts` | "Good morning, ladies and gentlemen, welcome aboard this flight to" |
| `BAW_EGLL.ogg` | `dep` | "London Heathrow" |
| `Welcome_Rest.ogg` | `parts` | everything after the destination |

FCAC makes the Morning, Afternoon and Evening files, so Fenix still chooses by time of day.

**Landing** = welcome to destination + local time + rest:

| Piece | Where | Example |
|-------|-------|---------|
| `BAW_EGLL.ogg` | `dest` | "Ladies and gentlemen, welcome to London Heathrow" |
| `LocalTime.ogg` | `parts` | "where the local time is" |
| `Hour_00.ogg` ... `Hour_23.ogg` | `parts` | "fourteen" |
| `Min_00.ogg`, `Min_05.ogg` ... `Min_55.ogg` | `parts` | "thirty-five" (`Min_00` is optional) |
| `Landing_Rest.ogg` | `parts` | everything after the time |

The time is rounded to the nearest 5 minutes. If a time piece is missing, the time is left out and the rest still plays.
Without `Welcome_Rest` / `Landing_Rest`, the `dep` and `dest` recordings are used whole, exactly as before.

**Live local time.** FCAC watches MSFS (SimConnect). When you have landed and slowed to 60 knots ground speed
(`landing_speed_knots` in `config.json`), it reads the local time at that airport from the sim and rebuilds the landing
PA with it. Before that, the landing PA uses SimBrief's planned landing time at the destination so something sensible is
there. A touch-and-go builds nothing. `segment_pause_ms` (default 60) is the pause left between joined pieces.

**Original announcements.** The first time FCAC changes an airline's pack it saves a copy of the original generic
files. **Restore original announcements** in the tray menu (or `--restore BAW`) puts them back at any time. You can
also put the generic files yourself in `Airlines\BAW\main`.

FCAC only edits a Fenix pack that already exists. If an airline has no pack in the Fenix folder it tells you and
creates your recordings folders, but never creates an empty pack (Fenix would use it instead of its default one).

Enjoying FCAC? [Buy me a Ko-fi](https://ko-fi.com/sjames1066) - there's a button for it at the bottom of the window too.

## Setup

1. Install Python 3.10+ for Windows, then in this folder: `pip install -r requirements.txt`
2. Edit `config.json`: set `simbrief_username` (the name, not the numeric ID). Set `catalog_url` too if you want
   downloadable packs (see "Publishing packs").
3. Run `pythonw fcac.py` (no console window). The icon appears in the system tray.
4. Put your recordings in `Airlines\<ICAO>\dep|dest|fuel`, or right-click the icon -> **Install airline pack**.

FCAC updates on every new SimBrief plan. Generate your plan and wait for the tray notification **before boarding
starts**; the sim reads the files when each announcement plays. Added a new recording for the flight you already
loaded? Click **Update from SimBrief now** so it is picked up.

### The FCAC window

On the very first run the window opens in the middle of your screen and explains which folders FCAC will change (your
Fenix airline packs, with your originals kept safe) before you do anything. After that it lives in the tray and shows
a notification each time it updates your announcements. Double-click the tray icon (or right-click -> **Open FCAC window**) for the main window: your latest SimBrief flight,
every airline pack with its logo, one-click **Install / Update / Uninstall** with a progress bar, the auto-update
switch and your SimBrief username. It also asks which recording to use when an airport has several. The first time
you run FCAC it asks for your SimBrief username there.

It is a small page served only to your own PC (127.0.0.1, protected by a random token) and shown in an app-style
Edge or Chrome window, so there is nothing extra to install. The typeface is [Inter](https://rsms.me/inter/)
(SIL Open Font License 1.1), built in so it looks the same offline.

**Settings** (button at the bottom of the window):
- **When I close this window:** keep FCAC running in the tray (default), or quit it completely.
- **When I minimize this window:** minimize normally, or **hide it in the tray** (it leaves the taskbar; double-click
  the tray icon to bring it back).
- **Start FCAC automatically:** no, **with MSFS**, or with Windows. "With MSFS" adds FCAC to MSFS 2024's `EXE.xml`
  (next to `UserCfg.opt`, for the Microsoft Store and Steam editions) so the sim launches it; your other entries
  there are left alone, a backup of the file is saved in `%LOCALAPPDATA%\FCAC\backups`, and choosing "No" removes
  FCAC's entry again. With that option you can also tick **Close FCAC when MSFS closes**. If the sim doesn't start
  FCAC, run MSFS as administrator the same way your other add-ons need (the usual EXE.xml requirement).
- Your SimBrief username.

**Logos.** FCAC doesn't ship airline logos. The pack list shows, in this order: a `logo.png` you put in
`Airlines\<ICAO>\` yourself, the logo the pack publisher links in the catalog (see `--logo-url` below), or a coloured
tile with the airline's ICAO code.

### Tray menu

- **Open FCAC window** (also the double-click action)
- **Update from SimBrief now**: re-check and re-apply immediately
- **Auto-update**: pause/resume checking (every 60 s by default)
- **Install airline pack**: packs in the catalog that aren't installed (or have an update). One click downloads, verifies and installs.
- **Uninstall airline pack**: one click removes what that pack added and **restores any pack that was in the Fenix
  folder before**. Recordings you made yourself stay in `Airlines`.
- **Refresh pack list**, **Restore original announcements**
- **Open Airlines folder**, open the Fenix airline packs folder, edit config, view log, quit

## Where files go

- **Your recordings:** `Airlines\<AIRLINE>\...` (next to the app)
- **Fenix pack:** `<MSFS 2024 Community>\fnx-airline-packs\AirlinePacks\<AIRLINE>` (folder name = `announcements_folder` in config)
- **The app's own data** (saved original announcements, backups, state, log): `%LOCALAPPDATA%\FCAC`

### How the Community folder is found

No path to type. In this order:

1. `community_dir` in `config.json`, if you set one
2. For each MSFS 2024 edition (**Microsoft Store / Xbox** and **Steam**; the Windows registry says which are installed
   and they're tried first): the `InstalledPackagesPath` line in that edition's `UserCfg.opt`, which also handles a
   custom install drive
3. The default Community folder of each edition

If nothing is found, the tray says so and you can set `community_dir` yourself.

## Downloadable airline packs

Installing a pack from the catalog adds its files to `Airlines\<ICAO>` (`main`, `dep`, `dest`, `fuel`) and its
general announcements to the Fenix folder. **Files you already have are never overwritten**: if you and the pack both have `BAW_BIKF.ogg`, yours is used.
Uninstalling removes only the files the pack added.

### Publishing packs

#### 1. Make the zip and catalog entry

```
python make_pack.py --icao BAW --name "British Airways" --version 1.0.0 ^
    --airline-dir "C:\FCAC\Airlines\BAW" ^
    --out dist ^
    --url https://github.com/YOU/REPO/releases/download/baw-1.0.0/BAW.zip
```

`--airline-dir` supplies `main`, `dep`, `dest` and `fuel` from your Airlines folder (or give `--main` yourself). This writes `dist\BAW.zip` and adds the pack (with a SHA-256 checksum) to `dist\catalog.json`.
Run it again for each airline; existing entries are replaced. The zip contains:

```
main\   the airline's general PAs (copied to AirlinePacks\<ICAO> and kept in Airlines\<ICAO>\main)
dep\    BAW_BIKF.ogg, BAW_EGLL.ogg ...
dest\   BAW_BIKF.ogg ...
fuel\   BAW_BIKF.ogg ...
```

Add `--logo-url https://.../logo.png` to show the airline's logo in the pack list (use artwork you have the right to
use). Files in `dep`/`dest`/`fuel` that aren't named `<ICAO>_<4-char ICAO>.ogg` are skipped with a message.

#### 2. Host it on GitHub

1. Create a repository. Commit `catalog.json` to the main branch.
2. For each pack, make a **Release** and attach `BAW.zip` (releases allow large files). Use that asset's download URL as `--url`.
3. Put the raw catalog URL in `config.json`:
   `"catalog_url": "https://raw.githubusercontent.com/YOU/REPO/main/catalog.json"`

To publish a new airline or an update, build the zip, attach it to a release, commit the updated `catalog.json`, and
users see it after **Refresh pack list** (or the next launch). Bumping `--version` shows it as "(update)".

`catalog_url` can also be a local file path while you test, with `url` entries relative to it.

Only recordings you have the right to distribute should go in a public pack.

## Safety

- Downloads are checked against the catalog's SHA-256 before anything is installed.
- Only the `main`, `dep`, `dest` and `fuel` folders are read from a zip, and any entry that tries to escape the install folder rejects the whole pack.
- A pack already in the Fenix folder is moved to `%LOCALAPPDATA%\FCAC\backups` and put back when you uninstall.
- Airline and airport codes are checked before they are used in a file name.
- If a file is locked by the sim, the app retries for about 6 seconds and then reports it in the tray.

## Command line (testing)

```
python fcac.py --community              show the detected Community folder
python fcac.py --add-airline BAW AAL    create Airlines\BAW\main|dep|dest|fuel (and AAL)
python fcac.py --list-packs
python fcac.py --install BAW
python fcac.py --uninstall BAW
python fcac.py --restore BAW            put the original generic announcements back
python fcac.py --once                   one update from SimBrief
python fcac.py --once --airline BAW --dest BIKF     force a flight, skip SimBrief
```

## Start with Windows / build an .exe

```
pip install pyinstaller
pyinstaller --noconsole --onefile --icon fcac.ico fcac.py
```

`fcac.ico` gives the .exe and its shortcuts the FCAC icon. The tray icon is drawn by `fcac_icon.py`, which is bundled
automatically; run `python fcac_icon.py` to re-export `fcac.ico` and `fcac_icon.png` if you change the design.

Put `config.json` and the `Airlines` folder next to `dist\fcac.exe`. To start with Windows, put a shortcut to it in
`shell:startup`.
