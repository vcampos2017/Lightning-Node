# noaa_check.py
"""
NOAA / NWS storm plausibility check using api.weather.gov (Weather.gov API).

Purpose:
- Provide a "storm plausible now?" signal to corroborate lightning detector triggers.
- This does NOT confirm lightning strikes; it checks thunderstorm conditions/likelihood.

Key signals:
1) Active NWS alerts affecting the location (strongest).
2) Hourly forecast mentions thunderstorms in the next N hours (anticipatory).

Usage (recommended: keep location out of git-tracked files):
    import os
    from noaa_check import NOAAStormChecker, NOAAConfig

    checker = NOAAStormChecker(
        NOAAConfig(user_agent=os.environ["NOAA_USER_AGENT"])
    )

    lat = float(os.environ["NOAA_LAT"])
    lon = float(os.environ["NOAA_LON"])

    result = checker.check_storm_plausibility(lat=lat, lon=lon)
    print(result.to_dict())

Environment variables:
- NOAA_USER_AGENT: e.g., "LightningDetector/1.0 (contact@example.com)"
- NOAA_LAT: latitude (float)
- NOAA_LON: longitude (float)

Notes:
- Respect NWS guidance: always send a descriptive User-Agent with contact info.
- This module is designed to be called on-demand (e.g., when your sensor triggers),
  not polled continuously.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests


# ---------------------------
# Configuration and results
# ---------------------------

@dataclass(frozen=True)
class NOAAConfig:
    """
    user_agent: REQUIRED by NWS policy; include an email or URL for contact.
    timeout_s:  reasonable network timeout.
    thunder_keywords: keywords used to identify thunder in shortForecast/detailedForecast.
    forecast_hours_ahead: look-ahead window for hourly forecast scan.
    """
    user_agent: str
    timeout_s: float = 8.0
    forecast_hours_ahead: int = 2
    thunder_keywords: Tuple[str, ...] = (
        "thunderstorm", "t-storm", "tstorm", "thunder",
    )

    # Alert event names that count as "storm-positive".
    # Tune this list to your needs.
    alert_event_whitelist: Tuple[str, ...] = (
        "Severe Thunderstorm Warning",
        "Severe Thunderstorm Watch",
        "Special Weather Statement",
        "Flash Flood Warning",
        "Flash Flood Watch",
        "Flood Advisory",
        "Flood Warning",
        "Tornado Warning",
        "Tornado Watch",
    )


@dataclass
class NOAAStormResult:
    """
    storm_positive: final gate result.
    score: heuristic score (higher = stronger corroboration).
    reasons: human-readable reasons for the decision.
    alerts: subset of alert details that triggered/appeared.
    forecast_hits: forecast periods that matched thunder keywords.
    fetched_at_utc: timestamp.
    """
    storm_positive: bool
    score: int
    reasons: List[str]
    alerts: List[Dict[str, Any]]
    forecast_hits: List[Dict[str, Any]]
    fetched_at_utc: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------
# NOAA Storm Checker
# ---------------------------

class NOAAStormChecker:
    """
    Implements:
    - /points/{lat},{lon} bootstrap (and caches key URLs)
    - active alerts query using the alerts endpoint and point filter
    - hourly forecast scan for thunder keywords
    """

    BASE = "https://api.weather.gov"

    def __init__(self, config: NOAAConfig) -> None:
        # Soft validation: NWS asks for a UA with contact info. Encourage good hygiene.
        if not config.user_agent or (("@" not in config.user_agent) and ("http" not in config.user_agent)):
            raise ValueError(
                "NOAAConfig.user_agent must include contact info, e.g. "
                "'LightningDetector/1.0 (contact@example.com)'."
            )

        self.config = config
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept": "application/geo+json, application/json",
        })

        # Simple in-memory cache for points lookups (per rounded lat/lon).
        self._points_cache: Dict[str, Dict[str, Any]] = {}

    def check_storm_plausibility(self, lat: float, lon: float) -> NOAAStormResult:
        """
        Returns storm_positive True if:
        - Storm-related NWS alerts are active for the point, OR
        - Hourly forecast mentions thunder within forecast_hours_ahead.
        """
        fetched_at = datetime.now(timezone.utc).isoformat()

        reasons: List[str] = []
        alerts: List[Dict[str, Any]] = []
        forecast_hits: List[Dict[str, Any]] = []
        score = 0

        points = self._get_points(lat, lon)

        # 1) Alerts (strong corroboration)
        alert_items = self._get_active_alerts_for_point(lat, lon)
        filtered_alerts = self._filter_alerts(alert_items)

        if filtered_alerts:
            alerts = filtered_alerts
            score += 3
            reasons.append(f"Active NWS alerts found ({len(filtered_alerts)}).")

        # 2) Hourly forecast thunder keywords (anticipatory corroboration)
        hourly_url = points.get("properties", {}).get("forecastHourly")
        if hourly_url:
            hits = self._scan_hourly_forecast_for_thunder(
                hourly_url,
                hours_ahead=self.config.forecast_hours_ahead
            )
            if hits:
                forecast_hits = hits
                score += 1
                reasons.append(
                    f"Hourly forecast mentions thunder within {self.config.forecast_hours_ahead}h."
                )
        else:
            reasons.append("No forecastHourly URL available from /points response.")

        # Decision: alerts alone should be sufficient; forecast hits alone is supportive.
        storm_positive = bool(alerts) or bool(forecast_hits)

        if storm_positive:
            reasons.append("NOAA storm plausibility: POSITIVE (storm conditions likely).")
        else:
            reasons.append("NOAA storm plausibility: NEGATIVE (no storm signals detected).")

        return NOAAStormResult(
            storm_positive=storm_positive,
            score=score,
            reasons=reasons,
            alerts=alerts,
            forecast_hits=forecast_hits,
            fetched_at_utc=fetched_at,
        )

    # ---------------------------
    # Internal helpers
    # ---------------------------

    def _get_points(self, lat: float, lon: float) -> Dict[str, Any]:
        # Round to reduce cache key churn and avoid storing overly precise location in memory logs.
        key = f"{lat:.4f},{lon:.4f}"
        if key in self._points_cache:
            return self._points_cache[key]

        url = f"{self.BASE}/points/{lat:.4f},{lon:.4f}"
        data = self._get_json(url)

        # Cache response; small and stable.
        self._points_cache[key] = data
        return data

    def _get_active_alerts_for_point(self, lat: float, lon: float) -> List[Dict[str, Any]]:
        """
        Uses alerts endpoint filtered by point. Returns raw 'features' items.
        """
        # /alerts/active?point=lat,lon
        url = f"{self.BASE}/alerts/active"
        params = {"point": f"{lat:.4f},{lon:.4f}"}
        data = self._get_json(url, params=params)
        features = data.get("features", [])
        return features if isinstance(features, list) else []

    def _filter_alerts(self, alert_features: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filters alert features by event name whitelist. Returns simplified objects.
        """
        wh = set(self.config.alert_event_whitelist)
        out: List[Dict[str, Any]] = []

        for feat in alert_features:
            props = (feat or {}).get("properties", {}) or {}
            event = props.get("event", "") or ""
            if event in wh:
                out.append({
                    "event": event,
                    "headline": props.get("headline"),
                    "severity": props.get("severity"),
                    "certainty": props.get("certainty"),
                    "urgency": props.get("urgency"),
                    "effective": props.get("effective"),
                    "expires": props.get("expires"),
                    "id": (feat or {}).get("id"),
                })

        return out

    def _scan_hourly_forecast_for_thunder(self, forecast_hourly_url: str, hours_ahead: int) -> List[Dict[str, Any]]:
        """
        Reads hourly forecast periods and returns those within the window that match thunder keywords.
        """
        data = self._get_json(forecast_hourly_url)
        periods = data.get("properties", {}).get("periods", [])
        if not isinstance(periods, list):
            return []

        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        keywords = tuple(k.lower() for k in self.config.thunder_keywords)

        hits: List[Dict[str, Any]] = []
        for p in periods:
            start_time_str = p.get("startTime")
            short_fc = (p.get("shortForecast") or "").strip()
            detailed_fc = (p.get("detailedForecast") or "").strip()

            start_dt = self._parse_iso8601(start_time_str)
            if start_dt is None:
                continue

            # Normalize to UTC if timezone-aware; if naive, treat as UTC.
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            start_dt_utc = start_dt.astimezone(timezone.utc)

            if start_dt_utc > cutoff:
                break  # periods are in chronological order

            text = f"{short_fc} {detailed_fc}".lower()
            if any(k in text for k in keywords):
                hits.append({
                    "startTime": start_time_str,
                    "temperature": p.get("temperature"),
                    "windSpeed": p.get("windSpeed"),
                    "shortForecast": short_fc,
                })

        return hits

    def _get_json(self, url: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        GET JSON with basic error handling.
        Returns an empty object with _error fields on failure.
        """
        try:
            resp = self._session.get(url, params=params, timeout=self.config.timeout_s)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            # Fail "closed" (no storm) by returning error info; caller treats as negative.
            return {"_error": str(e), "_url": url, "_params": params or {}}
        except ValueError as e:
            return {"_error": f"Invalid JSON: {e}", "_url": url, "_params": params or {}}

    @staticmethod
    def _parse_iso8601(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            # Handle trailing Z
            if s.endswith("Z"):
                s = s.replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        except Exception:
            return None