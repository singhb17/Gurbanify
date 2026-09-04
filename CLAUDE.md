# Shabad Library — Project Context

Handoff document. Everything decided so far, with reasoning. Read fully before writing code.

---

## 1. What this is

A personal shabad repertoire manager. **Single user. No auth system beyond gating access. No multi-tenancy. Do not build for other users.**

I do keertan in the AKJ tradition. When I hear or come across a shabad I want to learn or perform, I note it down. I need to store those, filter them, rediscover old ones, and find thematically related shabads across my collection.

This replaces a Notion database table.

---

## 2. Current state

**Capture flow today:**
- Hear a shabad → note it in a private WhatsApp chat to myself
- Periodically move entries into a Notion database table

**Notion table fields:**
- Status — done / in progress (still working out the tune) / only heard so far
- Speed — slow / medium / fast
- Rarity — common / rare
- Two more fields I don't remember; will confirm and add

**Also have:**
- An Excel sheet containing full lines of Gurbani
- ~~A small Python script that extracts the Gurmukhi line, English meaning and Punjabi meaning, printing to terminal.~~ **Done** — it grew into `import_shabads.py`, which writes straight to the database. The original scratch scripts were deleted once superseded.

**Library size:** ~500 shabads, averaging ~10 lines each ≈ 5,000 lines.

**BaniDB access:** confirmed working. Read access is available per their GitHub instructions; emailing them is only required for *write* access to their database, which is not needed here.

---

## 3. What the app must do

**Core (build first):**
- Add a shabad — search BaniDB by first letters, pick the match, store it
- Full CRUD on my own metadata fields
- Filter and sort by any tag
- List view that is actually usable on a phone

**Filters — BUILT, and shared by all three panels** (library, deck, similar):
- **Two tiers.** Basic: status, rarity, genre, speed, length. Advanced, behind a disclosure: raag, writer. Both are real filters, but raag is almost never the question and in the open it turns the panel into a wall you scroll past.
- **A hidden active filter is the bug to avoid**, so the collapsed summary counts advanced selections and the disclosure opens itself when any is set. A raag left on from last week, silently narrowing results with nothing on screen to explain it, looks exactly like correct behaviour.
- **Length is bands, not a slider** — Short 1–8 / Medium 9–16 / Long 17+, from the real distribution (median 11, p90 25). Multi-select is what makes one control cover every case: short+medium is a maximum of 16, medium+long is a minimum of 9. A "minimum lines" filter therefore needs nothing built.
- Bands are **defined server-side and served through `/api/filters`** with live counts, so the UI never hardcodes where "Short" ends.
- Everything routes through `filter_clauses()`, so a filter cannot mean one thing in the library and another in the deck or on the similar page.

**Similar page: filter first, then take the top 20.** The whole library is scored either way (~0.7 ms of numpy), so masking before the slice always returns a full page. Filtering the visible 20 afterwards would leave however few happened to match, and "show more" would exist to paper over that.
- **Paging belongs to the merged list, never to one model.** With an offset per model, a line model A puts on page 2 may be one model B already showed on page 1 — measured, and it repeated rows. Take each model's top (offset+limit+1) and page the merge.
- `model_results` records the **true unfiltered rank** of only what was actually shown. A rank that moved because a filter was on would make the model comparison depend on how the UI happened to be configured.
- Filters here **do not persist** — you arrive from one line for one reason, and a stale narrowing reads as a broken search.

**Swipe deck — BUILT:**
- Tinder-style card stack for rediscovering shabads I haven't done in a while
- Swipe left = dismiss, swipe right = adds to the `Interested` shortlist
- **Purpose:** when picking a shabad for a program I scroll and land on the same familiar ones because my fingers know where they are. This needs to break that habit.
- **Therefore: do not use pure random.** Weight the shuffle by recency — things not surfaced in a long time should come up more often. Pure `ORDER BY RANDOM()` misses the entire point.
- The `last_done` column was dropped: a date field maintained by hand never gets maintained. `last_surfaced` replaced it and the app writes it itself.

As implemented:
- Weight = days since `last_surfaced`; never-surfaced is treated as 400 days. Selection is Efraimidis-Spirakis (`u**(1/weight)`, take the largest) — a weighted sample without replacement in one pass, so no card repeats within a deck.
- Measured: after seeding 20 shabads at 1 day old and 20 at 200 days, across 2,000 draws the 1-day-old ones came up **zero** times. It is a weighting, not a hard ordering — they can still appear, just very rarely.
- `last_surfaced` is written **on the swipe**, not on render. A card preloaded behind two others hasn't been seen yet; one that's been swiped definitely has.
- Both directions stamp it — "I've seen this recently" is equally true of one I passed on.
- Shortlisted shabads are excluded from the deck. Swiping right twice is not an error.

