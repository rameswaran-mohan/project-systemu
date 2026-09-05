# Hallway test

A one-page protocol for watching three people use Systemu for the first time.

It is called a hallway test because the classic version is stopping whoever is
walking past and asking them to try the thing. You do not need usability
training and you do not need many people: three sessions is enough to find the
places where a first run stalls, because the same stall tends to repeat.

The point is not to prove the product works. The point is to find out where it
does not. A session where the person got stuck is the useful one.

---

## 1. Before they arrive

**Machine.** A machine that has never run Systemu: a fresh VM, a fresh user
account, or at minimum a brand-new virtual environment in an empty directory.
A machine with a warm vault, a saved API key or a previously answered setup
wizard is not a first run and will not tell you anything about one.

```
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install "systemu[dashboard]"
```

**A model provider.** They cannot get anywhere without one, so decide which
case you are testing and set it up before they sit down, or deliberately leave
it out to test the setup wizard itself. Either is a valid test; pick one and
write down which.

- a hosted provider: put the key in the environment or the `.env` (see the
  README's provider table for the variable names), or
- local Ollama: have `ollama serve` running with at least one model pulled.

**The starting state.** Open a terminal in an empty working directory of their
own, activated venv, nothing else on screen. Do not pre-run `systemu init`.
The first thing they should see is a prompt.

**Give them exactly this, out loud or on a card, and nothing more:**

> Here is a tool called Systemu. The install is done. Get it to do something
> useful for you. Talk out loud while you work - say what you are thinking,
> what you expect to happen, and what surprises you.

The two commands are in the README quick start (`systemu init`, then
`systemu start`). Do not say them out loud. Whether they find them, and how
long it takes, is data.

---

## 2. The rules while they work

These are the whole method. They are harder to follow than they look.

1. **Do not help.** Not a nudge, not a pointed look at the screen, not
   "hmm". If they are stuck, they are stuck, and that is the finding.
2. **Do not explain.** Not what a shadow is, not what the vault is, not why
   a gate appeared. If they need it explained, the product needed to explain
   it.
3. **Do not defend.** When they say something is confusing or bad, write it
   down verbatim. Do not answer it, do not say "well, actually", do not tell
   them what it was supposed to do.
4. **Only two things you may say.** "What are you trying to do right now?"
   and "What did you expect to happen?" Both are questions, neither is a hint.
5. **Do not touch the keyboard or the mouse.** Ever, for any reason.
6. **Let silence run.** Long pauses are the measurement. Count them, do not
   fill them.
7. **Stop at 30 minutes**, or when they say they are done, or when they ask to
   stop. If they hit a wall they genuinely cannot pass, let them sit with it
   for a full minute before you end the session, then end it and record it as
   a hard stop.

Only after you have ended the session may you answer their questions, and
only then do you get to explain anything.

Start a stopwatch when they touch the keyboard. Keep it running.

---

## 3. What to write down

Take notes on paper or in a separate file - not on their screen. Write times
as elapsed minutes:seconds from the stopwatch.

For each session, fill in this template. Copy it verbatim.

```
SESSION: <1, 2 or 3>
DATE:
PERSON: <one line - how technical, do they use a terminal normally,
         any prior exposure to Systemu>
PROVIDER SET UP IN ADVANCE: <yes / no - and which>
OS + install: <e.g. Windows 11, pip install "systemu[dashboard]", 4m10s>

TIME TO FIRST ARTIFACT: <mm:ss, or NEVER>
  first artifact = the first output they could point at and call a result:
  a file in the Outbox, a finished workflow, a completed task. Not the
  dashboard opening. Not a tool being forged. Something they would keep.
  Write NEVER if the session ended without one.

STALLS (every pause over 30 seconds where nothing was happening):
  mm:ss  <where they were>  <how long>  <what they said or did>
  mm:ss  ...
  (one line each; a session with no stalls is a real and reportable result)

FIRST WRONG GUESS:
  mm:ss  <what they believed and what they did because of it>
  e.g. "thought the chat box was a search box", "expected Record to
  screenshot the page", "clicked the tool name expecting it to run"

FIRST UNPROMPTED THING THEY SAID:
  mm:ss  "<verbatim, in their words - do not clean it up>"

DID THEY FIND RECORD UNAIDED? <yes / no / did not look>
  Record lives behind the "+ New" button in the dashboard header, as
  "Record session". Record yes only if they found and opened it without
  a hint, and note mm:ss.

HARD STOPS (anything they could not get past at all):
  mm:ss  <what blocked them>

WHAT THEY THOUGHT IT WAS FOR, in their words, at the end:
  "<verbatim>"

QUOTES worth keeping (positive or negative, verbatim):
  -
  -

ENDED AT: <mm:ss>  REASON: <finished / gave up / time / hard stop>
```

Two notes on filling it in:

- **Verbatim means verbatim.** "This is stupid, where's the go button" is more
  useful than "user could not locate the primary action". Do not summarise
  frustration into neutral language.
- **A blank field is a result.** If they never said anything unprompted, write
  "nothing". Do not invent one to make the sheet look complete.

---

## 4. After all three

Fill this in once, from the three sheets.

```
                              | Session 1 | Session 2 | Session 3
------------------------------|-----------|-----------|----------
Time to first artifact        |           |           |
Number of stalls over 30s     |           |           |
Longest single stall          |           |           |
Found Record unaided          |           |           |
Reached a hard stop           |           |           |
Ended because                 |           |           |

STALLS THAT HAPPENED IN MORE THAN ONE SESSION:
  <place>  <sessions>  <what they were trying to do>

WRONG GUESSES THAT HAPPENED IN MORE THAN ONE SESSION:
  <belief>  <sessions>

THINGS NOBODY FOUND:
  <feature - and whether they needed it>

THE ONE THING you would fix first, and why:
```

The repeated rows are the signal. Something that stalled one person may be
that person; something that stalled two out of three is the product.

Resist writing fixes into this sheet. Record what happened. Deciding what to
change is the next step, not this one.

---

## 5. Handing the results back

Paste the three filled session templates and the summary table into a message
to the assistant, exactly as you wrote them, and say what you want out of it -
for example "turn the repeated stalls into a fix list" or "which of these are
copy problems and which are wiring problems".

Two things that make the results usable:

- **Paste the raw sheets, not a summary.** The verbatim quotes and the exact
  timings are what the analysis runs on. A pre-digested version throws away
  the part that carries the information.
- **Say which parts you already disagree with.** If a stall looks wrong to you
  or you think the person was unusual, say so in the same message rather than
  editing it out of the sheet.

Nothing in this protocol is uploaded anywhere by Systemu. These sheets are
notes you took on paper or in your own file; the product does not collect
them and has no telemetry that would.
