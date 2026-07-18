# The shipped tools are no longer labelled as agent-written

systemu ships ~39 tools in its own vault. Every one of them was flagged
`forged_by_systemu` — the flag that means *an LLM wrote this body*. They are repo
code. Nobody's model wrote them.

That mislabel was not cosmetic. Four shipped tools were **hard-denied in a stock
install**: `fetch_html`, `fetch_json`, `api_call_get`, `download_file`. The network
deny exists to stop LLM-authored code from reaching the internet, and it was firing
on our own source. Anything the agent tried to fetch with a built-in tool refused.
Verified at both commits against the real tool bodies — denied before, allowed after,
exactly those four.

## What changed

The 39 bodies and their index headers now say what is true. Existing vaults are
repaired on boot.

## What the repair will and will not do

The repair clears the flag **only when your copy of the tool body hashes
byte-identical to the one in the installed package.** A body you edited, or one a
forge overwrote, keeps `forged=true` and stays denied and gated. That is the point:
the flag is a provenance claim, and the only honest way to make it is to check.

It refuses in three more cases, each fail-closed:

- **Two tools share a name.** The vault indexes by id, so a name can legitimately
  appear twice. When it does, the repair cannot tell which record it just hashed, so
  it clears neither. Before this refusal existed, a second tool named `fetch_html`
  could inherit a clean flag proven against a file that was not its own — and a
  later re-forge would then put model-written code behind it.
- **The body points somewhere else.** The repair hashes the implementation path the
  body actually declares, not the one its name implies, and refuses any path that
  resolves outside the vault's `implementations/` directory.
- **We cannot find both files.** No proof, no change.

## Two limits worth knowing

**SQLite-backed vaults are not repaired.** The repair reads the file-vault tool
index; a SQLite vault has none, so it cleanly does nothing and those installs keep
the four denied network tools. Re-installing into a fresh file vault is the current
workaround. This is a pre-existing gap shared by the whole seed-migration path, not
something this change introduced — but it does mean the fix does not reach every
install shape.

**A vault the repair declines will card more shell commands than before.** A
`run_command` body that no longer matches the package is treated as forged, which
means read-only calls like `git status` prompt for approval alongside the dangerous
ones. It fails safe, and it is exactly the population we refuse to vouch for — but
it is more friction than a stock install, and it is the same friction an earlier
audit paid to remove.

## Also true now

A seed no longer reports itself as `forgeable` to the planner. That field means
"self-forged and not operator-declined", so a shipped tool claiming it was always
wrong. Nothing branches on the field; the model's picture of its own toolset simply
stops overstating what it wrote.
