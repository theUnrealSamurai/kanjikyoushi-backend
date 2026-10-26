"""Mark which Words are on the JLPT vocabulary list (Word.list_level).

Pure Python and Django, so it runs on the phone as well as on the Mac.
"""
from django.core.management.base import BaseCommand

from type.jlpt_list import apply_list_levels
from type.models import Word


class Command(BaseCommand):
    help = ("Set Word.list_level (5 = N5 ... 1 = N1, empty = not on the list) from assets/jlpt_vocab. "
            "Re-run after build_corpus or load_passages add words.")

    def handle(self, **options):
        counts = apply_list_levels(Word)
        for level, count in sorted(counts.items(), reverse=True):
            self.stdout.write(f"N{level}: {count} words")
        listed = Word.objects.filter(list_level__isnull=False)
        self.stdout.write(self.style.SUCCESS(
            f"{listed.count()} words are on the list; {listed.filter(frequency__gt=0).count()} of them occur in the corpus (frequency > 0)."
        ))
