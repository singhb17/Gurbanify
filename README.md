# Gurbanify

A personal shabad repertoire manager, for keertan.

Built to replace a Notion table that had grown past what a table can do. It
holds the shabads I want to learn or perform, lets me filter and rediscover
them, and finds thematically related lines across the whole collection — even
when they share no vocabulary at all.

Runs on a PC at home. No cloud service, no monthly cost, no accounts anywhere
but mine.

---

## What it does

**Library** — every shabad I have noted down, with the Gurbani, the English
translation and the Punjabi teeka pulled from [BaniDB](https://banidb.com).
Search by first letters the way STTM does (`gkbvv`), or by English keywords.

**My own metadata** — status, rarity, genre, speed, notes. This is the part
nothing can regenerate, and the part the whole design protects.

**Swipe deck** — a card stack for rediscovering shabads I have not looked at in
a while. Scrolling a list always lands on the same familiar ones because my
fingers know where they are; this exists to break that.

**Similarity search** — tap any line and get other lines that mean something
similar. Not keyword matching: each line is summarised by a language model and
turned into a vector, so *"chanting the Name"* and *"vibrating on the sweet
melody"* find each other. Searches every line of every shabad, not just the one
a shabad is filed under.

**Memorizing** — a list of what I am learning, with three ways to test myself
(type the first letters, pick the meaning, or read the whole shabad as first
letters only). Nothing is scheduled and nothing is graded. There was a full
spaced-repetition system here once; it worked, went completely unused, and was
deleted.

**Several accounts, private libraries** — the Gurbani is shared, everything
personal is not. A shabad someone else has already added costs nothing to index
and is searchable immediately, while their notes stay invisible.

---

## How the search works

```
add a shabad  →  BaniDB gives every line
                      ↓
              a language model writes a dense
              thematic summary of each line
                      ↓
              BGE-M3 turns each summary into
              a 1024-dimension vector
                      ↓
              stored in SQLite, once, forever
```

At query time there are **no API calls and no model loaded** — every vector is
already on disk, so a search is arithmetic over numbers in memory. About a
millisecond for 5,500 lines.

Summaries are keyed by model, so swapping to a better or cheaper one later is an
insert and a regenerate rather than a migration. That mattered sooner than
expected: one model's price doubled mid-project and the swap cost nothing.

---

## Built with

Python · FastAPI · SQLite · vanilla JavaScript (no framework, no build step) ·
[BGE-M3](https://huggingface.co/BAAI/bge-m3) embeddings · summaries via
OpenRouter · Cloudflare Tunnel

---

## Running it

**[MOVING.md](MOVING.md)** — setting it up on a new machine, in order, from
one command.

**[INSTRUCTIONS.md](INSTRUCTIONS.md)** — daily use, backups, indexing, and what
to do when something breaks.

**[CLAUDE.md](CLAUDE.md)** holds every design decision and the reasoning behind
it, including the things that were tried and deliberately removed.

Two databases are needed and neither is in this repo: `shabads.db` (the library
— personal, irreplaceable) and `banidb.db` (the Gurbani corpus — 91 MB,
regenerable from BaniDB).

---

## A note on the Gurbani

**No model ever generates, corrects or reconstructs Gurbani text.** Every line
of Gurmukhi comes from BaniDB or is entered by hand and verified. Language
models are used in exactly one place — writing English summaries that are
converted to vectors and never shown to a reader — and that output lives in a
layer that can be deleted and rebuilt at any time.

Where text is missing, it is left missing and the gap is shown.
