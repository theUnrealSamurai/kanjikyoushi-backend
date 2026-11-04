"""Send warm-up words past each learner's stage back to unmet, after the switch to the kanji-by-kanji learning order
(see Learner.release_warmups). Pure Python and Django, so it runs on the phone. Dry run unless --apply.
"""
from django.core.management.base import BaseCommand

from type.models import Learner, teachable_words


class Command(BaseCommand):
    help = "List (or with --apply, release) warm-up words from a stage past the one each learner is on."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="delete them (otherwise only list them)")

    def handle(self, apply, **options):
        teachable = teachable_words()
        for learner in Learner.objects.filter(onboarded=True).select_related("user"):
            released = learner.release_warmups(teachable, apply=apply)
            self.stdout.write(f"{learner}: {len(released)} {'released' if apply else 'to release'} {' '.join(released)}")
        if not apply:
            self.stdout.write("Dry run: pass --apply to release them.")
