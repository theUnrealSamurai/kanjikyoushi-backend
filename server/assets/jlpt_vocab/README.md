# JLPT vocabulary lists

`n1.csv` ... `n5.csv` are the JLPT vocabulary lists, one file per level (columns: expression, reading, meaning,
tags, guid). `type/jlpt_list.py` reads them to decide which words KanjiKyoushi teaches, and in which level order.

**Credit: JLPT vocabulary lists by Jonathan Waller, tanos.co.uk, CC BY.**

- Source: https://github.com/jamsinclair/open-anki-jlpt-decks (`src/n1.csv` ... `src/n5.csv`, branch `main`,
  downloaded 2026-10-26). That project's data is "based on decks from tanos.co.uk" (via
  https://github.com/chyyran/jlpt-anki-decks), which are Jonathan Waller's JLPT lists, licensed CC BY.
  The repository's own MIT licence is in `LICENSE` (Copyright (c) 2020 Jamie Sinclair).
- File versions (git blob SHAs in the source repo):
  n1 `3cfe29d7af7bf200da6955e8e57c87a602ca0c39`, n2 `946ee8cbe9a00fd7acc57ad6744e59ab105b073d`,
  n3 `71cc46446a60497b20c359cf312d1fd4f5415b2d`, n4 `94bc42c6ef1bb40d0ef06f6d4235703a650feb8a`,
  n5 `11759690789422f3b84e32f693f20c0b87d657a9`.
- The CSVs are unmodified copies. All cleaning (dropping `～` and parenthesised parts, splitting `;` variants,
  katakana to hiragana) happens in code when they are read.
- The JLPT has published no official vocabulary list since 2010, so these are reconstructions, not the test's own
  lists.