**Memorization — BUILT, then deliberately gutted (2026-09-04).**

The goal is recall during keertan, not ratta. Somewhere between word-perfect and "the tune carries me" — the tune helps, but must not be load-bearing.

**A full SM-2 system was built and went unused.** Per-line ease factors, six scaffold levels, a rahao-first meaning gate, daily new-material caps, interval scheduling capped at 60 days, three-way grading, a `learning_lines` table. All of it worked. None of it was touched after the week it was written.

**The scheduling was why.** Being told what to practise and when turns something you want to do into something you are behind on — and the honest response to a feature you avoid is to delete it, not to tune it. 340 lines of `api.py` and 306 of `app.js` came out.

What replaced it, and the rule it follows — **give control, never impose structure**:

- **A list.** Add a shabad, remove it, that is the whole model. `last_practised` is recorded but is *information*, never a schedule — nothing reads it to decide what you should do next.
- **Three modes, always available, never unlocked:** first letters (type them, checked against `first_letters`), meaning (multiple choice English), and perform (the whole shabad as first letters, tap any line to reveal).
- **Prev/Next in every mode, wrapping both ways.** There is no correct direction through a shabad you are revising.
- **Nothing is graded and nothing is recorded.** Whether you got a line is between you and the line.
- **All lines included**, headers too. Line 1 is usually the raag/mahalla header, but not always — some shabads open on a real tuk, and a heuristic would misfire exactly where it matters. One extra line costs nothing to page past.
- **Meaning distractors come from the most similar lines** (§7's vectors), not random ones — near-misses force real discrimination where four random lines can be dismissed on subject alone. Falls back to random when a line has no vectors yet.
- **Perform mode is the one that maps onto actually singing it.** The others teach a line; this holds the shape of the whole.

There is now no irreplaceable progress here — the list itself is the only "mine" data, and it still goes in both backup forms, keyed on `banidb_shabad_id` so it survives a library rebuild.

**Similarity search (build last):**
- Tap a line → return other lines in my library with similar meaning
- **Must search all lines of every shabad, not just the line I originally saved the shabad by.** If I saved shabad X because of a line about naam, and a different line in shabad X is about fearlessness, that line must still be findable when I search from a fearlessness line elsewhere.
- Return ~20 results sorted by score, descending
- **No explanations needed. No LLM reranking step at query time.** This was considered and explicitly dropped. Score + sorted list is enough.

**Compare view — 90% decided, not built:**

Several models get indexed rather than one, and the search screen doubles as the thing that picks between them.

- **Why more than one model at all:** better and cheaper models keep arriving. Locking the derived layer to whichever won a bench in 2026 means every future swap is a migration. Keying summaries and vectors by model makes a swap an `INSERT` plus a regenerate.
- **The reversibility is the point.** Going back to one model is `DELETE FROM line_summaries WHERE model != 'x'` and a `VACUUM`. No schema change, no migration. Do not let this grow past that.
- **No global model selector in the navbar.** Six nav items already exist; a seventh for a decision made once is clutter. The comparison rides on the similarity screen, which is core product and stays after the decision is made.
- **Results are merged, shuffled and blind.** Both models' top-20 go into one list with no labels. Comparing two labelled columns means comparing one against a memory of the other, and knowing which model produced what biases the judgement.
- **One column, so phone and desktop share the design.** Desktop may reveal a labelled split *after* voting, never before.
- **Thumbs up and thumbs down per result card, not tapping.** Opening a shabad to check its tags is not an endorsement. Down-votes are load-bearing in their own right: they catch embeddings over-collapsing distinctions (§7 — anhad naad is not naam japna), which an absent up-vote cannot distinguish from not having got round to it.
- **Only uniquely-contributed results discriminate.** A line both models returned says nothing about either. Scores are reported twice: over everything, and over uniques alone.
- **Models can be switched off in settings**, not deleted. A model that keeps losing stops contributing to the merged pool but keeps its stored summaries and every past vote, so re-enabling is free.
- **A disabled model is never sent to the API.** Indexing — automatic or manual — targets only what is switched on, so switching off is also how you stop spending on one. Re-enabling is therefore the one action in the app that can cost real money, and it is **gated behind a priced confirmation**: lines outstanding, estimated cost, credit remaining. Agreeing starts a catch-up run capped at the figure shown, so the amount agreed to is the amount enforced.
- **Switching off is also the stop button.** There is no separate one. "Stop spending on this" is what the switch already means, so a run left going after it moved would make the switch a lie. It is a **cooperative stop** — a flag file the indexer checks after each line and each saved chunk — not a kill by pid: pids are recycled, and a flag also lets the run stop at a point of its own choosing, keeping the chunk already paid for. Embedding still finishes afterwards, because it is local, free, and skipping it would strand paid-for summaries in the one state the control panel calls a fault. The reverse does not hold: switching **on** never starts a run by itself, because that spends money.
- **Prices move mid-project, and the design has to absorb that.** DeepSeek v4 Pro roughly doubled overnight ($3.75 → $6.67 for a full index) — all twelve of its providers moved together, so an upstream rise, not a routing accident. `glm47nt` replaced it on 2026-08-19 at $3.41, having benched one point apart, which is noise across 108 lines. **The swap was an `UPDATE` and a regenerate.** No schema changed, and every vote survived, because `line_relations` records the judgement and not the model — which is the whole reason §5 keeps them in separate tables. Where a model has several providers at different prices, pin the cheapest in `models.json` with `allow_fallbacks` on.
- **A scores page** shows per-model up/down ratio, unique-contribution ratio, and coverage.

**Screen wake lock — BUILT.** The phone must not lock itself mid-keertan with a shabad on screen.
- **The native Screen Wake Lock API, not NoSleep.js.** That library predates the API and works by playing a hidden looping video — battery cost, and it can grab the audio session, which is wrong in an app used while a recording may be playing.
- **Requires https.** The API does not exist in an insecure context, so a plain `http://<lan-ip>:8000` cannot do it. Through the Cloudflare tunnel the browser sees https and it works; the http hop from `cloudflared` to this server is invisible to the browser.
- **The lock is auto-released every time the page stops being visible, and does NOT come back.** Re-acquiring on `visibilitychange` is the whole feature — without it it works once per launch and then silently stops, which reads as broken rather than expired. It also needs a user gesture, so the first attempt is retried on the first tap.
- Always on while the app is in front, one Settings switch, no per-screen rules. Routing is `pushState`, so the lock survives every navigation; only a hard reload drops it.
- The control panel reports **active / off / blocked / unsupported**, read from the browser rather than the server — whether this device's screen is being held is a fact the server cannot see, and "needs https" is a very different problem from "the browser refused".

**Control panel — BUILT.** `/status`, reached from Settings, not the nav.

**Diagnosis-only, with exactly one exception: Back up now.** Everything else reports a problem and names the command that fixes it rather than running it, so opening the page is safe precisely when something is already broken and the temptation is to press whatever is nearest. Backup earns the exception on a test the others fail — it only ever *adds* files, running it twice is harmless, and it is literally what the "no backups" alert instructs. The bar for a second button is that same test, not "it would be convenient". It runs `tools/backup.py` synchronously and returns that script's stdout verbatim, so there is one description of what happened rather than two that can drift.

- **Alerts are derived server-side**, because they are judgements ("a run that stopped is a problem; one you stopped is not") and belong beside the data they judge. The page only renders a sorted list, which is what keeps it simple as checks accumulate.
- Covers: balance, failed/stalled runs, **summarised-but-not-embedded** (the paid half worked, the free half didn't — fixed by `--embed-only` at no cost), malformed vectors, `prompt_ver` staleness, stale lock, missing backups, shabads with no lines.
- **The gear grows a red dot** when something needs attention. Checked once at startup, not polled — these conditions move slowly.
- One endpoint, not six: a diagnostics page assembled from separate requests can contradict itself about the same job.

**The votes outlive the decision, and that is the real prize.** A verdict of "these two lines are genuinely related" is a judgement about Gurbani, not about a model — §5 "mine", irreplaceable, and it stays true when every summary in the database is regenerated. Months of thumbing builds a labelled ground-truth set on the real library, which then evaluates any future model for free and at a scale the 108-line bench in `search/topics/` cannot approach. This is why the verdict is stored separately from which model surfaced it; merged into one table, regenerating summaries would throw the judgements away with the model output.

---

## 4. Architecture — decided

```
Add shabad
  ↓
BaniDB → ShabadID → all verses (Gurmukhi, English translation, Punjabi teeka)
  ↓
Postgres: raw text stored          ← app is fully usable from here
  ↓
[background, one-time per line]
LLM writes thematic summary per line
  ↓
Embedding model → vector per summary
  ↓
Stored in DB

Query time: compare stored vectors in JS. No API calls. Free. Instant.
```

**Key property:** when I tap a line to find similar ones, that line's vector is already stored. Nothing needs embedding at query time. The entire search is arithmetic on numbers already in memory.

---

## 5. Data model

Three categories of data, kept in separate columns/tables. This separation is deliberate and load-bearing:

| Layer | Contents | Property |
|---|---|---|
| **Raw** | BaniDB Gurmukhi, English translation, Punjabi teeka, ang, raag, writer | Never edit. Re-fetchable. |
| **Derived** | LLM summaries, embedding vectors | Disposable. Delete and regenerate freely when the prompt improves. |
| **Mine** | status, rarity, notes, and everything in `tags` (speed, genre) | **Irreplaceable. Nothing can regenerate this. Back it up.** |

Actual shape (see `schema.sql`, SQLite):

```sql
shabads (
  id, banidb_shabad_id, source_line, source_line_no,
  ang, raag_en, raag_pa, writer, source_en, source_pa,   -- raw
  rarity, status, notes,                                 -- mine (single-valued)
  last_surfaced,                                         -- written by the deck, never by hand
  is_user_added, imported_at
)

lines (
  id, shabad_id, line_no, banidb_verse_id,
  gurmukhi, transliteration_en, translation_en, teeka_pa,  -- raw
  summary, embedding                                        -- derived, SINGLE-model;
)                                                           -- superseded by line_summaries

tags (shabad_id, kind, value)    -- mine (multi-valued): speed, genre,
                                 -- later occasion / mood

shortlist (shabad_id, list, added_at)   -- mine: built by swiping right
```

Planned for the compare view in §3, not yet built:

```sql
line_summaries (                        -- derived, one row per line PER MODEL
  model, line_id, summary, embedding,
  prompt_ver, created_at,
  PRIMARY KEY (model, line_id)          -- model first: loading every vector for
) WITHOUT ROWID                         -- the active model is then a range scan

line_relations (                        -- MINE. Survives every model change.
  query_line_id, result_line_id, verdict, judged_at
)

model_results (                         -- derived: who surfaced what, disposable
  query_line_id, result_line_id, model, rank, prompt_ver
)
```

**`prompt_ver` is not optional.** The derived layer is regenerated whenever §6's prompt improves. Keyed on model alone, an improved prompt leaves a silent mix of old and new summaries with no way to tell them apart; a hash of the system prompt makes staleness a query.

**`ON DELETE CASCADE` on these needs `PRAGMA foreign_keys = ON` per connection.** SQLite defaults it off, silently.

**Backups exclude `line_summaries` and `model_results`** — disposable, and roughly 20 MB of vectors per model. `line_relations` goes in, because nothing can regenerate a human judgement.

**Why `shortlist` is its own table and not a `tags` row:** tags describe what a shabad *is*, are edited one at a time, and accumulate over years. A shortlist is working state — built in a burst before a program, emptied in one click afterwards. Putting a throwaway list in the same table as metadata that can't be regenerated invites a "clear" that takes the wrong rows with it. The `list` column exists already so a second folder is a no-op rather than a migration.

**Rule for where a field goes:** single-valued → column on `shabads` (the schema then enforces "one status per shabad"). Can ever hold two values → `tags`. Speed and genre are Notion multi-selects, so they must be rows; SQL has no multi-select type.

`source_line` is the line I know the shabad by — **this is what list views show.** `source_line_no` points at which verse that is, since verse 1 is always the raag/mahalla header (`ਗਉੜੀ ਮਹਲਾ ੫ ॥`), never a real tuk.

Blank tags import as the explicit value `Not chosen` — left blank on purpose, not missing data.

Dropped as unused: `last_done`, `recording_url`, `tune_source`. `last_surfaced` is the recency signal that replaced `last_done` — same purpose, except the app writes it (see §3).

Also store a shabad-level vector = mean of its line vectors, for shabad-level browsing.

**Support manual entry with the same shape.** BaniDB won't have everything, especially rarer panthic sources. Manually entered text must flow through the identical enrichment pipeline.

---

## 6. The summary step — this is where quality lives

Since the query-time reranker was dropped, **the summary is the only thing determining search quality.** Do not economise here.

**Model: Opus 5 or Sonnet 5 via the Batch API. Not Haiku.**
- Full library indexing costs roughly $7–14 one-time, ~$3–7 with Batch's 50% discount
- Haiku belongs nowhere in this pipeline now that reranking is dropped

**Input per line:** Gurmukhi + English translation + Punjabi teeka, all three.

Rationale: English translations flatten the meaning — they leave the rass behind. A Punjabi teeka carries grammar, implied subject, and padh chhedh reasoning that English drops. Feeding all three gives the model materially more to work with.

**Output shape — a sentence of meaning followed by a theme list:**

> Urges deliberate effort toward liberation. Human birth is a limited opportunity to cross the ocean of worldly existence; the time is being wasted in attachment to Maya. Themes: spiritual effort and discipline, urgency, the world as an ocean to be crossed, freedom from the cycle of birth and death, human life as a rare chance, distraction by worldly colour.

Rules for the summary:
- 40–80 words. Longer blurs the vector toward mush.
- **Deliberately restate the core idea several different ways.** Gurbani expresses the same concept through many images — chanting the Name, contemplating naam, vibrating on the tune, focusing on the sweet melody all point at remembrance. Varied phrasing in the summary helps those bridge to each other.
- No "This line says that…" — wasted tokens dilute the vector.
- Written to be embedded, not to be read.

**Token estimate:** ~300 input tokens per line (Gurmukhi tokenises expensively — roughly 60 tokens for a tuk, 25 for English, 200 for the teeka), ~50 output. 5,000 lines ≈ 1.5M input / 0.25M output.

**Before committing: measure.** Run 20 real lines through the token counting endpoint and multiply. My per-line estimate could be off by 2x for Gurmukhi.

---

## 7. Embeddings

**Model: BGE-M3.** Open weights, free, self-hosted. 100+ languages including Punjabi. Dense, sparse, and multi-vector retrieval in one model. 1024 dimensions.

Fallback if RAM is tight: EmbeddingGemma-300M, runs in under 200MB.

**Run it as a one-time script, not a service.** Python + sentence-transformers, walk the lines table, write vectors back. CPU-only is fine — expect 20–40 minutes for the full library, then only a handful of lines at a time when adding shabads. The web app never loads the model.

Exception: if free-text search ("find lines about fear of death") is added later, *that* needs the model running at query time. Not in scope for v1.

**Search implementation:**
- 5,000 vectors × 1024 dims × 4 bytes ≈ 20 MB total
- Load into memory, brute-force cosine in JS. ~5M multiply-adds, a few milliseconds.
- **Skip pgvector.** Two orders of magnitude below where an index earns its complexity.

**Critical: rank, never threshold.**
In high-dimensional space, unrelated vectors sit near perpendicular, so real cosine scores bunch in a narrow band — expect something like 0.31 to 0.72, not 0.05 to 0.99. A hardcoded `if (sim > 0.8)` returns nothing. Sort and take the top N. Never hardcode a similarity cutoff.

**Never auto-merge results.** Always show the actual tuk alongside the score. Embeddings can over-collapse genuine distinctions — anhad naad is not identical to naam japna. The judgement of whether two lines truly connect stays with me, not the machine.

---

## 8. Hosting

Everything runs on a mini PC at home that is always on.

- **Postgres** locally
- **Next.js** app
- **Python indexing script** beside it
- **Consider running BaniDB itself locally too** — their repo has setup instructions. This turns the 500-shabad import from 500 HTTP calls into SQL joins, and removes the network dependency entirely. Their DB read-only, mine for metadata and vectors, cross-referenced by ShabadID.
- **Cloudflare Tunnel** in front — no port forwarding, free HTTPS, survives dynamic IP and ISP port blocking
- **Cloudflare Access** for auth — gates it behind my Google login, zero auth code to write

**Backups are not optional.** The mini PC dying loses status flags, tunes, and last_done dates. Raw text and vectors are regenerable; my metadata is not. Automated nightly `pg_dump` to cloud storage, set up *before* importing data.

No Supabase, no Vercel, no monthly cost.

---

## 9. Frontend

**PWA, not a native app.** Installable to home screen, full-screen, one codebase for phone and laptop. Swipe gestures via pointer events, no library needed.

**Offline was a hard requirement; it has been relaxed.** The original reasoning stands — smagams, gurdwara wifi, basements — but in practice I'm at home or on data almost always. **Memorization v1 is online-only** (decided 2026-08-09): building the offline layer properly is a bigger job than the learning feature itself, and it was blocking it.

Still the target for the app generally, just not a gate on shipping features:

- Service worker caches the full library after first load (~5 MB of text)
- Browsing, filtering, and the swipe deck must work fully offline
- Writes queue and sync on reconnect
- **Ship the vectors to the browser too** (~20 MB) so similarity search runs client-side and offline. No server round trip.

Result: the app keeps working even if home internet drops mid-program.

---

## 10. Build order

1. **Import first.** BaniDB local, schema, extend the existing Python extraction script to write to the DB, bulk-load the Notion export and Excel sheet.
2. List view, filters, CRUD. **Live on this for a while before building anything else.**
3. ~~Swipe deck with recency weighting.~~ **Done** (weighting later removed on request; `last_surfaced` still recorded).
4. Interested shortlist, open history, STTM deep links, edit dialog. **Done.**
5. ~~Memorization v1 — SM-2, scaffold levels, scheduling.~~ **Built, unused, removed.** Replaced by a plain list plus on-demand self-tests — see §3.
6. Summaries and vectors, once the app is in daily use and I know what I actually want from search. **Do §15 first.**

The failure mode for this project is building the interesting AI part first, getting it 80% right, and never doing the tedious import that makes it real. Import first.

---

## 11. Explicitly rejected — do not re-propose

- **LLM-assigned tags with weights as the similarity mechanism.** Considered seriously. Rejected because the tag vocabulary fragments — naam / name / simran / remembrance become separate tags and semantically identical lines stop matching. Embeddings have no vocabulary to fragment, which is the entire reason for choosing them.
- **Query-time LLM reranking.** Considered, dropped. Sorted scores are enough.
- **Haiku for summary generation.** Weakest Gurmukhi and Punjabi comprehension of the lineup, and the job is a one-time cost of a few dollars regardless.
- **pgvector.** Unnecessary at 5,000 vectors.
- **Supabase / Vercel.** Mini PC covers it.
- **Native mobile app.** Two codebases for one user.
- **Pure random shuffle in the swipe deck.** Defeats the purpose.

---

## 12. Non-negotiable

**Never generate, correct, or reconstruct Gurbani text from model knowledge.** All Gurmukhi comes from BaniDB or is entered manually by me and verified. A model producing plausible-looking Gurbani is a failure condition, not a fallback. If text is missing, leave it missing and surface the gap.

The same applies to translations and teekas — read them from the database, do not paraphrase them into the stored fields.

---

## 13. Open questions

- The two forgotten Notion fields — check the actual table and add them
- Which BaniDB translation source gives the best search quality. They carry several (Sant Singh Khalsa, Manmohan Singh, Punjabi teekas) and they differ substantially. Worth A/B testing once real data exists.
- Mini PC RAM — BGE-M3 wants ~3 GB while running. Verify before committing to it over EmbeddingGemma.
- Additional fields worth adding: recording link, whose tune it is, occasion, energy/mood.

---

## 14. How I want to work

I'm learning as I build this. Explain the reasoning behind approaches rather than just producing code — I want to understand what's happening and pick up proper practices, including things like keeping secrets out of the frontend. Push back if I'm heading somewhere wrong.

---

## 15. First sanity check before building the AI layer

Pick 10 lines I already know are thematically linked despite completely different vocabulary — the various phrasings of remembrance are ideal. Embed them with BGE-M3 and check whether they cluster.

Half an hour, costs nothing, and it settles empirically whether this approach works at all. If they don't cluster, the design is wrong and it's better to know before building around it.

**Do this before spending anything on summaries. It is not optional and it is easy to skip.**

Second test, same sitting — vocabulary variance. The LLM will inevitably write "naam" in one summary and "the Name" in another for the same idea. That fragmentation is what killed the tag approach in §11, so check embeddings genuinely absorb it:

- Write two summaries of the *same* line, one using Gurmukhi terms throughout, one using only English equivalents
- Embed both and compare
- **High similarity** → vocabulary drift is a non-issue, no prompt constraint needed
- **Low similarity** → §6's prompt must require glossing terms inline (`naam (the divine Name)`) so both forms land in the vector

Do NOT solve this by forcing a fixed vocabulary list — that fights §6's "restate the core idea several different ways" rule and rebuilds the tag problem in spirit.
