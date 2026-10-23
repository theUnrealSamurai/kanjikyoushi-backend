# KanjiKyoushi backend

Django API for KanjiKyoushi: learn to read kanji by typing real Japanese sentences.
The frontend lives in [kanjikyoushi-frontend](https://github.com/theUnrealSamurai/kanjikyoushi-frontend).

## How it works

- **Corpus.** `build_corpus` splits every sentence into words with [fugashi](https://github.com/polm/fugashi)
  (MeCab + UniDic). Each word gets its reading for furigana and its dictionary form (食べ → 食べる);
  [cutlet](https://github.com/polm/cutlet) adds romaji. This runs once, offline, at roughly a few thousand
  sentences a second. Nothing heavy happens per request.
- **The unit is the word**, keyed by text and reading (方/ほう and 方/かた are different words). Names
  and numbers get furigana but are never graded.
- **Learning.** New words are taken in corpus-frequency order, five at a time. Each is typed 3 times with
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
uv run python manage.py runserver
```

`build_corpus` takes a `.txt` file (one sentence per line) or a `.tsv`/`.csv` with a header row and a
`text` column, plus an optional `translation` column. The old `df_n1n5.tsv` exports load as they are.
Run it again to append more sentences; add `--replace` to swap out the corpus without losing learners'
progress. See `--help` for the other options.

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
| `POST /type/submit` | `sentence_id`, `typed`, `revealed` (word ids whose furigana was shown), `missed` (word ids typed wrong) | grades it and returns the next sentence |

A sentence comes back as tokens. Each token has its `ruby` (furigana pairs) and a `role`:
`review` (graded, furigana hidden), `learning` (new, furigana shown), `unseen` (not taught yet, furigana
shown), `known`, or `null` for kana and punctuation.

## Tests

```bash
cd server && uv run python manage.py test
```

The original ichiran + sparse-matrix version is kept on the `roshan-original-main` and
`roshan-original-dev` branches.
