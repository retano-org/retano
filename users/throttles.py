"""OTP throttles keyed by the normalized phone number, not client IP."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.throttling import ScopedRateThrottle

from users.auth.phone import normalize_iranian_phone


class OTPPhoneScopedRateThrottle(ScopedRateThrottle):
    """Apply a view's throttle scope independently to each phone number."""

    def get_cache_key(self, request, view):
        if not self.scope:
            return None

        try:
            raw_phone = request.data.get("phone_number")
            phone_number = normalize_iranian_phone(raw_phone)
        except (AttributeError, DjangoValidationError, TypeError):
            # Invalid payloads still use an IP bucket, while every valid phone
            # gets its own bucket and can never consume another phone's quota.
            phone_number = f"invalid:{self.get_ident(request)}"

        return self.cache_format % {
            "scope": self.scope,
            "ident": phone_number,
        }
