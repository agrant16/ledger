def test_ci_gate_probe_fails_on_purpose() -> None:
    """Temporary: proves a red CI run blocks merging. Never merge; delete with the branch."""
    assert False, "deliberate failure to verify the merge gate"
