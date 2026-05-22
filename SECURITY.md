# Security Policy

## Supported versions

The project is pre-1.0.  Only the latest tagged release receives
security fixes.  If you're running an older version, upgrading is the
recommended first step before reporting an issue.

## Reporting a vulnerability

**Please do not report security issues through public GitHub
issues, pull requests, or discussions.**

Instead, open a [private security advisory](https://github.com/rameswaran-mohan/project-systemu/security/advisories/new)
on the repository.  GitHub will keep the report confidential until a
fix is ready.

Include in your report:

- A description of the issue and its impact
- Steps to reproduce, or a minimal proof of concept
- The version / commit you observed it on
- Your suggested fix, if you have one

We aim to acknowledge new reports within **3 business days** and
provide a status update within **10 business days**.  Coordinated
disclosure timelines are negotiated case by case — typical embargo is
30-90 days depending on severity and complexity.

## What's in scope

In scope:

- Authentication / authorisation gaps in the dashboard or REST API
- Tool sandbox escapes (a Shadow-generated tool gaining access it
  shouldn't have)
- Prompt-injection bypasses that cause unsafe actions (e.g. a Scroll
  containing instructions that bypass approval gates)
- Insecure handling of secrets in `.env`, vault, or capture sessions
- Path traversal, deserialisation, command injection in any pipeline
- Container image issues (root user, leaked secrets, vulnerable base
  layers)
- Migration / vault corruption bugs that could lose user data

Out of scope:

- Issues that require physical access to the host
- Self-XSS in operator-only dashboards (no other party can trigger it)
- Vulnerabilities in third-party services we depend on (report those
  upstream)
- Findings from automated scanners without a working proof of concept
- Denial of service requiring substantially more resources than the
  attacker — focus on amplification and authentication-bypass DoS

## Threat model

The project assumes a **single-operator** deployment by default.  The
dashboard is unauthenticated and bound to `localhost`; opening it to
a network requires the operator to put it behind their own auth
layer (reverse proxy, VPN, SSO).  We treat any unauthenticated
exposure outside `localhost` as **operator misconfiguration**, not a
project vulnerability — but we welcome reports of issues that would
amplify the blast radius of such a misconfiguration.

LLM outputs (Scrolls, tool specs, decisions) are treated as
**untrusted data**.  Every code-generating pipeline (Tool Forge,
Skill Forge, Evolution Engine) routes through an approval gate
before any artefact is enabled.  Bypasses of those gates are
in-scope security issues.

## Disclosure

We follow coordinated disclosure.  Once a fix is released:

1. We publish a GitHub Security Advisory describing the issue, the
   affected versions, and the upgrade path.
2. The advisory credits the reporter unless they request otherwise.
3. The `CHANGELOG.md` entry references the advisory ID.

If you find an issue and would prefer to publish it yourself after
the embargo, let us know in your initial report.
