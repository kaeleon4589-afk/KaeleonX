from app.logging.logger import _redact


def test_long_sequences_are_omitted_from_logs():
    value = {"ema200_series": list(range(320)), "small": [1, 2, 3]}
    out = _redact(value)
    assert out["ema200_series"] == {"_sequence_omitted": True, "count": 320}
    assert out["small"] == [1, 2, 3]


def test_long_strings_are_truncated():
    out = _redact("x" * 2000)
    assert len(out) < 1300
    assert out.endswith("<truncated>")
