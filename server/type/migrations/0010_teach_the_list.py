"""Teach only the words on the JLPT vocabulary list (assets/jlpt_vocab, see type.jlpt_list): give every word its level
on the list, then drop the learners' progress on the words that aren't on it, so they're never reviewed or taught
again (like names, they show furigana and a Tab card). The sentence on screen may grade one of them, so it's dropped
too. Pure Python, so it also runs on the phone."""
from django.db import migrations

from type.jlpt_list import apply_list_levels


def teach_the_list(apps, schema_editor):
    Word, UserWord, Learner = (apps.get_model("type", name) for name in ("Word", "UserWord", "Learner"))
    apply_list_levels(Word)
    UserWord.objects.filter(word__list_level__isnull=True).delete()
    Learner.objects.update(current={})


class Migration(migrations.Migration):

    dependencies = [
        ("type", "0009_word_list_level"),
    ]

    operations = [
        migrations.RunPython(teach_the_list, migrations.RunPython.noop),
    ]
