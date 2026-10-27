"""Teach the list words whose spelling in the list differs from the corpus's: the kana suffix rows (～たち is 達,
～すぎ, ～やすい, ～おき) and other okurigana (終る is mostly 終わる in the corpus, 下す,おろす is 下ろす), see
type.jlpt_list. Some rare spellings lose their level to the common one; progress on them is kept (on the phone it
was only words marked known at onboarding) and nobody's current sentence is reset. Pure Python, so it also runs
on the phone."""
from django.db import migrations

from type.jlpt_list import apply_list_levels


def match_the_spellings(apps, schema_editor):
    apply_list_levels(apps.get_model("type", "Word"))


class Migration(migrations.Migration):

    dependencies = [
        ("type", "0010_teach_the_list"),
    ]

    operations = [
        migrations.RunPython(match_the_spellings, migrations.RunPython.noop),
    ]
