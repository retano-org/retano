"""
OTP issuance, storage, and verification.

Design
------
* OTP codes are 4 random digits (``settings.OTP_LENGTH``).
* They live in the Django cache (Redis in prod) under a namespaced key,
  with a TTL of ``settings.OTP_TTL_SECONDS`` (default 120s).
* Each issued code allows at most ``MAX_VERIFY_ATTEMPTS`` wrong
  submissions. After that the code is invalidated and the user must
  request a new one.
* The Kavenegar call goes through :mod:`users.auth.sms` so it can be
  swapped for a fake in tests via ``settings.OTP_FAKE_MODE = True``.

The service is intentionally framework-agnostic — DRF views call
:class:`OTPService` directly. Errors surface as
:class:`core.exceptions.OTPError` (HTTP 400).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.core.cache import cache

from core.exceptions import OTPError
from users.models import OTP

from .sms import OTPSender, get_default_sender


logger = logging.getLogger("retano.auth.otp")


# ─────────────────────────────────────────────────────────────────────────────
# Tunables (override in settings if needed)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_OTP_TTL_SECONDS = 120
DEFAULT_OTP_LENGTH = 4
MAX_VERIFY_ATTEMPTS = 5


# ─────────────────────────────────────────────────────────────────────────────
# Cache keys
# ─────────────────────────────────────────────────────────────────────────────


def _code_key(phone: str) -> str:
    return f"otp:code:{phone}"


def _attempts_key(phone: str) -> str:
    return f"otp:attempts:{phone}"


def _record_key(phone: str) -> str:
    return f"otp:record:{phone}"


def _delete_otp_record(phone: str) -> None:
    record_id = cache.get(_record_key(phone))
    if record_id is not None:
        OTP.objects.filter(pk=record_id).delete()
    cache.delete(_record_key(phone))


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class OTPIssueResult:
    """What the request endpoint returns to the view layer."""

    phone_number: str
    ttl_seconds: int
    resend_in_seconds: int
    #: Only populated in fake/dev mode; never sent to clients in prod.
    debug_code: Optional[str] = None


class OTPService:
    """Thin orchestrator around the cache + SMS sender."""

    def __init__(self, sender: Optional[OTPSender] = None) -> None:
        self._sender = sender or get_default_sender()
        self._ttl = getattr(settings, "OTP_TTL_SECONDS", DEFAULT_OTP_TTL_SECONDS)
        self._length = getattr(settings, "OTP_LENGTH", DEFAULT_OTP_LENGTH)
        self._max_attempts = getattr(
            settings, "OTP_MAX_VERIFY_ATTEMPTS", MAX_VERIFY_ATTEMPTS
        )

    # ── Issue ──────────────────────────────────────────────────────────────

    def issue(self, phone_number: str) -> OTPIssueResult:
        """Generate an OTP, persist it, and send the SMS."""
        code = self._generate_code()

        # Persist the code first, then send. If sending fails we delete it
        # so an undelivered OTP cannot accidentally authenticate someone.
        cache.set(_code_key(phone_number), code, timeout=self._ttl)
        cache.set(_attempts_key(phone_number), 0, timeout=self._ttl)
        OTP.objects.purge_expired()
        otp = OTP.objects.create(otp_code=code)
        cache.set(_record_key(phone_number), otp.pk, timeout=self._ttl)

        try:
            self._sender.send(phone_number=phone_number, code=code)
        except Exception:
            OTP.objects.filter(pk=otp.pk).delete()
            cache.delete(_record_key(phone_number))
            cache.delete(_code_key(phone_number))
            cache.delete(_attempts_key(phone_number))
            logger.exception("Failed to send OTP to %s", phone_number)
            raise OTPError(
                "Could not send the verification SMS. Please try again shortly."
            )

        logger.info("Issued OTP for %s (ttl=%ss)", phone_number, self._ttl)
        return OTPIssueResult(
            phone_number=phone_number,
            ttl_seconds=self._ttl,
            resend_in_seconds=0,
            debug_code=code if self._sender.is_fake else None,
        )

    # ── Verify ─────────────────────────────────────────────────────────────

    def verify(self, phone_number: str, code: str) -> None:
        """Validate ``code`` against the stored OTP.

        Returns ``None`` on success, raises :class:`OTPError` otherwise.
        Successful verification consumes the code (single-use).
        """
        stored = cache.get(_code_key(phone_number))
        if stored is None:
            _delete_otp_record(phone_number)
            raise OTPError("The OTP has expired. Request a new one.")

        attempts = cache.get(_attempts_key(phone_number)) or 0
        if attempts >= self._max_attempts:
            _delete_otp_record(phone_number)
            cache.delete(_code_key(phone_number))
            cache.delete(_attempts_key(phone_number))
            raise OTPError(
                "Too many incorrect attempts. Request a new OTP."
            )

        # Constant-time compare avoids leaking timing information.
        if not secrets.compare_digest(str(stored), str(code).strip()):
            try:
                attempts = cache.incr(_attempts_key(phone_number))
            except ValueError:
                _delete_otp_record(phone_number)
                # Key vanished between get and incr — treat as expired.
                raise OTPError("The OTP has expired. Request a new one.")
            if attempts >= self._max_attempts:
                _delete_otp_record(phone_number)
            raise OTPError("The OTP is incorrect.")

        # Success — burn the code so it cannot be replayed.
        _delete_otp_record(phone_number)
        cache.delete(_code_key(phone_number))
        cache.delete(_attempts_key(phone_number))

    # ── Internals ──────────────────────────────────────────────────────────

    def _generate_code(self) -> str:
        upper = 10 ** self._length
        return f"{secrets.randbelow(upper):0{self._length}d}"
