from youtube_agent.quota import QuotaExceeded, QuotaStore


def test_byte_quota_cannot_be_crossed(tmp_path):
    quota = QuotaStore(tmp_path / "quota.sqlite3", jobs_per_hour=10, bytes_per_day=100)
    first = quota.reserve("alice")
    quota.complete(first, 80)
    second = quota.reserve("alice")
    try:
        quota.ensure_capacity(second, 21)
    except QuotaExceeded:
        pass
    else:
        raise AssertionError("daily byte quota was not enforced")
