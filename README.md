# Shabad Library — commands

Everything you need day to day. `CLAUDE.md` holds the design and reasoning; this is just the buttons.

---

## Every day

**Double-click `restart.bat`.** That is the whole thing — it stops whatever is
running, starts the app, opens a tunnel, checks the link actually works, copies
it to your clipboard and pushes it to your phone.

Or, by hand:

```bash
python -m uvicorn api:app --port 8000        # start the app -> http://localhost:8000
python tools/backup.py                       # back up (do this after editing)
```

Docker is **not** needed to run the app. Ctrl+C stops the server.

---

## Running it over the internet

### One-time setup

Put these in `.env` (copy `.env.example`):

```
APP_USER=keertan
APP_PASSWORD=<something only you know>
NTFY_TOPIC=gurbanify-<random>
```

Then install **ntfy** on your phone (free, no account) and subscribe to that
exact topic. That is where the link gets sent.

**The topic name is the only secret.** Anyone who knows it can read your
notifications, which contain the address of your library. Never a guessable one.

### Starting it

```
restart.bat                                    everything, with a public link
powershell -File tools\serve.ps1 -NoTunnel     local only, no link
```

What it does, in order: kills anything on port 8000 and any stray `cloudflared`
→ starts the app → waits for `/health` to actually answer → starts the tunnel →
**fetches the link to check it routes** → retries with a fresh one if it doesn't
→ copies it to the clipboard and pushes it to your phone.

That fetch-and-retry is the point. A Quick Tunnel regularly hands back a
hostname that is not yet routable, and the only way to know is to try it.

### Keeping it up forever

```powershell
powershell -File tools\register-task.ps1       # as Administrator, once
Start-ScheduledTask -TaskName GurbanifyWatchdog
Get-Content logs\watchdog.log -Wait -Tail 20   # watch it
```

The watchdog checks `/health` every 30 seconds, restarts whatever died, and
**notifies you only when the URL changes** — a flapping tunnel would otherwise
wake you hourly. It also re-sends the current link every 8 hours, which keeps a
live copy inside ntfy's ~12-hour retention window even if your phone was off.

Registered `AtStartup` and as `SYSTEM`, so a power cut or a Windows Update
reboot brings everything back with nobody logged in.

Remove it with `powershell -File tools\register-task.ps1 -Remove`.

### The password

Every request needs it except `/health`. Your browser asks once and remembers.

**There is no exemption for localhost, deliberately.** `cloudflared` runs on
this machine and proxies to `http://localhost:8000`, so every request off the
public internet arrives looking local — an exemption for local addresses would
exempt the entire internet.

`serve.ps1` refuses to open a tunnel when `APP_PASSWORD` is empty. A Quick
Tunnel URL is public, and without a password anyone who finds it can edit and
delete your library.

### Why not a fixed address

Quick Tunnel URLs change whenever `cloudflared` restarts, which is what all of
the above exists to work around. A **named tunnel** gives a permanent hostname
and makes the watchdog unnecessary — but it needs a domain (~$10/yr) on
Cloudflare. Worth it the day the random links become annoying.

---

## Backups

```bash
python tools/backup.py                             # snapshot + Notion-ready csv
python tools/backup.py --keep 100                  # retain more of them (this run only)
python tools/backup.py --export-only my-export.csv # just a csv, no snapshot
```

Writes two files to `backups/` each run:

| file | what it's for |
|---|---|
| `shabads-<stamp>.db` | exact restore — copy it back over `shabads.db` |
| `shabads-<stamp>.csv` | the real escape hatch — import straight into Notion |

**Retention: the last 30 _backups_, not the last 30 changes.** Nothing tracks
individual edits — one run of `tools/backup.py` is one restore point. So how far back
you can reach depends on how often you run it: nightly only gives you ~30 days,
but ten runs in one afternoon will push out three weeks of older ones.

