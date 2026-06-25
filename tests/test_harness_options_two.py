def test_harness_options_are_deny_approve_only():
    from systemu.interface.harness_review import _HARNESS_OPTIONS as A
    from systemu.interface.command.gate import _HARNESS_OPTIONS as B
    assert A == ["Deny", "Approve"]
    assert B == ["Deny", "Approve"]
    assert "Edit spec" not in A and "Edit spec" not in B
