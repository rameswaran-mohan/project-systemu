# A refusal is no longer a dead end (reclassify, scoring core)

The action gate's strictest tier is a **refusal**, not a prompt: it fires when systemu
cannot tell what a tool call will do *and* something about the call looks high-severity.
That's the right default, but a false positive left the operator stuck with no way
forward — the card could only be denied.

The remedy is reclassification: the operator states what the effect actually is, and the
gate **re-decides from the raw signals**. This lands the scoring half of that. Nothing
sets the field yet — the card and its typed confirmation are the next slice — so there is
no behaviour change for any existing call.

## The rule that makes it safe

Reclassification defeats exactly one thing: "we couldn't classify it." It cannot clear
anything the gate worked out for itself.

- **Destructive parameters and irreversibility are facts about the call**, not about the
  label, so they survive. The action becomes an honest approval card rather than a
  refusal — it does not become silent.
- **The operator's class is additive, never a replacement.** Telling the gate that a
  wire-transfer tool is "just a local read" does not strip the money escalation the name
  independently implies.
- **A reclassified action never becomes frictionless.** It was refused once; it is now
  approvable, which means the operator approves the *new* classification on a card.
  It can never fall through to running unprompted.
- **An unrecognised class is not a reclassification.** A value that names no real effect
  classifies nothing, so it leaves the call exactly as unclassifiable as it was.

## What review changed

The first version of this was **safe but useless** — an adversarial sweep of ~715,000
contexts proved it could never reach ALLOW *and* could never actually remedy anything:
every refusal stayed a refusal under every possible reclassification. The operator would
have completed a typed confirmation and landed precisely where they started, which is the
dead end this feature exists to remove. Two of the tests also passed for the wrong
reasons — one asserted a *regression* as though it were the fix, and another rested on a
false claim about how the refusal tier is reached.

Now the reclassified call re-enters the normal ladder, so refusals genuinely become
approvable, with only the never-frictionless floor kept on top. A sweep pins all four
properties — never ALLOW, only-the-remedy softens, junk changes nothing, and the remedy
is demonstrably *not* inert — because each individual case can pass for the wrong reason.

Also hardened: an unrecognised class used to silently strip "unclassifiable" and put
nothing in its place (harmless under the first design, but it would have become a
straight bypass under this one), and the security context now rejects unknown fields
outright — a mistyped field name previously scored the call as though the signal had
never been supplied.
