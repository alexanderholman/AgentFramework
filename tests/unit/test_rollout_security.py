"""Rollout cohort compatibility in environments forbidding cryptographic MD5."""

from hashlib import md5 as original_md5

from af.flags import strategies


def test_rollout_uses_nonsecurity_hash_without_reshuffling_cohorts(monkeypatch):
    def fips_hash(data, *, usedforsecurity=True):
        if usedforsecurity:
            raise ValueError("MD5 forbidden for security use")
        return original_md5(data, usedforsecurity=False)

    monkeypatch.setattr(strategies, "md5", fips_hash)
    for identity in ("user-1", "user-2", "customer-100"):
        expected = (
            int(original_md5(identity.encode(), usedforsecurity=False).hexdigest()[:8], 16)
            / 0xFFFFFFFF
        )
        assert strategies.PercentageRollout._hash_bucket(identity) == expected
