from django.contrib import admin
from .models import Learner, Sentence, UserWord, Word


@admin.register(Word)
class WordAdmin(admin.ModelAdmin):
    list_display = ("text", "reading", "frequency")
    search_fields = ("text", "reading")


@admin.register(Sentence)
class SentenceAdmin(admin.ModelAdmin):
    list_display = ("text", "translation")
    search_fields = ("text",)
    exclude = ("words",)


@admin.register(UserWord)
class UserWordAdmin(admin.ModelAdmin):
    list_display = ("learner", "word", "status", "sightings", "due")
    list_filter = ("status",)
    raw_id_fields = ("word",)


admin.site.register(Learner)
