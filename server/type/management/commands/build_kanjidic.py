import json
import re
import urllib.request
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from type.dictionary import RELEASE_URL

KANJIDIC_ASSET = re.compile(r"kanjidic2-en-\d[^/]*\.json\.zip")


def cached_kanjidic():
    """The newest downloaded KANJIDIC2 file in data/, or None."""
    found = sorted(path for path in settings.DATA_DIR.glob("kanjidic2-en-*") if path.suffix in (".zip", ".json"))
    return found[-1] if found else None


def download_kanjidic():
    """Download the latest English KANJIDIC2 release (jmdict-simplified) into data/ and return its path."""
    with urllib.request.urlopen(RELEASE_URL, timeout=60) as response:
        assets = json.load(response)["assets"]
    asset = next((a for a in assets if KANJIDIC_ASSET.fullmatch(a["name"])), None)
    if asset is None:
        raise RuntimeError("No English KANJIDIC2 file in the latest jmdict-simplified release.")

    settings.DATA_DIR.mkdir(exist_ok=True)
    target = settings.DATA_DIR / asset["name"]
    partial = target.with_name(target.name + ".part")
    with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as response, open(partial, "wb") as file:
        while chunk := response.read(1 << 20):
            file.write(chunk)
    partial.rename(target)
    return target


def read_characters(path):
    """The list of KANJIDIC2 characters from a .json file or a .json.zip holding one."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".json"))
            with archive.open(name) as file:
                return json.load(file)["characters"]
    with open(path, encoding="utf-8") as file:
        return json.load(file)["characters"]


def trim(characters, wanted):
    """{kanji: {"on", "kun", "meanings"}} in KANJIDIC's own order (main sense and common reading first),
    English meanings only, for the kanji in `wanted`."""
    trimmed = {}
    for character in characters:
        kanji = character["literal"]
        if kanji not in wanted:
            continue
        groups = (character.get("readingMeaning") or {}).get("groups") or []
        readings = [r for group in groups for r in group.get("readings", [])]
        meanings = [m["value"] for group in groups for m in group.get("meanings", []) if m.get("lang") == "en"]
        trimmed[kanji] = {
            "on": [r["value"] for r in readings if r["type"] == "ja_on"],
            "kun": [r["value"] for r in readings if r["type"] == "ja_kun"],
            "meanings": meanings,
        }
    return trimmed


class Command(BaseCommand):
    help = ("Write assets/kanjidic2.json (readings and meanings in KANJIDIC's own order) for the kanji cards, "
            "from KANJIDIC2 via jmdict-simplified; downloads it the first time.")

    def add_arguments(self, parser):
        parser.add_argument("--file", type=Path,
                            help="a local kanjidic2-en .json or .json.zip instead of downloading the latest release")

    def handle(self, file, **options):
        path = file or cached_kanjidic()
        if file and not file.exists():
            raise CommandError(f"{file} does not exist")
        if path is None:
            self.stdout.write("Downloading the latest jmdict-simplified English KANJIDIC2 release...")
            path = download_kanjidic()
        self.stdout.write(f"Using {path}")

        with open(settings.ASSETS_DIR / "kanji_data.json", encoding="utf-8") as kanji_file:
            wanted = set(json.load(kanji_file))
        trimmed = trim(read_characters(path), wanted)

        target = settings.ASSETS_DIR / "kanjidic2.json"
        with open(target, "w", encoding="utf-8") as out:
            json.dump(trimmed, out, ensure_ascii=False, separators=(",", ":"))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(trimmed)} of {len(wanted)} kanji to {target} ({target.stat().st_size / 1024:.0f} KB)."
        ))
