from datetime import timedelta

import pytest

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, override_settings
from django.utils import timezone

from core.exceptions import OTPError
from users.admin import OTPAdmin
from users.auth.otp import OTPService
from users.auth.sms import FakeOTPSender, OTPSender
from users.models import OTP

pytestmark = pytest.mark.django_db


class FailingOTPSender(OTPSender):
    def send(self, *, phone_number: str, code: str) -> None:
        raise RuntimeError("SMS delivery failed")


@pytest.fixture
def staff_user():
    return get_user_model().objects.create_user(
        phone_number="+989120000011",
        username="otp-staff",
        is_staff=True,
    )


@pytest.fixture
def superuser():
    return get_user_model().objects.create_superuser(
        phone_number="+989120000012",
        username="otp-superuser",
        password="test-password",
    )


def admin_request(user):
    request = RequestFactory().get("/admin/users/otp/")
    request.user = user
    return request


@override_settings(OTP_TTL_SECONDS=120)
def test_successful_fake_issue_stores_exact_code():
    result = OTPService(sender=FakeOTPSender()).issue("+989120000013")

    assert len(result.debug_code) == 4
    assert result.debug_code.isdigit()
    assert OTP.objects.get().otp_code == result.debug_code


@override_settings(OTP_TTL_SECONDS=120)
def test_otp_can_be_reissued_immediately_without_cooldown():
    service = OTPService(sender=FakeOTPSender())

    first = service.issue("+989120000017")
    second = service.issue("+989120000017")

    assert first.resend_in_seconds == 0
    assert second.resend_in_seconds == 0


@override_settings(OTP_TTL_SECONDS=120)
def test_failed_issue_does_not_store_code():
    with pytest.raises(OTPError):
        OTPService(sender=FailingOTPSender()).issue("+989120000014")

    assert not OTP.objects.exists()


@override_settings(OTP_TTL_SECONDS=120)
def test_successful_verification_removes_admin_copy():
    service = OTPService(sender=FakeOTPSender())
    result = service.issue("+989120000015")

    service.verify("+989120000015", result.debug_code)

    assert not OTP.objects.exists()


@override_settings(OTP_MAX_VERIFY_ATTEMPTS=1, OTP_TTL_SECONDS=120)
def test_exhausted_verification_attempts_remove_admin_copy():
    service = OTPService(sender=FakeOTPSender())
    result = service.issue("+989120000016")
    wrong_code = "0000" if result.debug_code != "0000" else "1111"

    with pytest.raises(OTPError):
        service.verify("+989120000016", wrong_code)

    assert not OTP.objects.exists()


def test_only_superusers_can_view_otp_admin(staff_user, superuser):
    model_admin = OTPAdmin(OTP, admin.site)

    assert not model_admin.has_module_permission(admin_request(staff_user))
    assert not model_admin.has_view_permission(admin_request(staff_user))
    assert model_admin.has_module_permission(admin_request(superuser))
    assert model_admin.has_view_permission(admin_request(superuser))


def test_otp_admin_is_entirely_read_only(superuser):
    model_admin = OTPAdmin(OTP, admin.site)
    request = admin_request(superuser)

    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)


@override_settings(OTP_TTL_SECONDS=120)
def test_expired_codes_are_purged_from_admin(superuser):
    otp = OTP.objects.create(otp_code="1234")
    OTP.objects.filter(pk=otp.pk).update(
        created_at=timezone.now() - timedelta(seconds=121)
    )
    model_admin = OTPAdmin(OTP, admin.site)

    queryset = model_admin.get_queryset(admin_request(superuser))

    assert not queryset.exists()
    assert not OTP.objects.filter(pk=otp.pk).exists()
