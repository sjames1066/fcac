"""Called by Build EXE.bat after the installer is built: puts the new version, the GitHub download link and the
installer's SHA-256 into catalog.json's "app" block.  Usage: python update_catalog.py <version> <installer.exe>"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

version, installer = sys.argv[1], Path(sys.argv[2])
here = Path(__file__).resolve().parent
catalog_path = here / "catalog.json"
REMOTE = "https://raw.githubusercontent.com/sjames1066/fcac/main/catalog.json"
try:   # start from the live catalog so the pack list is never lost
    with urllib.request.urlopen(REMOTE, timeout=20) as reply:
        catalog = json.loads(reply.read().decode("utf-8"))
    print("Started from the catalog currently on GitHub")
except Exception as exc:
    if not catalog_path.exists():
        sys.exit(f"Could not read the catalog from GitHub ({exc}) and there is no local copy - not writing one.")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    print(f"GitHub not reachable ({exc}); started from the local catalog.json")
digest = hashlib.sha256(installer.read_bytes()).hexdigest()
app = catalog.setdefault("app", {})
app.update({
    "version": version,
    "installer": f"https://github.com/sjames1066/fcac/releases/download/v{version}/FCAC-Setup-{version}.exe",
    "sha256": digest,
})
app.setdefault("url", "https://github.com/sjames1066/fcac/releases/latest")
catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"{catalog_path} now announces FCAC {version}")
print(f"SHA-256 {digest}")
