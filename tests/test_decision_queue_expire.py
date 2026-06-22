"""expire_by_dedup_key drops a pending row from the Inbox/list_pending."""


def test_expire_by_dedup_key(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    from systemu.approval.decision_queue import OperatorDecisionQueue
    vault = FileVault(Vault(str(tmp_path / "v")))
    q = OperatorDecisionQueue(vault)
    dec_id = q.post(title="t", body="b", options=["Skip", "Apply"],
                    context={"kind": "gate", "gate_type": "recovery"},
                    dedup_key="recovery:tool:tool_x:DEP_PENDING")
    assert any(d.id == dec_id for d in q.list_pending())
    expired = q.expire_by_dedup_key("recovery:tool:tool_x:DEP_PENDING")
    assert expired is True
    assert not any(d.id == dec_id for d in q.list_pending())  # gone from pending
    # idempotent: expiring again returns False (nothing pending)
    assert q.expire_by_dedup_key("recovery:tool:tool_x:DEP_PENDING") is False
