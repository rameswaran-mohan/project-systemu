# R-A11 — SSRF hardening: the agent can no longer be steered to fetch internal/metadata hosts

A security fix found by grounding the roadmap against the live code: the web-access
layer that backs `web_read` / `web_search` / `find_places` / `geocode` reached
`urllib.request.urlopen` with **no destination guard at all**. A task (or a
prompt-injected page) could steer the agent to fetch `http://169.254.169.254/…`
(cloud-metadata / IMDS), `http://127.0.0.1:8765/` (its own dashboard), or any
internal `10.x` / `192.168.x` service — the classic confused-deputy SSRF hole.
These are the very tools the burrito tryout used, so the exposure was live.

## The fix

A new **single canonical module `runtime/net_safety.py`** carries the fail-closed
outbound-address logic (lifted verbatim from the adversarially-hardened readback
client): it rejects loopback / private / link-local / **IMDS 169.254.169.254** /
reserved / multicast / unspecified addresses, and the IPv6 tunnel classes whose
*embedded* IPv4 is non-global — **NAT64** (`64:ff9b::/96`), **6to4** (`2002::/16`),
ipv4-mapped, teredo — which a naive `is_global` check misses (they report
`is_global = True`). Resolution is fail-closed: a **mixed** result (one public +
one private address) rejects the whole set, closing the DNS-rebind window; a
resolution failure refuses.

**Every outbound surface** now routes through the gate:
- **v2 stack** (`web_access`, the default): `_http_get`/`_http_post` check the URL
  before `urlopen`; a **guarded opener re-checks every 30x redirect hop** (a public
  URL can no longer 302 you to IMDS — the classic resolve-then-reject bypass); and the
  `render=True` Chromium path is gated in its own right.
- **legacy stack** (`web/fetch_core` httpx + `web/browser_pool`, active under
  `SYSTEMU_WEB_STACK_V2=false`): the httpx fetch pre-checks the URL and follows
  redirects **manually, re-gating each hop** (httpx's own `follow_redirects` would
  chase an internal target); the Playwright render adds an IP-level gate on top of its
  domain policy (which had no IP awareness).

A blocked destination returns the surface's existing error shape and never opens a
socket. One operator escape hatch, `SYSTEMU_ALLOWED_OUTBOUND_HOSTS` (comma-separated,
exact-host match), lives in `net_safety` as the single source for every surface —
empty by default, explicit opt-in only.

## Scope + honesty

- **Behavior change:** by default the agent's web tools can no longer fetch
  private/internal/metadata addresses. That's the intended security posture;
  legitimate internal use is enabled per-host via the escape-hatch env var.
- **DNS-rebind TOCTOU — now CLOSED for the `web_access` path (socket-pin).** The
  gates were *resolve-then-reject* — they validate the host, but the fetch resolved
  again at connect, leaving a narrow window for a rebinding resolver (public on the
  check, private on the fetch). This is now closed for the primary web egress: the
  `web_access` opener dials the **vetted IP literal** from
  `net_safety.resolve_pinned_ip` (a literal never re-resolves), so the address the
  kernel connects to is byte-identical to the address the gate approved — the same
  posture the money-move readback path already uses. TLS SNI + the HTTP `Host` header
  stay the hostname (only the socket creator is swapped), so cert validation is
  unchanged. The operator escape hatch and any configured HTTP(S) **proxy** dial
  as-is (operator infra, may be internal by design; the real target stays
  pre-checked). *Still resolve-then-reject-only:* the legacy `web/fetch_core` httpx
  path and the Chromium render path — they close the gross hole
  (IMDS/localhost/RFC-1918/`file://`) but pinning them is a further follow-up. One
  documented **LOW, operator-scoped** residual on the pin: the proxy exemption is
  scheme-blind, so a direct `https://<proxy-hostname>/` fetch when only an HTTP proxy
  is set dials that one operator-configured host as-is — not attacker-injectable, and
  its precondition (attacker DNS control over the operator's own proxy hostname)
  already MITMs all proxied traffic, so incremental risk is negligible.
- **Adversarially reviewed:** an SSRF-bypass review confirmed the literal-encoding
  (decimal/hex/octal/short/NAT64 literals), URL-parser-mismatch, userinfo/fragment,
  escape-hatch-widening, and fail-open vectors are all **closed**, and drove the
  redirect + legacy-stack hardening above.
- **Dedup deferred (deliberately):** the other surfaces that already carry their own
  hardened SSRF logic (the readback client, `url_safety`, `remote_policy`, the MCP
  precheck) are **not** re-pointed at `net_safety` in this change — their test
  suites mock DNS in their own module namespace, so a behavior-preserving
  delegation needs careful test-target updates. `net_safety` is the canonical
  go-forward module; consolidating those surfaces onto it is a follow-up that keeps
  the money-move readback fail-closed posture byte-identical.

New unit tests pin the fail-closed contract (IMDS/NAT64/6to4/ipv4-mapped/RFC-1918/
mixed-resolution/DNS-failure) and prove the `web_access` seams refuse a blocked
destination **without ever reaching `urlopen`**, still fetch public hosts, and honor
the escape hatch. The socket-pin adds tests proving the connection dials the **vetted
IP literal** (not the hostname), refuses a private-resolving host **before dialing**,
keeps SNI/`Host` = hostname, exempts the operator allowlist + configured proxies, and
maps a connect-time rebind block to the canonical `blocked: … (SSRF guard)` refusal.
The socket-pin was adversarially re-reviewed twice (the core confirmed sound; a proxy
availability regression and a refusal-shape inconsistency it surfaced are both fixed).
The readback adversarial suite (the money-move regression floor) stays untouched and
green.
