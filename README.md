# KanjiKyoushi backend

Django API for KanjiKyoushi: learn to read kanji by typing real Japanese sentences.
The frontend lives in [kanjikyoushi-frontend](https://github.com/theUnrealSamurai/kanjikyoushi-frontend).

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
- **Review mode.** New words only come in while "learn new" is on. With it off, only due words are
  targeted and the warm-up words are left alone, and when nothing is due you are done. The app nudges you
  to clear your due words before learning more.
- **Picking sentences.** The next sentence is the one that covers the most due and new words while
  containing the fewest words you haven't been taught yet. Sentence mode only serves standalone sentences,
  never lines that exist only inside a passage.
- **Paragraph mode.** Unlocks once every N5 kanji is covered: known at onboarding, or in a word you know or
  have learned. You then type a whole passage (an Aozora Bunko paragraph) one line at a time, with the passage
  shown around the line. A passage carries several words at once: up to 8 due words (from the 30 most overdue)
  and up to 5 new ones, your warm-up words first, then words from the next 40 in the learning order, which it
  brings in (so new words stay in JLPT order, roughly). A passage is only picked when you have met at least 60%
  of its words (`PASSAGE_COVERAGE`); the rest, including those 40 until they're brought in, are shown with
  furigana and never graded. Due words come first: while any are due, every passage has 2 of them when any
  candidate does (else 1). While learning new words, every passage also has at least 1 new word, and 2 when any
  candidate left does; in review mode it brings in nothing new. Among those, the passage with the most due and
  new words wins, then the best-known, then the longest. Each target is graded once, on the first line
  containing it. When no passage qualifies you get a single sentence, so practice never stalls.
- **Onboarding.** Words made only of kanji you already know are skipped; the repeat marks 々〆ヶ don't count,
  so 人々 is skipped when you know 人.

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

## Passages (paragraph mode)

Paragraph mode types a paragraph one sentence at a time. Its text comes from [Aozora Bunko](https://www.aozora.gr.jp/),
through the cleaned dataset [globis-university/aozorabunko-clean](https://huggingface.co/datasets/globis-university/aozorabunko-clean)
(`aozorabunko-dedupe-clean.jsonl.gz`, in `data/aozora/`, gitignored).

**Licences.** The texts are public domain (Aozora Bunko). The dataset is CC BY 4.0, so credit
globis-university/aozorabunko-clean and Aozora Bunko wherever the passages are shown; each passage also
carries its title, author and Aozora card URL for the credit line. No AI is involved: the texts are human-written
and the ranking is a formula.

Two steps. The first needs fugashi and cutlet (run it on the Mac); the second is plain Python and also runs on the phone.

```bash
cd server
uv run python manage.py build_passages ../data/aozora/aozorabunko-dedupe-clean.jsonl.gz ../data/aozora/passages.jsonl.gz
uv run python manage.py load_passages ../data/aozora/passages.jsonl.gz            # add --replace to swap the source's passages
```

`build_passages` writes a gzipped JSON Lines file (a header, the dictionary data of every word, then one line per
passage, easiest first). It is keyed by text and reading, never database ids, so the same file loads anywhere.
Options: `--max-sentences` (150000), `--max-sentence-chars` (80), `--min-sentences` (3), `--min-chars` (120),
`--min-kanji` (5), `--per-work` (150), `--limit-works N` (to try it out). Meanings come from the cached JMdict
(`load_dictionary`); without it the file has none.

A paragraph is one line of a work in modern spelling (新字新仮名). It is dropped if it:

- is shorter than `--min-chars`, has fewer than `--min-sentences` sentences or `--min-kanji` kanji, or has a sentence longer than `--max-sentence-chars`
- doesn't end like a sentence, or has unbalanced 「」『』（）
- contains anything but kana, kanji and 、。！？「」『』（）…―ー・ (so any markup remnant, Latin letter or digit, 〇, くの字点, or a full-width space inside the text, as in wakachi children's stories)
- is mostly katakana
- is archaic: ゐゑヰヱ, 候, やう / てふ / せう, ふ-verbs (言ふ, 思ふ), 言はず, べし, けり, ざる, なり。, 知らぬ, unwritten small っ (あつた)
- uses a kanji outside the jōyō list (hard to type with an IME)

Sentences split on 。！？ but never inside 「」『』（）. The rest are ranked easiest first by the share of kanji
in N5-N4, then N3, then length; at most `--per-work` per work, then the top ones until `--max-sentences`.
A paragraph's `position` is its number among all of the work's paragraphs, so positions stay stable.

`load_passages` creates the missing words (frequency 0), reuses any sentence whose text already exists (leaving its
`standalone` flag alone) and creates the rest with `standalone=False`, then the passages. Rerunning skips passages that
are already loaded. `--replace` deletes the source's passages first, then the passage-only sentences left without one.
It never changes an existing word's frequency, and word frequencies count standalone sentences only
(`build_corpus` too), so a word that only appears in passages is never taught: it shows as unseen, with furigana.
`build_corpus --replace` deletes only standalone sentences that are in no passage.

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
| `POST /type/mode` | `learn_new`: `true` (learn new words) or `false` (review only); anything else is a `400` | switches mode, drops the sentence on screen and returns the next one (or `done`, or `409` until onboarded) |
| `POST /type/paragraphs` | `on`: `true` or `false`; anything else is a `400`, and so is `true` while paragraph mode is locked | switches paragraph mode, drops the sentence on screen and returns the next one (or `done`, or `409` until onboarded) |
| `POST /type/due` | none | the due list: `words` (most overdue first, at most 300), `kanji` (every distinct kanji in all due words, with how many due words use it) and the full `total_words` / `total_kanji` |
| `POST /type/stats` | `days`: window length, clamped to 7-365 (optional, default 30) | the stats report: `today`, `totals` (incl. `retention` and `streak`), `days` (every day of the window, zero-filled, oldest first), a 7-day due `forecast`, `jlpt` progress per level and `tracking_since` (`409` until onboarded) |

Every sentence and `done` response carries `stats`: `learned` (words in review), `due` (words due now),
`due_kanji` (distinct kanji in those words), `learning` (new words being warmed up), `learned_today` and `learn_new` (the mode),
plus `paragraphs` (paragraph mode is on; only ever true once unlocked), `paragraphs_unlocked`, and `n5_covered` /
`n5_total` (N5 kanji covered so far, out of 109).

History is recorded from the day it was added: a word's `learned_at` (when its warm-up finished), a log row per review of a learned word and one per typed sentence. "Today" and every per-day figure use the user's own day, so the client sends an `X-Timezone` header (an IANA name such as `Asia/Tokyo`) on every request; the server stores the last valid one and falls back to UTC. The header is allowed through CORS (`CORS_ALLOW_HEADERS`).

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

`passage` is `null` for a single sentence. In paragraph mode it is the passage around the line being typed:
`id`, `title`, `author`, `source` ("Aozora Bunko"), `url`, `licence`, `index` (0-based: the line being typed;
lines before it are done) and `lines` (`sentence_id`, `text` and `tokens` for every line). The payload's own
fields (`sentence_id`, `text`, `tokens`, `words`...) always describe `lines[index]`, so submitting and Tab work
as for a single sentence. The lines' roles use the whole passage's graded words, so review words on lines still
to come keep their furigana hidden and new words stay highlighted.

## Tests

```bash
cd server && uv run python manage.py test
```

The original ichiran + sparse-matrix version is kept on the `roshan-original-main` and
`roshan-original-dev` branches.
