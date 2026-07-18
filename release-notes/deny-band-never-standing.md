# Security: no stored approval can satisfy the DENY band

A real hole in shipped code, found while grounding the next feature rather than by a
test — and the first fix for it was itself incomplete, which is the more useful story.

## What the DENY band is

The action gate's strictest tier. It fires when systemu **cannot classify** what a tool
call will do **and** a high-severity signal is present — the last control between the
agent and an unclassifiable destructive action. The rule is: an "always allow" can never
cover it.

## What was wrong

Two things, and the second is the one that mattered.

**The doorway.** The approval card for a DENY-band call offered "Always allow" like any
other prompt, and choosing it wrote a **standing** approval — because the code persisting
approvals read only *which button the operator pressed*, never the verdict the gate had
already computed and stamped on the decision.

**The wall.** Far worse: the approval-store lookup happens **before** any band check. And
the tool signature is *parameter-independent* (name + body + effect tags), while the DENY
verdict is *parameter-dependent*. So the same tool yields REQUIRE_APPROVAL on harmless
arguments and DENY on destructive ones — **under one signature**.

The consequence needs no adversarial operator and no mistake. Approve a tool once on a
benign call — the legitimate, intended flow — and every later call to it with a
destructive argument evaluates to DENY, finds the stored approval, and **runs ungated,
permanently.**

Closing only the doorway would have left that wide open. Recording-side refusal cannot
express "no stored approval satisfies this band"; only the consumption side can.

## What changed

- **The band check now precedes every short-circuit.** A DENY always posts a card, no
  matter what is stored — standing approval, one-shot bridge, or resolved-dedup bypass.
  This is the actual fix.
- **The recorder** refuses to persist a standing allow for a DENY, and a **missing**
  verdict now fails *closed*. (The remote lane's reader already floored on a missing
  verdict; the two readers had opposite defaults for the same field.)
- **The card** no longer offers the option, and matches the verdict enum as well as its
  string — passing the enum rather than its value would otherwise have silently
  re-enabled it.
- **Tool gates joined the bypass floor**, so a Bypass policy can never auto-grant one, and
  **the inbox rail** no longer one-click approves them (its one-click path picks the last
  option; the render-only set that exists to prevent exactly this was missing `tool:`).
- The coords-less rescue path records **nothing** for a DENY — routing it through the
  recorder would have created a dangling, parameter-independent one-shot, which is the
  precise artifact an earlier release deliberately refused to create.

The fail-closed default (Deny, first option) is unchanged, and the ordinary
require-approval path behaves exactly as before.

## What this is not

This does not give a DENY a legitimate way *forward*. The spec's answer is a reclassify
flow: the operator assigns the effect class under typed confirmation, and the gate
**re-arbitrates from the raw signals** — so reclassifying something independently
high-severity still lands on DENY. That is the next piece of work.

## On the tests

The suite was green *before* this fix and green *after* the incomplete version of it —
neither state was evidence of anything, because nothing drove the gate twice with
different parameters. The regression pins added here were checked by removing the fix and
confirming they fail; two existing fixtures were updated to stamp the verdict that
production always stamps, and the metrics test that documented tool gates as *not*
floored now asserts the opposite.
