from types import SimpleNamespace

from users.throttles import OTPPhoneScopedRateThrottle


def _cache_key(phone_number: str, remote_addr: str = "203.0.113.10") -> str:
    request = SimpleNamespace(
        data={"phone_number": phone_number},
        META={"REMOTE_ADDR": remote_addr},
    )
    throttle = OTPPhoneScopedRateThrottle()
    throttle.scope = "otp_request"
    return throttle.get_cache_key(request, SimpleNamespace())


def test_otp_throttle_does_not_share_a_bucket_between_phones():
    assert _cache_key("09120000001") != _cache_key("09120000002")


def test_otp_throttle_normalizes_equivalent_phone_formats():
    assert _cache_key("09120000001") == _cache_key("+989120000001")


def test_otp_throttle_is_not_affected_by_ip_for_valid_phones():
    assert _cache_key("09120000001", "203.0.113.10") == _cache_key(
        "09120000001", "203.0.113.11"
    )