`--keep` applies to the run you pass it to, it isn't remembered. For a
permanently bigger window, change the default in `tools/backup.py`. A pair is ~2.8 MB,
so 100 of them is ~280 MB — cheap for what it protects.

**How often:** the question is *how much work am I willing to redo.*

- **After any session where you edited or added shabads.** This is the one that matters. The Gurbani text and vectors are re-fetchable; your status/rarity/tags/notes are not (CLAUDE.md §5)
- **Before any import or schema change** — those touch many rows at once
- **Nightly, automated** — catches the times you forget. See below

Each backup is ~2.8 MB, so 30 of them is ~85 MB. Disk is cheap; keep plenty. The likeliest thing you'll actually need a backup for isn't a dead disk — it's undoing a bulk edit you regret.

### Automate the nightly one (Windows Task Scheduler)

Run once, in PowerShell **as Administrator**:

```powershell
$py = (Get-Command python).Source
$dir = "C:\Users\binwa\OneDrive\Documents\Code\Gurbanify"
$action = New-ScheduledTaskAction -Execute $py -Argument "tools/backup.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 3am
Register-ScheduledTask -TaskName "Shabad Library backup" -Action $action -Trigger $trigger -Description "Nightly backup of shabads.db"
```

Check it: `Get-ScheduledTask -TaskName "Shabad Library backup"`
Run it now: `Start-ScheduledTask -TaskName "Shabad Library backup"`

---

## Adding shabads

**Normally: in the app.** Add shabad → type first letters → pick → save.

**In bulk from a Notion export:** needs Docker running (see below).

```bash
python tools/import_shabads.py --csv "path/to/export.csv" --limit 20     # dry run first
python tools/import_shabads.py --csv "path/to/export.csv" --limit 0 --write
```

- Hand it the **whole** export — shabads already in your library are skipped automatically. No need to strip out ones you already have
- `--limit 0` means everything; any other number caps it
- Without `--write` it's a dry run and saves nothing
- Anything it can't match is written to `import_failures.csv`, never guessed

If a line fails, add a row to `manual_matches.csv`:

| column | use |
|---|---|
| `SourceLine` | the line as it appears in your csv |
| `Ang` | when the line is in more than one shabad — picks by ang |
| `VerseID` | when BaniDB spells or splits it differently — points at the exact verse |

---

## Docker / BaniDB (only for imports and corpus rebuilds)

```bash
docker start banidb-api                      # start it
docker stop banidb-api                       # stop it when done
./tools/start_local_banidb.sh                      # first run / if the container is gone
```

Rebuild the search corpus (only if BaniDB updates, or `banidb.db` is lost):

```bash
docker start banidb-api
python tools/extract_corpus.py                     # ~4 seconds, 91 MB, 142,428 verses
```

`banidb.db` is fully regenerable — never back it up.

---

## Searching

Two search boxes, and they do different things:

| where | searches |
|---|---|
| **Library** view | only your own shabads |
| **Add shabad** view | all 142,428 verses in BaniDB |

If the library finds nothing, it offers a button to search BaniDB instead.

### First Letter Gurbani

**Case-sensitive only when you use capitals.** In the Gurmukhi encoding `B` is ਭ and `b` is ਬ:

- `gkbbj` — forgiving, matches ਬ and ਭ alike
- `gkBBj` — precise, only ਭ

Type lowercase if you're unsure; capitals only narrow.

### English Translation

**Your words are keywords, not a phrase.** `love name` finds *"Perfect is the Love of the Lord's Name."* — both words present, order and position irrelevant.

- **All the words must land in one line**, not scattered across the shabad. That isn't a limitation, it's the point: "love" and "name" appear *somewhere* in 128 of your shabads but together in a line in only 31. Allowing scattered matches would return a third of the library for any two common words
- Each word matches as a substring, so `name` also finds "names" and `love` finds "beloved" — but not across a stem change, so `love` misses "loving"
- **`"quoted words"` search as one exact phrase**, which is what the box used to do for everything
- Notes are searched too, under the same rule — all the keywords in the notes, or all in one line. Half in each is not a match
- `%` and `_` are literal, so `100%` searches for what it looks like

