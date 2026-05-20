from utils.yf_retry import classify_yahoo_error, fetch_with_retry


def test_classify_yahoo_error_distinguishes_dns_and_rate_limit():
    assert classify_yahoo_error(Exception("Failed to perform, curl: (6) Could not resolve host: query2.finance.yahoo.com")) == "dns"
    assert classify_yahoo_error(Exception("HTTP Error 429: Too Many Requests")) == "rate_limit"
    assert classify_yahoo_error(Exception("boom")) == "other"


def test_fetch_with_retry_retries_dns_with_exponential_backoff():
    attempts = {"count": 0}
    sleeps = []

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise Exception("curl: (6) Could not resolve host: query2.finance.yahoo.com")
        return "ok"

    out = fetch_with_retry(
        flaky,
        ticker="1810.HK",
        max_attempts=3,
        sleep_fn=sleeps.append,
        logger=None,
    )

    assert out == "ok"
    assert attempts["count"] == 3
    assert sleeps == [1.0, 2.0]


def test_fetch_with_retry_retries_429_with_longer_backoff():
    attempts = {"count": 0}
    sleeps = []

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise Exception("HTTP Error 429: Too Many Requests")
        return "ok"

    out = fetch_with_retry(
        flaky,
        ticker="AAPL",
        max_attempts=3,
        sleep_fn=sleeps.append,
        logger=None,
    )

    assert out == "ok"
    assert attempts["count"] == 3
    assert sleeps == [2.0, 4.0]


def test_fetch_with_retry_does_not_retry_unknown_errors():
    attempts = {"count": 0}
    sleeps = []

    def bad():
        attempts["count"] += 1
        raise RuntimeError("unexpected parse failure")

    try:
        fetch_with_retry(
            bad,
            ticker="AAPL",
            max_attempts=3,
            sleep_fn=sleeps.append,
            logger=None,
        )
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "unexpected parse failure" in str(exc)

    assert attempts["count"] == 1
    assert sleeps == []
