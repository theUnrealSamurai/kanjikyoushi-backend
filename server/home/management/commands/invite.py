from django.core.management.base import BaseCommand

from home.models import InviteCode


class Command(BaseCommand):
    help = "Make one-time signup invite codes, or list the unused ones."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1, help="how many codes to make (default 1)")
        parser.add_argument("--note", default="", help="who the code is for, to remember later")
        parser.add_argument("--list", action="store_true", help="list unused codes instead of making new ones")

    def handle(self, count, note, list, **options):
        if list:
            for invite in InviteCode.objects.filter(used_by__isnull=True).order_by("created"):
                self.stdout.write(f"{invite.code}  {invite.created:%Y-%m-%d}  {invite.note}")
            return
        for _ in range(count):
            self.stdout.write(InviteCode.objects.create(note=note).code)
