from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from type.dictionary import cached_dictionary, download_dictionary, fill_meanings
from type.models import Word


class Command(BaseCommand):
    help = "Fill in English meanings for every word from JMdict (jmdict-simplified); downloads it the first time."

    def add_arguments(self, parser):
        parser.add_argument("--file", type=Path,
                            help="a local jmdict-eng .json or .json.zip instead of downloading the latest release")

    def handle(self, file, **options):
        path = file or cached_dictionary()
        if file and not file.exists():
            raise CommandError(f"{file} does not exist")
        if path is None:
            self.stdout.write("Downloading the latest jmdict-simplified English release...")
            path = download_dictionary()
        self.stdout.write(f"Using {path}")

        total = Word.objects.count()
        filled = fill_meanings(Word.objects.all(), path)
        self.stdout.write(self.style.SUCCESS(f"{filled} of {total} words have meanings."))