Searches every line of every shabad, not just the one you saved it by.

---

## The swipe deck

**Deck** tab. Drag a card, tap the round buttons, or press **←** / **→**.

- **Left** = pass. **Right** = add to **Interested**
- The badge on the *Interested* tab is the current count
- **Clear folder** empties it in one go. It only deletes shortlist rows — shabads, tags and notes are untouched

**The order is random.** There was recency weighting here once — surface what you
haven't seen in a while — but it was removed as unnecessary. `last_surfaced` is
still recorded on every swipe, so turning it back on later needs no new data.

- Anything already in Interested is kept out — unless you tick **Include Interested**
- Both swipe directions mark the shabad as seen
- **Undo** (↶ or Ctrl+Z) takes back the last swipe, up to 25 deep. It un-shortlists and restores the old timestamp
- **Reshuffle**, or just leaving and returning to the tab, draws a fresh deck

`last_surfaced` is written by the app and never needs touching. It's in the backup, so the deck doesn't lose its memory when you restore.

---

## Memorizing

**Learn** tab. Open a shabad → **Learn** to add it to the list.

Tap any shabad in the list to practise it. Three modes, switch freely:

| mode | what it does |
|---|---|
| **First letters** | shows the English, you type `gkbvv`, checked against the real first letters |
| **Meaning** | shows the Gurmukhi, pick the right English from four |
| **Perform** | the whole shabad as first letters only — tap any line to reveal it |

- **Prev / Next in every mode**, and they wrap, so you can go round as many times as you like
- **Nothing is scheduled, graded or recorded.** No due dates, no levels, no streaks
- Meaning distractors come from the most *similar* lines, so they're near-misses rather than obviously wrong
- The list shows when you last practised each one — information, not a deadline

There was a full spaced-repetition system here (SM-2, six levels, meaning gates,
daily caps). It worked and went completely unused, so it was removed. If you
ever want scheduling back, it's in git history at `bf27a11`.

---

## Search quality: picking the summariser model

The §6/§7 work. **Done** — `gemini37` and `glm47nt` are both fully indexed
(5,530 lines each), and the Similar page runs the blind comparison between them.
This section is what you'd use to evaluate a *replacement* model.

**One-time setup**

```bash
python -m pip install sentence-transformers   # ~2.5 GB with torch
```

Your OpenRouter key goes in `.env` at the project root (never commit it):

```
OPENROUTER_API_KEY=sk-or-...
```

### The bench

Compares summariser models on topics you group by hand.

```bash
python search/bench.py --list                    # models + what's cached
python search/bench.py --models all              # every registered model
python search/bench.py --models cached           # only ones already paid for — free
python search/bench.py --models gemini37 glm5    # just these two
python search/bench.py --models all --topics search/topics/recall.txt
python search/bench.py --show gemini37           # similarity results, in Gurmukhi
python search/bench.py --new death               # scaffold a new topic file
python search/bench.py --models all --yes        # skip the spend prompt
```

With no `--topics`, it runs **every file in `search/topics/`**.

### Adding a topic set

```bash
python search/bench.py --new death
```

Then paste into `search/topics/death.txt`:

```
# Body is temporary
ਪਹਿਲੀ ਪੰਗਤੀ ॥
ਦੂਜੀ ਪੰਗਤੀ ॥

# Time is short
ਤੀਜੀ ਪੰਗਤੀ ॥
```

- `# Heading` starts a topic; every line under it belongs to it
- Full Gurmukhi lines. Looked up in your library first, then all of SGGS
- `//` for comments. Anything it can't find is named, never silently dropped

**What makes a set worth having**

- **Recall from memory. Don't find lines by searching.** A searched set shares vocabulary, every model scores well, and it proves nothing
- 3–4 topics, 4–8 lines each, topics far enough apart that confusing them would annoy you
- Different wording within a topic — that's the entire point

