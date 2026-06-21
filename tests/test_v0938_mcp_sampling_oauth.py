"""P4 (v0.9.38) — MCP sampling, URL-mode OAuth, remote-transport hardening."""
from __future__ import annotations

import json
import pytest


class TestSamplingCore:
    def test_route_sampling_request_calls_llm_router_and_shapes_result(self, monkeypatch):
        """A sampling-request dict is routed through llm_router.llm_call (systemu's
        own model choice) and shaped back into a CreateMessageResult-style dict.
        NO api key appears anywhere in the request dict the server sent."""
        from systemu.runtime.mcp.sdk import sampling

        seen = {}

        async def fake_llm_call(*, tier, system, user, config, **kw):
            seen["tier"] = tier
            seen["system"] = system
            seen["user"] = user
            return {"content": "hello from the parent model", "model": "stub/model-x",
                    "tier": tier, "input_tokens": 3, "output_tokens": 5, "latency_ms": 1}

        monkeypatch.setattr(sampling, "_llm_call", fake_llm_call)

        req = {
            "systemPrompt": "You are a helper.",
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "say hi"}},
            ],
            "maxTokens": 256,
            "temperature": 0.2,
        }
        result = sampling.route_sampling_request(req, config=object(), tier=2)

        # Routed through llm_router with systemu's tier + the server's prompts.
        assert seen["tier"] == 2
        assert seen["system"] == "You are a helper."
        assert "say hi" in seen["user"]

        # Result shaped like a CreateMessageResult.
        assert result["role"] == "assistant"
        assert result["content"]["type"] == "text"
        assert result["content"]["text"] == "hello from the parent model"
        assert result["model"] == "stub/model-x"
        assert result["stopReason"] == "endTurn"

        # Hard invariant: NO api key / secret anywhere in what we routed.
        blob = json.dumps(req) + json.dumps(seen) + json.dumps(result)
        assert "api_key" not in blob and "sk-" not in blob and "Authorization" not in blob


