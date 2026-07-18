# A refused action can now be resolved (reclassify, end to end)

The action gate's strictest tier is a **refusal**, not a prompt: it fires when systemu
cannot tell what a tool call will do *and* something about the call looks high-severity.
A prior fix correctly ensured no stored approval could ever satisfy that tier — which
left the operator with no way forward at all. The only real resolution was to fail the
task.

This completes the remedy. A refusal card now offers **Reclassify effect…**: you state
what the effect actually is, under typed confirmation, and the gate re-decides from the
underlying signals. If it still needs approval, you get an honest approval card *for the
classification you assigned*, and approving that runs the call exactly once.

## What keeps it honest

Saying what something is defeats exactly one thing — "we could not tell". It never
clears anything the gate worked out for itself:

- **Destructive arguments and irreversibility survive**, because they are facts about the
  call rather than the label. Reclassifying turns a refusal into an approval card; it
  never turns one into a silent run.
- **The assignment is single-use, and bound to the call it was made for** — the exact
  tool, the exact classification, and a fingerprint of the exact arguments. A different
  destructive call cannot ride it, which matters because the card cannot show you every
  difference between two calls to the same tool.
- **It expires.** An abandoned assignment does not sit indefinitely waiting to be spent.
- **A value that names no real effect is not a classification**, so it leaves the call
  exactly as unclassifiable as it was.
- The refusal card no longer offers "Approve once" at all — at that tier it did nothing,
  and offering it was actively harmful (see below).

## What review found

Three rounds, each finding a way the remedy could be short-circuited. The pattern is
worth recording, because none of these were visible in a passing test suite.

**A button that did nothing turned out to do something.** "Approve once" on a refusal was
a documented no-op *at the gate* — but the code recording approvals didn't know about
tiers, so it quietly wrote a single-use token anyway. Harmless while nothing could lift a
refusal. Reclassification lifts exactly those calls, which made that dormant token
redeemable: click the button that does nothing, then legitimately reclassify, and the
call ran with **no card ever shown**.

**Then the same end state via a different channel.** Resolved decisions are never
retired, so an ordinary approval of an unrelated call left a row that a later reclassify
could cash — the destructive call running while its follow-up card sat untouched.

**Then the question changed** from "is this hole closed" to "how many channels are
there". The answer, derived from the code rather than from anyone's summary: the gate has
exactly five paths that return without asking, and a refused call reaches only one of
them. Every remaining path is now bound to the specific card, classification and
arguments it was granted for.

Also closed: a verdict that can never occur today sat in a position where, if it ever
occurred, it would have run the call ungated.

## Honest residuals

The typed confirmation is enforced where the record is written rather than only in the
UI, but it remains a flag the caller asserts — it is defence against surface drift, not
against something that can already write to the vault. Assignments expire after thirty
minutes, so approving a follow-up card much later gets a fresh refusal (fail-closed, but
it reads as "I approved and it refused"). Tools whose arguments change every call — a
timestamp or nonce — can never redeem the remedy, since the fingerprint won't match.

Separately, verification confirmed two **pre-existing** gaps unrelated to this work: the
dry-run path and the built-in registry path do not enter this gate at all. Neither can
cash a reclassification. Both are being tracked separately.
