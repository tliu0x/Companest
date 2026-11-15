def test_exception_details():
    from companest.exceptions import PiError
    e = PiError("test error", details={"model": "claude"})
    assert "test error" in str(e)
    assert "claude" in str(e)
