def test_branch_protection_probe():
    """Deliberately failing. Throwaway probe branch, never merged."""
    observed = 1
    expected = 2
    assert observed == expected