**The bench detects a weak set automatically.** If plain English translations
already score ≥75% on it, it's flagged `WEAK TEST` and dropped from the overall
ranking. `topics/images.txt` is exactly this — 94% baseline, useless for
comparison. `topics/recall.txt` is the honest one at 52%.

### Reading the result

The score is **R-precision**: of a line's nearest neighbours, how many come from
the topic you put it in. Each set is scored against a **baseline** — the same
lines embedded from their plain English translation, no model involved.

- **Beat the baseline** → the summaries are adding meaning
- **At or below it** → that model isn't earning its cost
- A gap under ~5% between two models is **noise** at this size; the tool says so

### What it costs

- Summaries are **cached per model+line**. Re-running a model is free; adding one
  model to a comparison only pays for that one
- Vectors are cached too, so re-scoring is instant
- It estimates the spend and asks first
- After each run it writes the **measured** tokens/line back into
  `search/models.json`, so the next estimate is real rather than a guess
- Rough guide: **one 20-line topic set across all 11 models ≈ $0.20**

### The other scripts

```bash
python search/embed.py --stats                 # how much of the library is indexed
python search/embed.py --field summary         # write vectors into shabads.db
python search/test_clustering.py               # the original standalone §15 check
```

(`search/similar.py` is gone — the Similar page in the app replaced it.)

BGE-M3 loads only while a script runs (~3 GB RAM, ~30s) and **never in the web
app** — the app only ever reads vectors already in the database.

---

## Indexing the library

This is what turns the red badges green and makes the Similar page work.

```bash
python search/index_library.py --estimate              # cost, spends nothing
python search/index_library.py --verify                # one call, checks the endpoint
python search/index_library.py --model gemini37 --limit 200   # a small slice first
python search/index_library.py --model gemini37        # the whole library
python search/index_library.py --embed-only            # vectors for existing summaries
```

With no `--model` it does every model switched on in Settings.

**Stop it whenever.** Ctrl-C, a reboot, a dead network — it writes to the
database every 25 lines and only ever asks for lines that have no summary yet,
so running it again carries on from where it stopped. Nothing already paid for
is ever re-bought.

Progress looks like this:

```
  gemini37    1204/4987  ███████░░░░░░░░░░░░░░░  24%   162/min  eta 23m18s  $1.09
```

**Two phases.** Summarising calls OpenRouter and costs money; embedding runs
BGE-M3 locally and is free. `--summarise-only` and `--embed-only` split them.

**Duplicate tuks are summarised once.** 306 of the 5,530 lines appear in more
than one shabad; the same line means the same thing wherever it sits, so the
summary is written to every line that shares it. About 6% off the bill.

**`--batch` prices the work but cannot run it.** OpenRouter serves `:batch`
models only through its async `/api/beta/batches` endpoint, which is
submit-a-job-and-poll rather than a normal request. Worth about $2.30 on a full
run if that path is ever implemented.

**If the prompt in `summarize.py` changes**, `prompt_ver` changes with it and
every line counts as unindexed again. That is deliberate (§6) — but it means
editing that prompt costs a full re-run, so settle it on the bench first.

---

## When something breaks

**Anything wrong with the server or the tunnel — double-click `restart.bat`.**
It handles the port, the stale processes, the tunnel and the link. The commands
below are what it does, for when you want to do it by hand.

