"""Rank sentences and words by their hardest kanji (corpus.kanji_rank) and add the Bookmark table, for the owner's
sentence picking: only sentences up to the learner's bookmark are searched. Existing sentences keep their ids, so
the bookmarks only bound the search properly once build_corpus has stored the corpus in rank order; until then
every sentence's rank is still checked. Pure Python, so it also runs on the phone (about a minute per million
sentences)."""
from django.db import migrations, models

from type.corpus import kanji_rank


CHUNK = 5000


def rank_everything(apps, schema_editor):
    Word, Sentence, Bookmark = (apps.get_model("type", name) for name in ("Word", "Sentence", "Bookmark"))
    with schema_editor.connection.cursor() as cursor:
        for model in (Word, Sentence):
            table, last = model._meta.db_table, 0
            while True:
                rows = list(model.objects.filter(id__gt=last).order_by("id").values_list("id", "text")[:CHUNK])
                if not rows:
                    break
                cursor.executemany(f"UPDATE {table} SET rank = %s WHERE id = %s", [(kanji_rank(text), id) for id, text in rows])
                last = rows[-1][0]

    last, bookmarks = 0, []
    for rank, top in (Sentence.objects.filter(standalone=True).order_by().values("rank")
                      .annotate(top=models.Max("id")).order_by("rank").values_list("rank", "top")):
        last = max(last, top)
        bookmarks.append(Bookmark(rank=rank, last_sentence=last))
    Bookmark.objects.bulk_create(bookmarks, batch_size=1000)


class Migration(migrations.Migration):

    dependencies = [
        ('type', '0011_list_spelling_fixes'),
    ]

    operations = [
        migrations.CreateModel(
            name='Bookmark',
            fields=[
                ('rank', models.SmallIntegerField(primary_key=True, serialize=False)),
                ('last_sentence', models.PositiveIntegerField()),
            ],
        ),
        migrations.AddField(
            model_name='sentence',
            name='rank',
            field=models.SmallIntegerField(default=9999),
        ),
        migrations.AddField(
            model_name='word',
            name='rank',
            field=models.SmallIntegerField(default=9999),
        ),
        migrations.RunPython(rank_everything, migrations.RunPython.noop),
        # Sentences with a word, below a bookmark: one index range per target word, however big the corpus is.
        migrations.RunSQL(
            "CREATE INDEX type_sentence_words_word_sentence ON type_sentence_words (word_id, sentence_id)",
            "DROP INDEX type_sentence_words_word_sentence",
        ),
    ]
