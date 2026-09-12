# Moving machines, and making changes later

The two procedures I always forget. Nothing else in this file.

- [First move: laptop → home PC](#first-move-laptop--home-pc)
- [Making a change later](#making-a-change-later)
- [The one rule](#the-one-rule)

---

## What runs where

**Everything runs on the home PC.** The app, the tunnel, the watchdog, the
OpenRouter calls that write summaries, and the BGE-M3 embedding model that turns
them into vectors. It is always on, always on wifi, and never sleeps — which is
the whole reason for moving.

The laptop keeps nothing but the code, for editing.

---

## First move: laptop → home PC

### 1. On the laptop — take a fresh backup and push

```powershell
cd C:\Users\binwa\OneDrive\Documents\Code\Gurbanify
python tools\backup.py
git status          # should be clean; if not, commit and push first
```

### 2. Carry three files across

USB stick, OneDrive, whatever. **These are the only things git does not have:**

| file | what it is |
|---|---|
| `shabads.db` | the library, accounts, summaries, vectors. **Irreplaceable.** |
| `banidb.db` | the Gurbani corpus. Big but rebuildable. |
| `.env` | OpenRouter key, ntfy topic |

Accounts live *inside* `shabads.db`, so both logins work the moment it lands.
Nothing to recreate.

### 3. On the home PC — install Python and Git, then clone

**Two things have to be installed by hand**, because they are what everything
else gets installed *with*. `tools/setup.ps1` does all the rest, but it cannot
install the tool used to fetch it.

**Python 3.11 or newer** from [python.org](https://www.python.org/downloads/).
**Tick "Add python.exe to PATH"** on the first screen — almost every setup
problem traces back to that box.

**Git**, which is both how the code gets here and how every later change
arrives:

```powershell
winget install --id Git.Git -e
```

**Then close the terminal and open a new one.** An installer only puts itself on
PATH for *new* windows, so in the window you installed from, `git` is still
"not recognised" — which looks exactly like the install having failed.

Now:

```powershell
cd C:\wherever\you\want
git clone https://github.com/singhb17/Gurbanify.git
cd Gurbanify
```

> **Do not download the ZIP from GitHub instead.** It works once and then traps
> you: a ZIP is not a repo, so `git pull` never works, and every future update
> means downloading and unpacking the whole thing by hand.

Now **put the three files from step 2 into this folder**, then:

```powershell
powershell -ExecutionPolicy Bypass -File tools\setup.ps1
```

That does the rest: a virtual environment, every Python package including the
embedding model's dependencies, cloudflared via winget, and a check that the
databases and `.env` are all present. **It takes a while** — torch alone is
about 2.5 GB.

It prints a tick or a cross for every step, and refuses to say "Ready" until
they all pass. Safe to run again if something needed fixing.

### 4. Check it locally before exposing it

```powershell
powershell -ExecutionPolicy Bypass -File tools\serve.ps1 -NoTunnel
```

Open `http://localhost:8000`, sign in, click around. **If this works, the hard
part is done.**

Ctrl+C to stop it.

### 5. Go live

```powershell
.\restart.bat
```

The link appears on screen, on your clipboard, and on your phone via ntfy.

### 6. Make it survive reboots

In an **Administrator** PowerShell:

```powershell
cd C:\wherever\Gurbanify
powershell -ExecutionPolicy Bypass -File tools\register-task.ps1
Start-ScheduledTask -TaskName GurbanifyWatchdog
```

Now a power cut or a Windows Update reboot brings everything back on its own,
with nobody logged in.

### 7. Retire the laptop's copy

**Rename `shabads.db` to `shabads.db.OLD` on the laptop.** See
[the one rule](#the-one-rule).

---

## Making a change later

Two kinds. The only question that matters is **"does the shape of the database
change?"**

### A. Code only — almost always

Anything in `static/`, most of `api.py`, the tools and scripts. No new column,
no new table.

```powershell
# on the HOME PC
cd C:\wherever\Gurbanify
git pull
.\restart.bat
```

**That is all of it.** The database is untouched, so there is nothing to move
and nothing to migrate.

### B. The database shape changes

A new column, a new table, data moved between tables.

**The database still never leaves the home PC.** The change arrives as a
*migration script*, the same way `tools/migrate_multiuser.py` did.

```powershell
# on the HOME PC
cd C:\wherever\Gurbanify
git pull
python tools\backup.py                      # ALWAYS first
python tools\migrate_<name>.py              # dry run -- prints the plan, changes nothing
python tools\migrate_<name>.py --write      # do it
.\restart.bat
```

Every migration script here follows three rules, and any new one must too:

- **Dry run by default** — no flags means it only tells you what it would do
- **Safe to run twice** — each step checks whether it has already happened
- **All or nothing** — one transaction, so a failure leaves the database exactly as it was

`api.py` also refuses to start against a database it knows is un-migrated,
rather than starting and quietly returning nothing.

### If a migration needs testing against real data

Sometimes it is worth trying a migration on a real library before running it on
the only copy that matters.

1. Copy `shabads.db` **to the laptop**. This copy is a **test fixture, not a library**
2. Test the migration against it
3. **Delete it.** Do not open the app against it, do not edit anything in it
4. Push the finished script, run it on the home PC as above

`tools/test_isolation.py` already works exactly this way — it copies the
library to a temp folder, attacks the copy, and throws it away.

---

## THE ONE RULE

**There is exactly one live `shabads.db`, and it is on the home PC.**

Never copy a database from the laptop back to the home PC. The moment you do,
everything done at home in the meantime is gone — every shabad added, every
note, every swipe, every vote. SQLite has no merge, so there is no way to
reconcile two copies; you would simply be choosing which set of edits to lose.

If you ever find yourself wanting to copy one back, the thing you actually want
is a migration script.

---

## When it goes wrong

| | |
|---|---|
| `git` not recognised | either git is not installed (`winget install --id Git.Git -e`) or the window predates the install — open a new terminal |
| `python` not recognised | Python installed without "Add to PATH". Re-run its installer → Modify → tick it |
| `cloudflared` not recognised | close the terminal and open a new one; winget updates PATH only for new shells |
| setup says a database is missing | step 2 — the file has to be in the project folder, beside `api.py` |
| app starts, login fails | the password is in the database, not `.env`. Use the one you set in the app |
| "not been migrated for multiple accounts" | `python tools\migrate_multiuser.py --write` |
| link stopped working | the tunnel restarted. Check ntfy for the new one, or `.\restart.bat` |

Everything else: **[INSTRUCTIONS.md](INSTRUCTIONS.md)**.
