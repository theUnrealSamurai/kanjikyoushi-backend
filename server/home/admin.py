from django.contrib import admin

from .models import InviteCode


@admin.register(InviteCode)
class InviteCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "note", "created", "used_by", "used_at")
    readonly_fields = ("used_by", "used_at")