class TestSamplingGate:
    def test_operator_deny_raises_and_never_calls_llm(self, monkeypatch):
        from systemu.runtime.mcp.sdk import sampling

        called = {"n": 0}

        async def fake_llm_call(**kw):
            called["n"] += 1
            return {"content": "should not happen", "model": "x"}

        monkeypatch.setattr(sampling, "_llm_call", fake_llm_call)

        req = {"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}],
               "maxTokens": 32}
        with pytest.raises(PermissionError):
            sampling.route_sampling_request(req, config=object(), tier=2,
                                            on_gate=lambda summary: False)
        assert called["n"] == 0  # fail-closed: model never invoked on deny

    def test_no_gate_means_allow(self, monkeypatch):
        from systemu.runtime.mcp.sdk import sampling

        async def fake_llm_call(**kw):
            return {"content": "ok", "model": "x"}

        monkeypatch.setattr(sampling, "_llm_call", fake_llm_call)
        req = {"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}]}
        out = sampling.route_sampling_request(req, config=object(), tier=2)
        assert out["content"]["text"] == "ok"


class TestSamplingGateRailAndFloor:
    def test_sampling_is_on_the_bypass_floor(self):
        """H9: BYPASS must STILL ask for a sampling request — `sampling` is a
        floor gate type, and the dial returns 'ask' even under BYPASS."""
        from systemu.interface.command.gate_mode import (
            FLOOR_GATE_TYPES, GateModePolicy, GateMode)
        assert "sampling" in FLOOR_GATE_TYPES
        policy = GateModePolicy(mode=GateMode.BYPASS)
        # Floor pierces BYPASS for sampling.
        assert policy.decide(risk="low", gate_type="sampling") == "ask"

    def test_build_sampling_callback_defaults_to_gate_not_allow(self, monkeypatch):
        """The production on_gate posts a 'sampling' gate (does NOT silently
        allow). We assert enqueue was called with gate_type='sampling' and a
        policy, and that a DENY resolution raises through route_sampling_request."""
        from systemu.runtime import shadow_runtime

        posted = {}

        class _FakeInbox:
            def __init__(self, vault): pass
            def enqueue(self, descriptor, *, gate_type, body="", policy=None,
                        capability="", vault=None, context_extras=None):
                posted["gate_type"] = gate_type
                posted["has_policy"] = policy is not None
                posted["body"] = body
                posted["context_extras"] = context_extras or {}
                # Simulate the operator DENYING (the safe default).
                return "dec_deny"

        # The callback resolves the posted gate's outcome via a resolver hook;
        # stub it to report 'deny' for dec_deny.
        monkeypatch.setattr(shadow_runtime, "InboxQueue", _FakeInbox, raising=False)
        monkeypatch.setattr(shadow_runtime, "_resolve_sampling_gate",
                            lambda decision_id, **kw: False, raising=False)

        on_gate = shadow_runtime._build_sampling_on_gate(
            server_id="srv1", session_id="sess1", vault=object(),
            policy=object(), ledger=[])

        # Deny ⇒ on_gate returns False (route_sampling_request will raise).
        allowed = on_gate({"kind": "sampling", "tier": 2,
                           "message_count": 1, "max_tokens": 32})
        assert allowed is False
        assert posted["gate_type"] == "sampling"
        assert posted["has_policy"] is True
        # Scope coords ride the card so a 'Trust for session' is per-server/session.
        assert posted["context_extras"]["server_id"] == "srv1"
        assert posted["context_extras"]["session_id"] == "sess1"

    def test_per_call_ledger_entry_written(self, monkeypatch):
        """Every sampling call writes a per-call ledger entry (auditable),
        regardless of allow/deny."""
        from systemu.runtime import shadow_runtime

        ledger = []

        class _FakeInbox:
            def __init__(self, vault): pass
            def enqueue(self, descriptor, *, gate_type, body="", policy=None,
                        capability="", vault=None, context_extras=None):
                return "dec_ok"

        monkeypatch.setattr(shadow_runtime, "InboxQueue", _FakeInbox, raising=False)
        monkeypatch.setattr(shadow_runtime, "_resolve_sampling_gate",
                            lambda decision_id, **kw: True, raising=False)

        on_gate = shadow_runtime._build_sampling_on_gate(
            server_id="srv1", session_id="sess1", vault=object(),
            policy=object(), ledger=ledger)
        on_gate({"kind": "sampling", "tier": 2, "message_count": 2, "max_tokens": 64})

        assert len(ledger) == 1
        entry = ledger[0]
        assert entry["server_id"] == "srv1"
        assert entry["session_id"] == "sess1"
        assert entry["allowed"] is True
        assert entry["message_count"] == 2
        # Hard invariant: NO prompt text / secret in the ledger entry.
        import json as _json
        blob = _json.dumps(entry).lower()
        assert "say hi" not in blob and "api_key" not in blob and "sk-" not in blob

    def test_summary_redactor_carries_no_prompt_text(self):
        from systemu.runtime.mcp.sdk import sampling
        req = {"systemPrompt": "SECRET-SYSTEM",
               "messages": [{"role": "user",
                             "content": {"type": "text", "text": "SECRET-USER"}}],
               "maxTokens": 99}
        s = sampling.sampling_summary(req, server_id="srv1", session_id="sess1", tier=2)
        import json as _json
        blob = _json.dumps(s)
        assert "SECRET-SYSTEM" not in blob and "SECRET-USER" not in blob
        assert s["message_count"] == 1 and s["max_tokens"] == 99
        assert s["server_id"] == "srv1" and s["tier"] == 2


class TestHermeticSamplingRoundTrip:
    @pytest.mark.asyncio
    async def test_in_process_server_sampling_routes_through_stubbed_llm_router(self, monkeypatch):
        """A hermetic in-process MCP server issues sampling/createMessage. The
        client's registered callback must route it through systemu's sampling
        core (stubbed llm_router) and return the completion. No key leaks to the
        server side."""
        mcp = pytest.importorskip("mcp")  # P2 dependency; skip honestly if absent
        from systemu.runtime.mcp.sdk import sampling, transports

        # Stub the parent model so the test is offline + key-free.
        captured = {}

        async def fake_llm_call(*, tier, system, user, config, **kw):
            captured["routed"] = True
            captured["user"] = user
            return {"content": "PARENT-ANSWER", "model": "stub/model"}

        monkeypatch.setattr(sampling, "_llm_call", fake_llm_call)

        # Build a connected (server, client-session) pair over in-memory streams.
        # VERIFIED against mcp==1.26.0: create_connected_server_and_client_session.
        from mcp.shared.memory import create_connected_server_and_client_session as connect

        # A minimal server that, when its one tool is called, asks the CLIENT to sample.
        server = transports.build_test_sampling_server()

        sampling_cb = transports.make_sampling_callback(config=object(), tier=2)
        async with connect(server, sampling_callback=sampling_cb) as client:
            await client.initialize()
            result = await client.call_tool("ask_parent", {"q": "what is 2+2?"})

        # The server's tool echoes back the parent's answer it received via sampling.
        text = "".join(getattr(c, "text", "") for c in result.content)
        assert "PARENT-ANSWER" in text
        assert captured.get("routed") is True
        assert "what is 2+2?" in captured["user"]


class TestRemotePolicy:
    def test_enforce_tls_rejects_plain_http_remote(self):
        from systemu.runtime.mcp.sdk import remote_policy
        with pytest.raises(remote_policy.InsecureTransportError):
            remote_policy.enforce_tls("http://example.com/mcp")

    def test_enforce_tls_allows_https(self):
        from systemu.runtime.mcp.sdk import remote_policy
        remote_policy.enforce_tls("https://example.com/mcp")  # no raise

    def test_enforce_tls_allows_loopback_http_when_explicitly_allowed(self):
        """A localhost dev server over http is fine ONLY when the operator put
        its host in allowed_mcp_hosts (the require_tls floor is for remote)."""
        from systemu.runtime.mcp.sdk import remote_policy
        remote_policy.enforce_tls("http://127.0.0.1:8080/mcp",
                                  allowed_hosts={"127.0.0.1"})  # no raise

    def test_host_allowed_denies_loopback_not_in_allowlist(self):
        from systemu.runtime.mcp.sdk import remote_policy
        assert remote_policy.mcp_host_allowed(
            "https://127.0.0.1/mcp", allowed_hosts=set()) is False

    def test_host_allowed_denies_metadata_endpoint(self):
        from systemu.runtime.mcp.sdk import remote_policy
        assert remote_policy.mcp_host_allowed(
            "https://169.254.169.254/latest", allowed_hosts=set()) is False

    def test_host_allowed_permits_public(self):
        from systemu.runtime.mcp.sdk import remote_policy
        assert remote_policy.mcp_host_allowed(
            "https://api.example.com/mcp", allowed_hosts=set()) is True

    def test_host_allowed_permits_allowlisted_private(self):
        from systemu.runtime.mcp.sdk import remote_policy
        assert remote_policy.mcp_host_allowed(
            "https://10.0.0.5/mcp", allowed_hosts={"10.0.0.5"}) is True

    def test_bounded_truncates_oversized_payload(self):
        from systemu.runtime.mcp.sdk import remote_policy
        out, truncated = remote_policy.bounded("x" * 100, max_chars=10)
        assert len(out) == 10 and truncated is True

    def test_health_marks_unhealthy_after_threshold_then_recovers(self):
        from systemu.runtime.mcp.sdk import remote_policy
        h = remote_policy.RemoteHealth(fail_threshold=2)
        assert h.healthy is True
        h.record_failure(); assert h.healthy is True
        h.record_failure(); assert h.healthy is False   # crossed threshold
        h.record_success(); assert h.healthy is True     # reconnect resets


class TestVaultTokenStore:
    def _vault(self, tmp_path):
        class _V:
            root = str(tmp_path)
        return _V()

    def test_store_roundtrip_persists_tokens(self, tmp_path):
        from systemu.runtime.mcp.sdk.oauth import VaultTokenStore
        store = VaultTokenStore(self._vault(tmp_path), "server_abc")
        assert store.load() == {}
        store.save({"access_token": "tok-123", "refresh_token": "ref-456"})
        assert store.load()["access_token"] == "tok-123"

    def test_store_path_is_under_connections_mcp_oauth(self, tmp_path):
        from systemu.runtime.mcp.sdk.oauth import VaultTokenStore
        store = VaultTokenStore(self._vault(tmp_path), "server_abc")
        p = store.path
        assert p.parent.name == "mcp_oauth"
        assert p.parent.parent.name == "connections"
        assert p.name == "server_abc.json"

    @pytest.mark.skipif(__import__("os").name == "nt",
                        reason="POSIX 0600 permission bits not enforced on Windows")
    def test_store_file_is_0600(self, tmp_path):
        import stat
        from systemu.runtime.mcp.sdk.oauth import VaultTokenStore
        store = VaultTokenStore(self._vault(tmp_path), "server_abc")
        store.save({"access_token": "tok"})
        mode = stat.S_IMODE(store.path.stat().st_mode)
        assert mode == 0o600

    def test_clear_removes_tokens(self, tmp_path):
        from systemu.runtime.mcp.sdk.oauth import VaultTokenStore
        store = VaultTokenStore(self._vault(tmp_path), "server_abc")
        store.save({"access_token": "tok"})
        store.clear()
        assert store.load() == {}


class TestUrlModeOAuth:
    def _vault(self, tmp_path):
        class _V:
            root = str(tmp_path)
        return _V()

    def test_acquire_oauth_prefers_url_mode_and_returns_pending(self, tmp_path):
        """URL-mode is the default: acquire_oauth asks the elicitation surface to
        show the operator the full authorize URL and returns a pending marker —
        it does NOT block, busy-wait, or carry any token."""
        from systemu.runtime.mcp.sdk import oauth

        shown = {}

        def fake_elicitation(*, url, server_id):
            shown["url"] = url
            shown["server_id"] = server_id
            return {"mode": "url", "status": "pending"}

        out = oauth.acquire_oauth(
            "server_abc",
            "https://auth.example.com/authorize?client_id=x&scope=read",
            vault=self._vault(tmp_path),
            elicitation_fn=fake_elicitation,
        )
        assert out["status"] == "oauth_pending"
        assert out["mode"] == "url"
        assert shown["url"].startswith("https://auth.example.com/authorize")
        assert shown["server_id"] == "server_abc"

    def test_no_token_or_secret_in_returned_payload(self, tmp_path):
        from systemu.runtime.mcp.sdk import oauth
        out = oauth.acquire_oauth(
            "server_abc",
            "https://auth.example.com/authorize?client_id=x",
            vault=self._vault(tmp_path),
            elicitation_fn=lambda **kw: {"mode": "url", "status": "pending"},
        )
        blob = __import__("json").dumps(out).lower()
        assert "access_token" not in blob
        assert "secret" not in blob
        assert "sk-" not in blob

    def test_from_oauth_url_uses_only_real_descriptor_fields(self):
        """B8: from_oauth_url must construct with the REAL fields only (the model
        is extra=forbid). dedup_key=/gate_type= would raise — assert it builds and
        carries the right fields."""
        from systemu.interface.command.gate import GateDescriptor
        d = GateDescriptor.from_oauth_url(
            server_id="server_abc",
            authorize_url="https://auth.example.com/authorize?client_id=x",
            execution_id="exec_1",
        )
        assert d.title == "Authorize MCP server: server_abc"
        assert d.risk == "high"
        assert d.inspect == "https://auth.example.com/authorize?client_id=x"
        assert d.options == ["Deny", "Approve"]
        assert d.safe_default == "Deny"
        assert d.what_approve_does == "Authorizes the MCP server out-of-band."
        assert d.dedup == "mcp_oauth:exec_1:server_abc"
        # The descriptor has NO dedup_key / gate_type attributes (extra=forbid).
        assert not hasattr(d, "dedup_key")
        assert not hasattr(d, "gate_type")

    def test_url_card_carries_url_not_secret(self, tmp_path, caplog):
        """The operator card carries the authorize URL on its context (operator
        must open it) but the BODY and LOG lines are query-masked — no raw query
        string (client_id / PKCE code_challenge) appears in body or transcript."""
        import logging
        from systemu.interface import harness_review

        posted = {}

        def fake_enqueue(self, descriptor, *, gate_type, body="", context_extras=None):
            posted["gate_type"] = gate_type
            posted["body"] = body
            posted["descriptor"] = descriptor
            posted["context_extras"] = context_extras or {}
            return "dec_test"

        import systemu.interface.command.inbox as inbox_mod
        orig = inbox_mod.InboxQueue.enqueue
        inbox_mod.InboxQueue.enqueue = fake_enqueue
        try:
            class _V:
                root = str(tmp_path)
            with caplog.at_level(logging.INFO):
                dec_id = harness_review.surface_oauth_url_card(
                    "server_abc",
                    "https://auth.example.com/authorize?client_id=x&code_challenge=abc",
                    execution_id="exec_1", activity_id="act_1", shadow_id="sh_1",
                    vault=_V(),
                )
        finally:
            inbox_mod.InboxQueue.enqueue = orig

        assert dec_id == "dec_test"
        assert posted["gate_type"] == "mcp_oauth"
        # The follow-up coords ride the context so the reconciler can find it.
        assert posted["context_extras"]["server_id"] == "server_abc"
        assert posted["context_extras"]["execution_id"] == "exec_1"
        assert posted["context_extras"]["follow_up"] == "mcp_oauth"
        # The full URL is present on context + inspect (operator must open it),
        # but there is no token field.
        assert "authorize" in posted["context_extras"]["authorize_url"]
        assert "access_token" not in posted["context_extras"]
        assert posted["descriptor"].inspect == (
            "https://auth.example.com/authorize?client_id=x&code_challenge=abc")
        # LOW: the BODY and LOG carry NO raw query string.
        assert "client_id=x" not in posted["body"]
        assert "code_challenge=abc" not in posted["body"]
        log_blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "client_id=x" not in log_blob
        assert "code_challenge=abc" not in log_blob


class TestOAuthPendingReconciler:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "connections/mcp_oauth"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                    "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def _post_oauth_gate(self, vault, *, choice, dispatched=False, age_seconds=0):
        """Post a resolved mcp_oauth follow-up gate decision."""
        from systemu.interface.command.gate import GateDescriptor
        from systemu.interface.command.inbox import InboxQueue
        from systemu.core.utils import utcnow
        from datetime import timedelta
        desc = GateDescriptor.from_oauth_url(server_id="server_abc",
                                             authorize_url="https://auth/x",
                                             execution_id="exec_1")
        ctx = {
            "execution_id": "exec_1", "activity_id": "act_1", "shadow_id": "sh_1",
            "server_id": "server_abc", "follow_up": "mcp_oauth",
            "authorize_url": "https://auth/x",
            "created_at": (utcnow() - timedelta(seconds=age_seconds)).isoformat(),
        }
        if dispatched:
            ctx["mcp_oauth_dispatched"] = True
        did = InboxQueue(vault).enqueue(desc, gate_type="mcp_oauth",
                                        body="", context_extras=ctx)
        d = vault.get_decision(did)
        d.status = "resolved"
        d.choice = choice
        vault.save_decision(d)
        return did

    def test_oauth_followup_does_not_stamp_harness_grant_dispatched(self, tmp_path):
        """The harness-grant reconciler must SKIP mcp_oauth follow-up rows so the
        ORIGINAL escalation can still complete — and must never stamp the
        harness_grant_dispatched flag on them."""
        from systemu.scheduler import jobs
        vault = self._vault(tmp_path)
        did = self._post_oauth_gate(vault, choice="Approve")

        class _FakeSup:
            def __init__(self): self.calls = []
            def resume_after_grant(self, **kw): self.calls.append(kw)

        sup = _FakeSup()
        jobs.reconcile_resolved_harness_grants(vault=vault, supervisor=sup,
                                               data_dir=tmp_path / "data")
        d = vault.get_decision(did)
        # The harness-grant reconciler ignored the follow-up row entirely.
        assert "harness_grant_dispatched" not in (d.context or {})

    def test_oauth_approve_resumes_and_stamps_its_own_flag(self, tmp_path, monkeypatch):
        """The dedicated mcp_oauth reconciler resumes the run on Approve and
        stamps its OWN idempotency flag (mcp_oauth_dispatched), not the harness one."""
        from systemu.scheduler import jobs
        vault = self._vault(tmp_path)
        did = self._post_oauth_gate(vault, choice="Approve")

        class _FakeSup:
            def __init__(self): self.calls = []
            def resume_after_grant(self, **kw): self.calls.append(kw)

        sup = _FakeSup()
        n = jobs.reconcile_resolved_mcp_oauth(vault=vault, supervisor=sup,
                                              data_dir=tmp_path / "data")
        assert n == 1
        assert len(sup.calls) == 1
        assert sup.calls[0]["execution_id"] == "exec_1"
        d = vault.get_decision(did)
        assert d.context.get("mcp_oauth_dispatched") is True
        assert "harness_grant_dispatched" not in d.context  # never the harness flag

    def test_oauth_idempotent_second_tick_no_double_resume(self, tmp_path):
        from systemu.scheduler import jobs
        vault = self._vault(tmp_path)
        self._post_oauth_gate(vault, choice="Approve", dispatched=True)

        class _FakeSup:
            def __init__(self): self.calls = []
            def resume_after_grant(self, **kw): self.calls.append(kw)

        sup = _FakeSup()
        n = jobs.reconcile_resolved_mcp_oauth(vault=vault, supervisor=sup,
                                              data_dir=tmp_path / "data")
        assert n == 0 and sup.calls == []

    def test_oauth_deny_resumes_with_harness_grant_failed(self, tmp_path):
        """Operator Deny ⇒ resume carries a denied payload so the run gets a
        harness_grant_failed observation (it abandons the connection)."""
        from systemu.scheduler import jobs
        vault = self._vault(tmp_path)
        self._post_oauth_gate(vault, choice="Deny")

        class _FakeSup:
            def __init__(self): self.calls = []
            def resume_after_grant(self, **kw): self.calls.append(kw)

        sup = _FakeSup()
        jobs.reconcile_resolved_mcp_oauth(vault=vault, supervisor=sup,
                                          data_dir=tmp_path / "data")
        assert len(sup.calls) == 1
        assert sup.calls[0]["grant_payload"]["denied"] is True


class TestHarnessPolicyMcpFields:
    def test_defaults_are_fail_closed(self):
        from systemu.runtime.harness_policy import HarnessPolicy
        p = HarnessPolicy()
        assert p.mcp_require_tls is True         # remote must be TLS by default
        assert p.mcp_oauth_timeout_s == 1800
        # NOTE: allowed_mcp_hosts is P3-owned — NOT asserted here (P4 does not add it).

    def test_from_config_reads_mcp_fields(self):
        from systemu.runtime.harness_policy import HarnessPolicy
        p = HarnessPolicy.from_config({
            "mcp_require_tls": False,
            "mcp_oauth_timeout_s": 600,
        })
        assert p.mcp_require_tls is False
        assert p.mcp_oauth_timeout_s == 600

    def test_from_config_defaults_when_absent(self):
        from systemu.runtime.harness_policy import HarnessPolicy
        p = HarnessPolicy.from_config({})  # no MCP keys present
        assert p.mcp_require_tls is True
        assert p.mcp_oauth_timeout_s == 1800


class TestManagerRemotePolicy:
    def test_plain_http_remote_refused_before_transport_opens(self, monkeypatch):
        """ConnectionManager.connect must enforce TLS + host policy BEFORE it
        opens the SDK transport — a plain-http public URL is refused and the SDK
        opener is never called."""
        pytest.importorskip("mcp")
        from systemu.runtime.mcp.sdk import manager as mgr_mod
        from systemu.runtime.mcp.sdk.remote_policy import InsecureTransportError

        opened = {"n": 0}
        # Neutralise the actual SDK transport open so the test stays offline.
        if hasattr(mgr_mod.ConnectionManager, "_open_remote"):
            async def fake_open(self, *a, **k):
                opened["n"] += 1
            monkeypatch.setattr(mgr_mod.ConnectionManager, "_open_remote", fake_open)

        m = mgr_mod.ConnectionManager()
        with pytest.raises(InsecureTransportError):
            m.connect_remote_sync("http://api.example.com/mcp",
                                  allowed_hosts=set(), require_tls=True)
        assert opened["n"] == 0

    def test_ssrf_loopback_refused(self, monkeypatch):
        pytest.importorskip("mcp")
        from systemu.runtime.mcp.sdk import manager as mgr_mod
        m = mgr_mod.ConnectionManager()
        with pytest.raises(PermissionError):
            m.connect_remote_sync("https://127.0.0.1/mcp",
                                  allowed_hosts=set(), require_tls=True)


class TestWebActBridgeReuse:
    def test_plan_next_uses_bridge_when_supplied(self, monkeypatch):
        """When a bridge is supplied, _plan_next routes the planning LLM call
        through it (the same parent-LLM path as MCP sampling) instead of calling
        llm_call_json in-process."""
        from systemu.runtime.web import act_loop

        used = {}

        def fake_bridge(messages, *, config, tier):
            used["messages"] = messages
            used["tier"] = tier
            return '{"action":"DONE","result":"bridged"}'

        decision = act_loop._plan_next(
            "click login", [{"role": "button", "name": "Login", "ref": "e1"}],
            [], config=object(), bridge=fake_bridge,
        )
        assert decision["action"] == "DONE"
        assert decision["result"] == "bridged"
        assert used["tier"] == 2  # systemu's tier choice, not the page's

    def test_plan_next_default_path_unchanged(self, monkeypatch):
        """No bridge ⇒ byte-identical legacy behavior (llm_call_json in-process)."""
        from systemu.runtime.web import act_loop

        called = {"n": 0}

        def fake_llm_call_json(**kw):
            called["n"] += 1
            return {"action": "READ"}

        monkeypatch.setattr(act_loop, "llm_call_json", fake_llm_call_json, raising=False)
        # The default path imports llm_call_json lazily inside _plan_next; patch the
        # module attribute it resolves to.
        import systemu.core.llm_router as router
        monkeypatch.setattr(router, "llm_call_json", fake_llm_call_json)

        decision = act_loop._plan_next("read page", [], [], config=object())
        assert decision == {"action": "READ"}
        assert called["n"] == 1
