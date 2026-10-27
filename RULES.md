# KanjiKyoushi project rules

Rules for anyone (human or AI) working on KanjiKyoushi, compiled from the owner's instructions across past working sessions. Compiled 2026-10-26. Where a later instruction overrode an earlier one, only the latest is kept.

## Product principles

- No AI anywhere. All logic is deterministic and all text is human-written. Never add LLM-generated or LLM-simplified corpora, model-based difficulty scoring, or other AI features.
- Formula-based scoring (e.g. jReadability) is acceptable because it is not a model.
- The site is free and ad-free. Keep it that way, and keep the "no AI" promise loud on the home page.
- Only words on the JLPT vocabulary list (Jonathan Waller's Tanos lists, CC BY; `assets/jlpt_vocab`) are taught, in the list's level order: N5 to N2 first, then N1, most frequent first within a level. Every other word is out (owner, 2026-10-26: "nuke every other word that's not in the official list"): never taught, reviewed or graded, shown with furigana and a Tab card like a name. This replaced the old "a word is as hard as its hardest kanji" order.
- Corpus sources must be human-written text with a licence that permits this use. Check source and licence before the site goes public. Excluded: NHK News Web Easy (copyrighted), JParaCrawl and JESC (research-only), BCCWJ (paid).
- Preferred sources, per PLAN-paragraph-mode.md: Aozora Bunko (cleaned) for paragraphs, Aozora difficulty scores to rank works easiest first, SNOW T15/T23 and Tatoeba for sentence mode.
- Paragraph mode: type one sentence at a time with the whole paragraph shown around it (bigger paragraphs are better), keeping unlearned words to a minimum.
  - It unlocks once all 109 N5 kanji are covered (known at onboarding or in a learned word). A "Paragraphs" switch in the practice header turns it on; while locked it shows N5 progress.
  - One paragraph for one word is wasteful: every passage should carry several new words (up to 5, at least 2 when available, at most 2 on a line) and every due word in it. New words for passages come from the next ~40 words in the learning order, so the order stays roughly JLPT.
  - Due first: while you have due words, every passage includes them (at least 2 when available); new words come on top.
  - Never serve a passage where under 60% of its list words are known (words off the list don't count). Unknown words show furigana and aren't graded. If nothing qualifies, practice falls back to single sentences.
  - Passage text comes from Aozora Bunko (modern spelling, public domain, via the CC BY 4.0 aozorabunko-clean dataset): jōyō kanji only, so everything is typable with an IME, with no ellipses (… ‥), no archaic style, no markup and no very long sentences. Credit the title, author and Aozora Bunko on screen.
  - Passage-only sentences never appear in sentence mode, and words that only occur in passages are never queued for learning.
- Max out every sentence: it grades every due word in it. While learning, a sentence grades up to 2 new words and sentences with 2 are preferred (10 words warm up at a time, so most sentences can have 2).

## Learning and review behaviour

- The word is the unit, keyed by text and reading. Names and numbers show furigana but are never graded.
- Readings carry over (owner, 2026-10-27: "track it with the readings"): furigana is decided per kanji reading, not per word. A word never met shows no furigana when each of its kanji is read the way it is in a word the user has learned or knows (友達 makes 人達 readable; 発達, with 達 read たつ, still isn't), or is a kanji known at onboarding (any reading). Rendaku and っ count as the same reading; special readings (今日, 大人) must be learned as words. Such words aren't graded and Tab on them changes nothing. Warm-up words keep their furigana.
- The repeat marks 々〆ヶ aren't kanji to learn: a word like 人々 counts as known once 人 is known.
- A new word's first sighting is a test, with furigana hidden: typed without help (no Tab, help mode or ⌥Tab), it skips the warm-up and gets its FSRS card straight away. Otherwise it gets the warm-up (3 typings in all, furigana showing), then an FSRS card. Trust FSRS scheduling; there are no flashcards to rate.
- Reviews are graded only by whether you needed the reading: no help is Good, needing Tab (or help mode) for the reading is Hard. Typos never count.
- Due words are typed with furigana hidden.
- Sentence picking prefers the sentence covering the most due words (a sentence with 4 due words beats one with 2), while containing as few not-yet-taught words as possible.
- While "Learn new words" is on, every sentence contains at least one new word.
- Review mode (switch off) targets only due words. No new words come in, warm-up words are left alone, and when nothing is due the user is done.
- Encourage users to finish their due words before learning more.

## Practice page UI

- One row holds: the "Learn new words" switch (on = learn new words, off = review), the circular ⓘ beside it, a "See what's due" button to the left of the switch, and a counter of new words learned today.
- The ⓘ appears only when there are due words. Clicking it shows the due counts and the nudge to finish due words first.
- "See what's due" is a simple button in that same row. It opens a panel on the right listing the due words and kanji.
- Never show answers while the user is typing. The due list shows only the kanji and words, with no readings or meanings.
- Keep the UI simple and uncluttered. Prefer one clear control over scattered ones.

## Home page

- Keep the page short and cheeky in tone: a rotating hero line (Japanese with romaji and a translation) drawn at random from a JSON file on every load.
- The "why I made this" blurb, the free/no-ads note, the loud no-AI statement and the feature-request invitation stay on the page. Polish them but keep the owner's voice.
- Keep the "start typing" button prominent, with the short motto below it.
- Make small copy edits in one file, a few lines. Do not touch dozens of files for a text change.

## Stats

- Provide a simple stats page with graphs for the user: kanji learned through the app, words or kanji learned per day, and other useful metrics.

## Deployment and operations

- The app is self-hosted on the owner's spare Android phone (rooted, Debian chroot) on home Wi-Fi, running all day. It serves at most 4 or 5 users (the owner and a few friends). Do not propose paid hosting.
- Access is private only. Serve over a private Tailscale network and share with friends via node sharing. Never open a public tunnel (no Tailscale Funnel or similar) and never tunnel into the home network.
- Signup is invite-only. Keep rate limits on login, signup and the API. Block the admin from the outside. Only the needed API routes are forwarded.
- SSH to the phone is allowed from the home Wi-Fi only. The firewall admits the web port only from loopback and private LAN ranges.
- Never overwrite the phone's database. It holds every user's progress. Back it up before any change that touches data.
- The owner deploys new versions manually. Always get explicit confirmation before deploying to the phone. Run a local server for review first and wait for the go-ahead.
- Keep phone-specific deployment code on its own branch, separate from main, until told otherwise.
- The phone is a dedicated server: strip what it does not need, but keep battery health protected (held within a partial charge band).
- Build the frontend on the development machine, not the phone (the phone has little RAM).

## Working style

- Always use `uv` for Python (`uv run python ...`, `uv add ...`), never bare `python` or `pip`. Clean up any tool installed only for a one-off task.
- Do not start building until the owner explicitly says so. Questions are not instructions to build.
- When told to build, split separable work across parallel sub-agents with a shared contract, and do independent prep yourself. Trivial one-file edits can be done inline.
- Sub-agents default to Sonnet. Use Opus only for genuinely hard multi-step reasoning, and say why.
- Commit and push only when asked. Push to main only when told to.
- The two repos (frontend and backend) belong to the owner's personal GitHub account. Push with that account's token for the single command (do not switch the active account); the work account is refused.
- Keep dependencies patched. When the host reports security alerts after a push, check them and upgrade on request.
- Answer questions directly and keep suggestions short. Show options (such as copy lines) and let the owner pick.

## Open questions

- The practice page falls back to the browser's built-in translator for sentences with no translation, which is an on-device ML model. The owner chose to keep it, in paragraph mode too (2026-10-26), but the home page's "no AI anywhere" claim isn't strictly true while it stays.
- The current 1M-sentence corpus has an unknown source and licence. Verify before the site goes public.
- Permission to use the Tadoku graded readers (CC BY-NC-ND) must be requested before using them.
- List words written only in kana (about 800, e.g. 嘘, 嬉しい) and ones the tokenizer splits (お金, 映画館) can't be taught yet.
- The vocabulary list's CC BY credit is in the repo (`assets/jlpt_vocab/README.md`, backend README); whether to show it on the site too wasn't decided.
- How the Tab key on a grey (not-yet-carded) word should behave during onboarding was asked about and not recorded as a decision.
