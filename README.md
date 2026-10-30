# KanjiKyoushi backend

Django API for KanjiKyoushi: learn to read kanji by typing real Japanese sentences.

> [!NOTE]
> This is the backend. The frontend (Next.js site) is
> **[kanjikyoushi-frontend](https://github.com/theUnrealSamurai/kanjikyoushi-frontend)**.

## How it works

- **Corpus.** `build_corpus` splits every sentence into words with [fugashi](https://github.com/polm/fugashi)
  (MeCab + UniDic). Each word gets its reading for furigana and its dictionary form (食べ → 食べる);
  [cutlet](https://github.com/polm/cutlet) adds romaji. This runs once, offline, at roughly a few thousand
  sentences a second. Nothing heavy happens per request.
- **Meanings.** `load_dictionary` fills each word's English meanings (up to 3 senses) from
  [JMdict](https://github.com/scriptin/jmdict-simplified), matching on text and reading.
- **The unit is the word**, keyed by text and reading (方/ほう and 方/かた are different words). Names
  and numbers get furigana but are never graded.
- **Learning.** New words are taken in JLPT order (N5 first; a word is as hard as its hardest kanji), most
  frequent first within a level, five at a time. Each is typed 3 times with
  furigana showing, then gets an [FSRS](https://github.com/open-spaced-repetition/py-fsrs) card.
- **Reviews happen while typing.** Each sentence grades up to 2 due words, with their furigana hidden:
  - typed without help → Good
  - needed Tab to see the reading → Hard
  - typed the wrong kanji → Again
- **Picking sentences.** The next sentence is the one that covers the most due and new words while
  containing the fewest words you haven't been taught yet.
- **Onboarding.** Words made only of kanji you already know are skipped.

## Setup

Needs [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cd server
uv run python manage.py migrate
uv run python manage.py build_corpus assets/sample_sentences.txt   # 2.4k-sentence sample
uv run python manage.py load_dictionary                            # English meanings (downloads JMdict once)
uv run python manage.py runserver
```

`build_corpus` takes a `.txt` file (one sentence per line) or a `.tsv`/`.csv` with a header row and a
`text` column, plus an optional `translation` column. The old `df_n1n5.tsv` exports load as they are.
Run it again to append more sentences; add `--replace` to swap out the corpus without losing learners'
progress. If the dictionary is already in `data/`, new words get their meanings too; otherwise it
prints a reminder to run `load_dictionary`.

### Filtering a big corpus

The file is streamed row by row, never loaded whole, so a multi-gigabyte table is fine. These options
skip rows before they are tokenized:

| Option | |
|---|---|
| `--min-length N`, `--max-length N` | sentence length in characters (defaults 1 and 60) |
| `--min-score X`, `--score-column COL` | skip rows scoring below X or with no valid score (column defaults to `score_ev_20230516`) |
| `--sample FRACTION`, `--seed N` | keep each passing row with this probability (seeded, default seed 0) |
| `--joyo-only` | skip sentences with any kanji outside the jōyō list (school grades 1–6 and 8) |
| `--limit N` | stop after loading N sentences |

Rows in the Massif `fragment_table.tsv` are in novel order, so `--limit` alone would only take the first few
novels; `--sample` spreads the loaded sentences over the whole file. A reasonable ~1M-sentence subset
(of the 30.4M rows, about 6.3M are 6-40 characters, score 1.46 or more and use only jōyō kanji, so
sampling 15% gives roughly 950k; the `--limit` is a safety cap that would otherwise cut the sample off
at the end of the file):

```bash
uv run python manage.py build_corpus ../../data/massif/fragment_table.tsv \
    --min-length 6 --max-length 40 --min-score 1.46 --joyo-only --sample 0.15 --limit 1000000
uv run python manage.py load_dictionary   # if you haven't yet; meanings for the new words
```

`load_dictionary` downloads the latest full English `jmdict-eng-*.json.zip` from jmdict-simplified into
`data/` (gitignored) the first time; `--file PATH` uses a local `.json` or `.json.zip` instead. It
prints how many words got meanings.

## Configuration

With no environment variables set you get a local dev setup: DEBUG on, SQLite at `server/db.sqlite3`,
and CORS open to `http://localhost:3000`.

| Variable | Default | |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev-only key | required when `DJANGO_DEBUG=0` |
| `DJANGO_DEBUG` | `1` | |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | comma-separated |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000` | comma-separated |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | unset | set `POSTGRES_DB` to use Postgres instead of SQLite |

## API

All `/type/*` endpoints need `Authorization: Bearer <access token>`.

| Endpoint | Body | Returns |
|---|---|---|
| `POST /home/signup` | `username`, `email`, `password` | tokens + user |
| `POST /home/login` | `username` (or email), `password` | tokens + user |
| `POST /type/onboard` | `kanji_list`: string of kanji already known | resets progress |
| `POST /type/next` | none | the next sentence (`409` until onboarded) |
| `POST /type/submit` | `sentence_id`, `typed`, `revealed` (word ids whose furigana was shown), `skipped` (word ids you asked how to type: not graded) | grades it and returns the next sentence |

A sentence comes back as tokens. Each token has its `ruby` (furigana pairs) and a `role`:
`review` (graded, furigana hidden), `learning` (new, furigana shown), `unseen` (not taught yet, furigana
shown), `known`, or `null` for kana and punctuation. Kana-only words like つぐむ also carry a `lookup` id.

The sentence also has a `words` object with the dictionary data for every word in it (Tab can open any
of them), keyed by word id (as a string, the same id as `token.word`):

```json
"words": {
  "42": {
    "text": "男", "reading": "おとこ", "meanings": ["man; male", "fellow; guy; chap; bloke"],
    "kanji": [{"kanji": "男", "on_readings": ["ダン", "ナン"], "kun_readings": ["おとこ"], "meanings": ["male", "man"]}]
  }
}
```

`meanings` is one string per sense (empty until `load_dictionary` has run).

`kana_words` has the same shape (with an empty `kanji` list), keyed by `token.lookup`: meanings for
kana-only words, found through UniDic's standard spelling (つぐむ → 噤む). Tab shows them on the page, but
they are never learned or graded; FSRS only tracks words written with kanji.

## Tests

```bash
cd server && uv run python manage.py test
```

The original ichiran + sparse-matrix version is kept on the `roshan-original-main` and
`roshan-original-dev` branches.
