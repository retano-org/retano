# users/views.py
from django.contrib.auth import get_user_model

from rest_framework import generics, permissions, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView as BaseTokenRefreshView

from core.exceptions import OTPError
from core.schema import OTP_REQUEST_SCHEMA, OTP_VERIFY_SCHEMA, REGISTER_SCHEMA, LOGOUT_SCHEMA, PROFILE_SCHEMA, ACCOUNT_STATUS_SCHEMA, TOKEN_REFRESH_SCHEMA
from .auth.otp import OTPService
from .throttles import OTPPhoneScopedRateThrottle
from .serializers import (
    LogoutSerializer,
    OTPRequestSerializer,
    OTPVerifySerializer,
    ProfileSerializer,
    RegisterSerializer,
)

User = get_user_model()


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────

@OTP_REQUEST_SCHEMA
class OTPRequestView(APIView):
    """POST /api/v1/auth/otp/request/"""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [OTPPhoneScopedRateThrottle]
    throttle_scope = "otp_request"

    def post(self, request):
        serializer = OTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone_number = serializer.validated_data["phone_number"]

        result = OTPService().issue(phone_number)

        data = {
            "phone_number": result.phone_number,
            "ttl_seconds": result.ttl_seconds,
            "resend_in_seconds": result.resend_in_seconds,
        }
        if result.debug_code is not None:
            data["debug_code"] = result.debug_code

        return Response(data, status=status.HTTP_200_OK)


@OTP_VERIFY_SCHEMA
class OTPVerifyView(APIView):
    """
    POST /api/v1/auth/otp/verify/

    Verifies the code and issues JWT tokens. If no user exists yet for
    this phone number, one is created (verify doubles as implicit
    registration), which also triggers the existing Tenant-creation
    signal.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [OTPPhoneScopedRateThrottle]
    throttle_scope = "otp_verify"

    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone_number = serializer.validated_data["phone_number"]
        code = serializer.validated_data["code"]

        OTPService().verify(phone_number, code)

        user, _created = User.objects.get_or_create(phone_number=phone_number)
        tokens = serializer.create_tokens(user)

        return Response(
            {
                "user_id": user.id,
                "phone_number": user.phone_number,
                **tokens,
            },
            status=status.HTTP_200_OK,
        )


@REGISTER_SCHEMA
class RegisterView(generics.CreateAPIView):
    """POST /api/v1/auth/register/"""

    permission_classes = [permissions.AllowAny]
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "user_id": user.id,
                "phone_number": user.phone_number,
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_201_CREATED,
        )


@TOKEN_REFRESH_SCHEMA
class TokenRefreshView(BaseTokenRefreshView):
    pass


@LOGOUT_SCHEMA
class LogoutView(APIView):
    """POST /api/v1/auth/logout/ — blacklists the supplied refresh token."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            token = RefreshToken(serializer.validated_data["refresh"])
            token.blacklist()
        except TokenError:
            raise OTPError("Invalid or already-expired refresh token.")

        return Response(status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────────────────────
# Profile
# ─────────────────────────────────────────────────────────────────────────────

@PROFILE_SCHEMA
class ProfileView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/v1/profile/ — the authenticated user's own profile."""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ProfileSerializer

    def get_object(self):
        user = self.request.user
        if user is None or not user.is_authenticated:
            raise NotFound("No authenticated user.")
        return user


@ACCOUNT_STATUS_SCHEMA
class AccountStatusView(APIView):
    """
    GET /api/v1/account/status/

    Lightweight account-state summary for frontend gating (e.g. whether
    to show an onboarding flow). No serializer — the shape is fixed and
    doesn't map onto a model field-for-field.
    """

    permission_classes = [permissions.IsAuthenticated]

    #: Profile fields considered part of "completing" the profile.
    PROFILE_REQUIRED_FIELDS = ("first_name", "last_name", "shop_name")

    def get(self, request):
        user = request.user

        profile_complete = all(
            getattr(user, field, None) for field in self.PROFILE_REQUIRED_FIELDS
        )
        has_tenant = hasattr(user, "tenant") and user.tenant_id is not None

        return Response(
            {
                "user_id": user.id,
                "phone_number": user.phone_number,
                # Reaching an authenticated request requires a valid JWT,
                # which is only ever issued after a successful OTP verify
                # or registration — so any authenticated user's phone is
                # verified by construction. There is no separate
                # "unverified but logged in" state in this system.
                "phone_verified": True,
                "is_premium": user.is_premium,
                "has_tenant": has_tenant,
                "profile_complete": profile_complete,
            },
            status=status.HTTP_200_OK,
        )
