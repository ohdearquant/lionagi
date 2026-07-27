def test_enforce_admins_probe():
    """Deliberately failing. Throwaway probe branch, never merged."""
    observed = 1
    expected = 2
    assert observed == expected
