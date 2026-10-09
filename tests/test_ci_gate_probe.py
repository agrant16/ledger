def test_ci_gate_probe_fails_on_purpose() -> None:
    """Temporary: proves a failing test blocks merging. Never merge; delete with the branch."""
    expected = 1
    actual = 2
    assert expected == actual, "deliberate failure to verify the merge gate"
