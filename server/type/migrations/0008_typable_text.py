"""Fix sentences built before two corpus fixes (see type.corpus): drop invisible characters nobody can type (34
web-novel sentences started with U+202C, so they could never be finished), and read 1人 and 2人 as ひとり and
ふたり instead of a bare number and にん. Pure Python, so it also runs on the phone, which has no fugashi or cutlet."""
from django.db import migrations
from django.db.models import Q

from type.corpus import PEOPLE, counted_people, invisible_re, people_romaji, strip_invisible


def fix_sentences(apps, schema_editor):
    Sentence = apps.get_model("type", "Sentence")

    for sentence in Sentence.objects.filter(text__regex=invisible_re().pattern):
        text = strip_invisible(sentence.text)
        tokens = [[strip_invisible(surface), *rest] for surface, *rest in sentence.tokens]
        tokens = [token for token in tokens if token[0]]
        if not text.strip() or text != text.strip() or "".join(t[0] for t in tokens) != text \
                or Sentence.objects.filter(text=text).exists():
            sentence.delete()  # nothing left, odd spacing at the ends, or a copy of a sentence we already have
            continue
        romaji = sentence.romaji
        if invisible_re().match(sentence.text) and romaji.startswith("?"):  # cutlet wrote it as "? sono yoru, ..."
            romaji = romaji[1:].lstrip()
            romaji = romaji[:1].upper() + romaji[1:]
        sentence.text, sentence.tokens, sentence.romaji = text, tokens, romaji
        sentence.save(update_fields=["text", "tokens", "romaji"])

    people = Q()
    for digit in PEOPLE:
        people |= Q(text__contains=digit + "人")
    changed = []
    for sentence in Sentence.objects.filter(people):
        tokens = sentence.tokens
        merges = counted_people([t[0] for t in tokens], [t[1] for t in tokens])
        for index, reading in sorted(merges.items(), reverse=True):
            tokens[index:index + 2] = [[tokens[index][0] + "人", reading, None, None]]
        romaji = people_romaji(sentence.romaji)
        if merges or romaji != sentence.romaji:
            sentence.tokens, sentence.romaji = tokens, romaji
            changed.append(sentence)
    Sentence.objects.bulk_update(changed, ["tokens", "romaji"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("type", "0007_passages"),
    ]

    operations = [
        migrations.RunPython(fix_sentences, migrations.RunPython.noop),
    ]