**"address already in use" / server won't start** — a previous server is still holding the port:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select -Expand OwningProcess -Unique | ForEach { Stop-Process -Id $_ -Force }
```

**The link in my phone stopped working** — the tunnel restarted and got a new
hostname. If the watchdog is running it has already pushed the new one; check
ntfy. Otherwise run `restart.bat`.

**No notification arrived** — check `NTFY_TOPIC` in `.env` matches the topic you
subscribed to, exactly. Test it with:

```powershell
Invoke-RestMethod -Uri "https://ntfy.sh/<your-topic>" -Method Post -Body "test"
```

**It asks for a password and I don't know it** — it's `APP_PASSWORD` in `.env`.
Change it there and restart; browsers will re-prompt.

**Is the watchdog actually running?**

```powershell
Get-ScheduledTask -TaskName GurbanifyWatchdog
Get-Content logs\watchdog.log -Tail 20
```

Don't use `uvicorn --reload` — its workers outlive the parent and keep serving stale code.

**Changes to `api.py` not showing** — restart the server. Changes to `app.js` / `style.css` / `index.html` just need a hard refresh (Ctrl+Shift+R).

**"Can't reach BaniDB MySQL"** — `docker start banidb-api`, wait ~10s.

**Restore from a backup:**
```bash
copy backups\shabads-<stamp>.db shabads.db
```

---

## The files

Everything sits in one flat folder on purpose — the scripts find each other and
the databases by sitting beside them. Grouped by job:

**The app** — what runs day to day
| file | |
|---|---|
| `api.py` | the whole server: library, deck, shortlist, history, learning |
| `static/` | `index.html`, `app.js`, `style.css` — the entire frontend |
| `schema.sql` | table definitions for a fresh database |

**The data**
| file | |
|---|---|
| `shabads.db` | **your library. The irreplaceable one.** Back this up |
| `banidb.db` | all 142,428 verses, for search. 91 MB, **regenerable — never back up** |
| `manual_matches.csv` | your decisions on lines the importer couldn't match. **Yours** |
| `notion-export.csv` | the original Notion export the library was built from |
| `backups/` | `.db` + `.csv` + `-learning.csv` per run |

**Getting shabads in** — only needed for bulk imports
| file | |
|---|---|
| `tools/import_shabads.py` | Notion csv → library |
| `extract_corpus.py` | Docker MySQL → `banidb.db` |
| `start_local_banidb.sh` | first-time Docker setup |
| `tools/backup.py` | snapshot + Notion-ready csv + learning progress csv |

**Search quality** — the §6/§7 work, not finished
| file | |
|---|---|
| `search/bench.py` | **the model comparison tool** — start here |
| `search/models.json` | short names → OpenRouter ids, with measured tokens/line |
| `search/topics/*.txt` | your hand-grouped topics. **Your judgement, not regenerable** |
| `search/cache/` | summaries already paid for, plus vectors. Rebuilding costs money |
| `search/summarize.py` | LLM → an English summary per line, via OpenRouter |
| `search/index_library.py` | **the full-library indexing pass** — resumable, per model |
| `search/embed.py` | BGE-M3 → vectors in the database |
| `search/test_clustering.py` | standalone §15 check; bench reuses its scoring |
| `.env` | OpenRouter key, app password, ntfy topic. **Never commit this** |

**Serving it** — app + tunnel + staying alive
| file | |
|---|---|
| `restart.bat` | double-click: stop everything, start fresh, get a working link |
| `tools/serve.ps1` | the actual logic behind it |
| `tools/watchdog.ps1` | runs forever, restarts what dies, pushes the new link |
| `tools/register-task.ps1` | installs the watchdog to run at boot |
| `tools/loadtest.js` | do the frontend scripts load? catches what `node --check` can't |
| `logs/` | server, tunnel and watchdog logs, plus the current url. Gitignored |

**Docs**
| file | |
|---|---|
| `CLAUDE.md` | every design decision and why |
| `README.md` | this — the commands |

---

## Two gotchas worth remembering

**Notion exports use non-breaking spaces** (U+00A0) between words, not normal ones. They look identical and break exact matching. `tools/import_shabads.py` normalises them — if you ever write your own script against a Notion export, it must too.

**Gurbani text always comes from BaniDB, never from a model** (CLAUDE.md §12). Where your csv and BaniDB disagree on spelling, BaniDB wins and gets stored. Your original wording stays in the csv.
