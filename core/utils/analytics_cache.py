"""Tenant-scoped cache helpers for dashboard and report payloads."""

from __future__ import annotations

import logging
import time
from typing import Any

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("retano.analytics.cache")
_CACHE_FORMAT_VERSION = "v1"
_PAYLOAD_KEY = "payload"
_CREATED_AT_KEY = "created_at"


def analytics_cache_key(tenant_id: int, report: str) -> str:
    return f"analytics:{_CACHE_FORMAT_VERSION}:tenant:{tenant_id}:{report}"


def analytics_refresh_lock_key(tenant_id: int) -> str:
    return f"analytics:{_CACHE_FORMAT_VERSION}:tenant:{tenant_id}:refresh-lock"


def get_cached_payload(
    tenant_id: int,
    report: str,
    *,
    fresh_seconds: int,
) -> tuple[Any | None, bool]:
    """Return ``(payload, is_stale)``; a stale payload is still usable."""

    envelope = cache.get(analytics_cache_key(tenant_id, report))
    if not isinstance(envelope, dict) or _PAYLOAD_KEY not in envelope:
        return None, False

    created_at = envelope.get(_CREATED_AT_KEY, 0)
    is_stale = time.time() - created_at >= fresh_seconds
    return envelope[_PAYLOAD_KEY], is_stale


def set_cached_payload(tenant_id: int, report: str, payload: Any) -> None:
    cache.set(
        analytics_cache_key(tenant_id, report),
        {
            _PAYLOAD_KEY: payload,
            _CREATED_AT_KEY: time.time(),
        },
        timeout=settings.ANALYTICS_CACHE_TTL_SECONDS,
    )


def schedule_analytics_refresh(tenant_id: int) -> bool:
    """Queue one refresh per tenant and collapse concurrent refresh requests."""

    lock_key = analytics_refresh_lock_key(tenant_id)
    acquired = cache.add(
        lock_key,
        "1",
        timeout=settings.ANALYTICS_REFRESH_LOCK_SECONDS,
    )
    if not acquired:
        return False

    try:
        from core.tasks.analytics import refresh_tenant_analytics_cache

        refresh_tenant_analytics_cache.delay(tenant_id)
    except Exception:
        cache.delete(lock_key)
        logger.exception("Could not queue analytics refresh for tenant %s", tenant_id)
        return False

    return True


def release_analytics_refresh_lock(tenant_id: int) -> None:
    cache.delete(analytics_refresh_lock_key(tenant_id))
