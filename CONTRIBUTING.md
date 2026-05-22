# Contributing to Systemu / Sharing-On

Thanks for taking the time.  This project welcomes contributions from
humans **and** AI agents — see the dedicated section below for
expectations on each side.

---

## TL;DR

```bash
# 1. Fork, then clone your fork
git clone https://github.com/<you>/project-systemu.git
cd project-systemu

# 2. Install for development
./install.sh --mode local --non-interactive \
    --openrouter-key=<your-or-key> --google-key=<your-google-key>
.venv/Scripts/activate   # or  source .venv/bin/activate

# 3. Branch, code, test
git checkout -b feat/short-descriptive-name
pytest tests/ -q

# 4. Commit with Conventional Commits + push
git commit -m "feat(runtime): describe what changed and why"
git push -u origin feat/short-descriptive-name

# 5. Open a PR — fill out the template, link related issues
```

---

## Project layout

A complete tour is in [`ARCHITECTURE.md`](ARCHITECTURE.md).  The
high-bullet view:

| Path | What lives here |
|---|---|
| `sharing_on/` | Capture engine (recorder + LLM analyser + CLI) |
| `systemu/runtime/` | Supervisor + ShadowRuntime + tool sandbox |
| `systemu/pipelines/` | Scroll refinery, activity extractor, tool forge, memory consolidator |
| `systemu/interface/` | NiceGUI dashboard + REST endpoints |
| `systemu/queue/` | In-process priority queue + Huey app |
| `systemu/storage/` | SQLite / Postgres vault backends |
| `systemu/vault/` | Starter tools, shadows, skills, scrolls (JSON + Python) |
| `tests/` | Pytest suite |
| `alembic/` | DB migrations |
| `docs/` | Architecture notes, smoke + e2e results, this guide |

---

## Code style

