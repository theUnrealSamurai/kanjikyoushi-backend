import secrets

from django.contrib.auth.models import User
from django.db import models


CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I, so codes survive being read out loud


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))


class InviteCode(models.Model):
    """Signup is invite-only: each code lets one person make an account. Make them with `manage.py invite`."""

    code = models.CharField(max_length=32, unique=True, default=new_code)
    note = models.CharField(max_length=100, blank=True)  # who it was for
    created = models.DateTimeField(auto_now_add=True)
    used_by = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="invite")
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.code} ({f'used by {self.used_by}' if self.used_by else 'unused'})"
