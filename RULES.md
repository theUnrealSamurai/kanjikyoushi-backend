# KanjiKyoushi project rules

Rules for anyone (human or AI) working on KanjiKyoushi, compiled from the owner's instructions across past working sessions. Compiled 2026-10-26. Where a later instruction overrode an earlier one, only the latest is kept.

## Product principles

- No AI anywhere. All logic is deterministic and all text is human-written. Never add LLM-generated or LLM-simplified corpora, model-based difficulty scoring, or other AI features.
- Formula-based scoring (e.g. jReadability) is acceptable because it is not a model.
- The site is free and ad-free. Keep it that way, and keep the "no AI" promise loud on the home page.
- Kanji and words are taught in clean JLPT order (N5 first). A word is as hard as its hardest kanji. Keep this guaranteed in the logic.
- Corpus sources must be human-written text with a licence that permits this use. Check source and licence before the site goes public. Excluded: NHK News Web Easy (copyrighted), JParaCrawl and JESC (research-only), BCCWJ (paid).
- Preferred sources, per PLAN-paragraph-mode.md: Aozora Bunko (cleaned) for paragraphs, Aozora difficulty scores to rank works easiest first, SNOW T15/T23 and Tatoeba for sentence mode.
- Paragraph mode (not built yet): type one sentence at a time with the whole paragraph shown around it (bigger paragraphs are better), keep unlearned kanji in the paragraph to a minimum, and unlock it only after N5 is fully finished.
- Sentences and paragraphs should carry more review words each. Raise the per-sentence review and due-target limits as described in the plan.

## Learning and review behaviour

- The word is the unit, keyed by text and reading. Names and numbers show furigana but are never graded.
- New words get a warm-up (typed with furigana showing), then an FSRS card. Trust FSRS scheduling; there are no flashcards to rate.
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

- The practice page falls back to the browser's built-in translator for sentences with no translation. That is an on-device ML model, which conflicts with the no-AI principle. Unresolved.
- The current 1M-sentence corpus has an unknown source and licence. Verify before the site goes public.
- The exact definition of "N5 fully finished" for unlocking paragraph mode is undecided.
- Permission to use the Tadoku graded readers (CC BY-NC-ND) must be requested before using them.
- The code still grades at most 2 due words per sentence (`REVIEWS_PER_SENTENCE`) and targets the 6 most overdue (`DUE_TARGETS`), so a sentence with 4 due words scores the same as one with 2. Raising them (the plan proposes 4 and 12) is needed to follow the sentence-picking rule above; not done yet.
- How the Tab key on a grey (not-yet-carded) word should behave during onboarding was asked about and not recorded as a decision.