| Concern | Tool / convention |
|---|---|
| Python formatting | `black` (88 cols, default settings) |
| Linting | `ruff` |
| Type hints | Encouraged on new public APIs; not enforced repo-wide yet |
| Docstrings | Google style on public modules / classes / functions |
| Commit messages | [Conventional Commits](https://www.conventionalcommits.org/) |
| Branch names | `feat/...`, `fix/...`, `docs/...`, `chore/...` |

```bash
# Format + lint (run before committing)
ruff check . --fix
black .
```

### Commit message format

```
<type>(<optional-scope>): <short summary, imperative mood>

<optional body — what changed, why, edge cases, refs>

<optional footer — Co-Authored-By, Closes #N>
```

Common types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`,
`perf`, `build`, `ci`.

Examples:

```
feat(runtime): tolerate alias keys in tool-call parameters

fix(queue): re-raise strict-mode error in docker-enterprise

docs(readme): document per-mode resource minimums
```

---

## Tests

Every functional change needs a test that **would have failed before
the change**.  Run the suite locally before opening a PR:

```bash
pytest tests/ -q
```

The full suite takes ~30 s on a modern laptop.  CI runs the same suite
plus an integration tier against real Postgres + Redis — see
the live workflow at [`.github/workflows/test.yml`](.github/workflows/test.yml).

When you add a new tool, scroll, or skill: prefer adding a focused
unit test plus, where it makes sense, an end-to-end test that runs the
whole pipeline against your new artefact.

### Real-LLM tests

Most tests mock the LLM router.  The `tests/e2e/` tier contains a
few tests that hit real Postgres + Redis but still stub the LLM —
they verify queue + storage behaviour, not model behaviour.  Tests
that talk to a real LLM live behind the `@pytest.mark.real_llm`
marker and are off by default; gate them on an env var
(`SYSTEMU_RUN_REAL_LLM=1`) so contributors without an API key still
get a green local run.

---

## Documentation

If your change touches user-facing behaviour, update:

* `README.md` — the headline summary
* `docs/user-guide.md` — operator-level guidance
* `docs/getting-started.md` — first-run walkthrough
* `ARCHITECTURE.md` — only when the system architecture changes
* `CHANGELOG.md` — add an entry under the `[Unreleased]` section
  using the Keep-a-Changelog format

Markdown is linted on CI; basic checks (`markdownlint`) catch broken
links and inconsistent heading levels.

---

## Pull request process

1. **Open an issue first** for non-trivial changes — design discussion
   is cheaper before code than during review.
2. **Keep PRs focused.**  One concern per PR.  Refactors that are
   incidental to a fix go in a separate PR.
3. **Fill out the PR template** — it lists the summary, test plan, and
   risk assessment maintainers need to review.
4. **Pass CI** — the workflow runs unit + integration + compose-render
   suites.  CI red blocks merge.
5. **Wait for a review.**  At least one maintainer approval is required
   before merge.  Maintainers may request changes; respond by pushing
   new commits to the same branch.
6. **Squash on merge** — the merged history keeps one commit per PR
   with the PR title + body as the message.

---

## For AI contributors

Systemu is built by humans **with** AI assistance, and we welcome PRs
authored by AI agents under three principles:

### 1. Transparency

AI-authored commits MUST include a `Co-Authored-By:` trailer
identifying the agent and the human operator who reviewed the work.
Example:

```
Co-Authored-By: <Agent name + vendor> <noreply@<vendor>.example>
Co-Authored-By: <Operator name> <op@example.com>
```

Multiple `Co-Authored-By` lines are fine.  The human operator's line
is the one we hold accountable for merging — it's the same expectation
as any other maintainer review.

### 2. Human review gate

Every AI-authored PR requires explicit approval from a maintainer.
The maintainer attests they reviewed the diff for correctness, scope,
and security.  Auto-merge bots **are not allowed** to skip this gate.

### 3. Same quality bar as humans

AI PRs are held to the same standards:

* Conventional Commits.
* Tests for new behaviour.
* Docs updated for user-visible changes.
* No tautological tests (a test that just re-implements the function
  it's testing is worse than no test).
* No dead code, no commented-out blocks, no unused imports.

We rebase or reject PRs — human or AI — that ignore project
conventions, generate dead code, or duplicate existing utilities.

### Guardrails on the AI side

Things AI contributors **must not do**, even if asked:

* Modify `SECURITY.md`, `CODE_OF_CONDUCT.md`, or `LICENSE` without
  explicit human sign-off.
* Create maintainer accounts or push directly to `main`.
* Self-approve or merge their own PRs.
* Disable hooks, signing, or required checks (`--no-verify`,
  `--no-gpg-sign`, etc.) without an explicit operator instruction.
* Embed credentials in code or documentation.

### What works well for AI

The issue tracker tags some issues with `good-for-ai` — these have
sharp scope, clear success criteria, and minimal cross-file
coordination:

* Bug fixes with a failing test already attached
* Doc updates with a specific style
* Test gap fills for a specific module
* Mechanical refactors (rename, extract method, etc.)
* Boilerplate scaffolding (issue templates, config files)

Things to avoid in an unscoped session:

* Cross-cutting refactors without a design discussion first
* Performance work without measurements
* Adding new dependencies
* Changes that touch the security model or the threat boundary
  (queue, sandbox, allowlists)

---

## Reviewer checklist

Maintainers verify the following before merging:

* [ ] PR template fields are filled (summary, test plan, risk)
* [ ] CI is green (or each failure is explained in the PR description)
* [ ] New behaviour has a test
* [ ] User-facing changes update the relevant docs section
* [ ] No secrets, tokens, or PII in the diff
* [ ] Migration / deprecation notes added to `MIGRATION.md` if a
      public surface changed
* [ ] `CHANGELOG.md` updated under `[Unreleased]`
* [ ] Conventional Commit message
* [ ] For AI PRs: `Co-Authored-By` trailer + human reviewer attestation

---

## Reporting bugs / requesting features

* Use the corresponding [issue template](./.github/ISSUE_TEMPLATE/).
* For security issues, **do not** open a public issue — follow
  [`SECURITY.md`](SECURITY.md).

---

## Governance

The project is currently maintained by the original author and a
small set of trusted contributors.  Decisions are made by rough
consensus on the issue tracker; the project maintainer has final say
in unresolved disagreements.

If you'd like to take on a larger ongoing responsibility (release
management, security response, a specific subsystem), open an issue
proposing it.

---

## For human contributors

### Quick start (4 steps)

1. Fork + clone: `git clone git@github.com:<you>/project-systemu.git`
2. Install dev deps: `pip install -e ".[dev]"`  (or `pip install -e .` if the `[dev]` extra isn't yet defined)
3. Run tests: `pytest tests/ --ignore=tests/e2e`
4. Open a draft PR early — we'll iterate together.

### Picking a first issue

Look for the `good first issue` label.  Each issue includes:
- **Scope** — what to change and where (file paths)
- **Acceptance criteria** — how we know it's done
- **Hints** — suggested approach + gotchas

If something is unclear, comment on the issue.  Don't worry about
asking — every maintainer was a first-time contributor once.

### Earning commit access

We grow the maintainer pool from regular contributors:

- **3 merged PRs** → `triage` role (close stale issues, label PRs, add reviewers)
- **10 merged PRs + 3 months active** → `maintain` role (merge PRs, manage milestones, edit Discussions)
- Tracked in `CODEOWNERS` — see `docs/governance.md` for the contract.

### Conventional commits

Use the prefix style already in this repo:

- `v0.X-y: <change>` for feature commits during an active version's sub-releases
- `fix: <change>` for bug fixes that don't belong to a sub-release
- `docs: <change>` for docs-only edits
- `chore: <change>` for tooling / dependency bumps

### Where to get help

- **GitHub Discussions** — Q&A, ideas, show & tell.  See the templates under `.github/DISCUSSION_TEMPLATE/`.
- **Issue comments** — for issue-specific questions.
- **Draft PRs** — open early; reviewers will jump in.
