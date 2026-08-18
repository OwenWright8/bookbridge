"""
CWA Sync API client — reads and writes reading progress via
Calibre-Web Automated's Kobo sync endpoints.
"""

import os
import logging
import requests

from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)

# Kobo reading status constants (per Kobo API protocol)
STATUS_READING = "Reading"
STATUS_FINISHED = "Finished"
STATUS_READY = "ReadyToRead"


class CWASyncApi:
    def __init__(self, cwa_client=None, credentials: dict = None):
        self._cwa_client = cwa_client
        self._creds = credentials
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._timeout = 15

        # Snapshot config at init (matches CWAClient pattern). CWA_SYNC_TOKEN/
        # ENABLED are per-user when credentials are provided; server is global.
        self._server = (cwa_client.base_url if cwa_client else
                        resolve_setting(credentials, "CWA_SERVER", "").rstrip("/"))
        self._token = (resolve_setting(credentials, "CWA_SYNC_TOKEN", "") or "").strip()
        self._enabled = str(resolve_setting(credentials, "CWA_SYNC_ENABLED", "")).lower() == "true"

    @property
    def _base_url(self) -> str:
        return f"{self._server}/kobo/{self._token}/v1"

    def is_configured(self) -> bool:
        return self._enabled and bool(self._server) and bool(self._token)

    def check_connection(self) -> bool:
        if not self.is_configured():
            logger.warning("⚠️ CWA Sync not configured (skipping)")
            return False

        try:
            url = f"{self._base_url}/initialization"
            r = self._session.get(url, timeout=5)
            if r.status_code == 200:
                get_persistent_condition_logger().resolve(
                    logger,
                    f"cwa_sync_connection:{self._base_url}",
                    f"✅ CWA Sync connection recovered at {self._server}",
                )
                logger.info(f"✅ Connected to CWA sync at {self._server}")
                return True
            elif r.status_code in [401, 403]:
                logger.error(f"❌ CWA Sync auth failed: {r.status_code}. Check auth token.")
                return False
            else:
                logger.error(f"❌ CWA Sync connection failed: {r.status_code}")
                return False
        except Exception as e:
            # An unreachable CWA host fails this check on every cycle, so the
            # same ERROR repeated forever. Suppress repeats but keep the
            # pre-existing ERROR severity, matching CWAClient.check_connection.
            get_persistent_condition_logger().warn(
                logger,
                f"cwa_sync_connection:{self._base_url}",
                f"❌ CWA Sync connection error: {e}",
                exc_info=True,
                level=logging.ERROR,
            )
            return False

    def get_reading_state(self, book_uuid: str) -> dict | None:
        """Returns dict with progress_percent (0-1), status, href, frag; or None."""
        if not self.is_configured():
            return None

        try:
            url = f"{self._base_url}/library/{book_uuid}/state"
            r = self._session.get(url, timeout=self._timeout)

            if r.status_code != 200:
                logger.debug(f"📖 CWA Sync: GET state for {book_uuid} returned {r.status_code}")
                return None

            data = r.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                return None

            entry = data[0]
            bookmark = entry.get("CurrentBookmark") or {}
            status_info = entry.get("StatusInfo") or {}

            # CWA API uses 0-100 scale; normalize to 0-1
            raw_progress = float(bookmark.get("ProgressPercent", 0.0) or 0.0)
            progress = raw_progress / 100.0
            status = status_info.get("Status", STATUS_READY)

            location = bookmark.get("Location") or {}

            return {
                "progress_percent": progress,
                "status": status,
                "href": location.get("Source"),
                "frag": location.get("Value"),
                # Position freshness. Deliberately CurrentBookmark.LastModified:
                # the entry-level LastModified/PriorityTimestamp also move on
                # status changes and on the bridge's own writes (verified live),
                # so they would manufacture false "fresh position" signals.
                "bookmark_last_modified": bookmark.get("LastModified"),
            }

        except Exception as e:
            logger.error(f"❌ CWA Sync: Failed to get reading state for {book_uuid}: {e}", exc_info=True)
            return None

    def update_reading_state(self, book_uuid: str, progress_percent: float, status: str = STATUS_READING,
                              location: dict = None) -> bool:
        """Push reading position to CWA via Kobo sync protocol.

        `location`, when provided, is a native Kobo span pointer
        ({"Source": href, "Type": "KoboSpan", "Value": "kobo.N.M"}) — see
        KepubSpanResolver. Percent alone doesn't move a stock Kobo's resume
        position; Nickel trusts its own local CurrentBookmark.Location over
        the server-reported percent. Left as None when resolution wasn't
        possible, which CWA/Nickel treat as percent-only.
        """
        if not self.is_configured():
            return False

        try:
            url = f"{self._base_url}/library/{book_uuid}/state"
            # CWA API uses 0-100 scale; bridge uses 0-1
            api_pct = progress_percent * 100.0
            logger.debug(f"📖 CWA Sync: resolved Location={location!r} for {book_uuid}")
            payload = {
                "ReadingStates": [{
                    "CurrentBookmark": {
                        "ProgressPercent": api_pct,
                        "ContentSourceProgressPercent": api_pct,
                        "Location": location,
                    },
                    "Statistics": None,
                    "StatusInfo": {"Status": status},
                }]
            }

            r = self._session.put(url, json=payload, timeout=self._timeout)

            if r.status_code == 200:
                resp = r.json()
                if resp.get("RequestResult") == "Success":
                    logger.info(f"📖 CWA Sync: Updated {book_uuid} to {progress_percent:.1%} ({status})")
                    return True
                else:
                    logger.warning(f"⚠️ CWA Sync: Update returned non-success: {resp}")
                    return False
            else:
                logger.error(f"❌ CWA Sync: Update failed for {book_uuid}: HTTP {r.status_code}")
                return False

        except Exception as e:
            logger.error(f"❌ CWA Sync: Failed to update reading state for {book_uuid}: {e}", exc_info=True)
            return False

    def resolve_book_uuid(self, calibre_id: str) -> str | None:
        if not self._cwa_client:
            return None
        return self._cwa_client.get_book_uuid(calibre_id)

    def download_kepub(self, book_id: str) -> bytes | None:
        """Download the actual converted KePub CWA serves to a real Kobo device.

        Used to read kepubify's real embedded span IDs — CWA stores whatever
        Location a client sends verbatim (no server-side validation), so a
        span ID only works if it matches what's really in the file Nickel
        has on-device. This hits the same endpoint a real device does
        (Calibre-Web's /kobo/<token>/download/<book_id>/<format>), which
        converts EPUB -> KEPUB on the fly via kepubify if not already cached.
        """
        if not self.is_configured() or not book_id:
            return None

        try:
            url = f"{self._server}/kobo/{self._token}/download/{book_id}/kepub"
            r = self._session.get(url, timeout=30)
            if r.status_code != 200:
                logger.debug(f"📖 CWA Sync: kepub download for {book_id} returned {r.status_code}")
                return None
            return r.content
        except Exception as e:
            logger.debug(f"📖 CWA Sync: kepub download failed for {book_id}: {e}")
            return None
