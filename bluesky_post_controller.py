# bluesky_post_controller.py
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PostDecision:
    allow: bool
    reason: str  # "ok" or a suppression reason code
    retry_after_s: int = 0  # suggested wait time if suppressed


class BlueskyPostController:
    """
    A small policy gate between "event detected" and "publish to Bluesky".

    Key features:
      - startup grace period (no posts for N seconds after app start)
      - rolling rate limits: per 15 minutes, per hour, per day
      - dedupe window (e.g., treat a storm as one post for X minutes)
      - persistent state in JSON so limits survive restarts
      - optional dry_run (never allows posting, but returns decisions)
    """

    def __init__(
        self,
        *,
        state_path: str | Path = "state/posting_state.json",
        startup_grace_s: int = 15 * 60,
        max_per_15m: int = 1,
        max_per_hour: int = 3,
        max_per_day: int = 10,
        dedupe_window_s: int = 20 * 60,
        dry_run: bool = False,
        time_fn=time.time,
    ) -> None:
        self.state_path = Path(state_path)
        self.startup_grace_s = int(startup_grace_s)
        self.max_per_15m = int(max_per_15m)
        self.max_per_hour = int(max_per_hour)
        self.max_per_day = int(max_per_day)
        self.dedupe_window_s = int(dedupe_window_s)
        self.dry_run = bool(dry_run)
        self._time = time_fn

        self._started_at = int(self._time())
        self._state: Dict[str, Any] = self._load_state()

    # -------------------------
    # Public API
    # -------------------------

    def should_post(self, event: Dict[str, Any]) -> PostDecision:
        """
        Decide whether we should publish this event.

        Expected event fields (flexible):
          - "type": e.g. "lightning"
          - "timestamp": optional unix seconds (defaults to now)
          - "dedupe_key": optional stable key (defaults to event["type"])
        """
        now = int(event.get("timestamp") or self._time())

        # 1) Dry run = never post
        if self.dry_run:
            return PostDecision(False, "dry_run", retry_after_s=0)

        # 2) Startup grace
        uptime = now - self._started_at
        if uptime < self.startup_grace_s:
            return PostDecision(
                False,
                "startup_grace",
                retry_after_s=self.startup_grace_s - uptime,
            )

        # Normalize and prune state timestamps
        self._prune(now)

        # 3) Dedupe (storm window)
        dedupe_key = str(event.get("dedupe_key") or event.get("type") or "event")
        last_by_key = self._state.get("last_post_by_key", {})
        last_ts = int(last_by_key.get(dedupe_key) or 0)
        if last_ts and (now - last_ts) < self.dedupe_window_s:
            return PostDecision(
                False,
                "dedupe_window",
                retry_after_s=self.dedupe_window_s - (now - last_ts),
            )

        # 4) Rate limits
        posts_15m = self._count_since(now, 15 * 60)
        if posts_15m >= self.max_per_15m:
            return PostDecision(False, "rate_limit_15m", retry_after_s=self._retry_after(now, 15 * 60))

        posts_hour = self._count_since(now, 60 * 60)
        if posts_hour >= self.max_per_hour:
            return PostDecision(False, "rate_limit_hour", retry_after_s=self._retry_after(now, 60 * 60))

        posts_day = self._count_since(now, 24 * 60 * 60)
        if posts_day >= self.max_per_day:
            return PostDecision(False, "rate_limit_day", retry_after_s=self._retry_after(now, 24 * 60 * 60))

        return PostDecision(True, "ok", retry_after_s=0)

    def record_post(self, event: Dict[str, Any]) -> None:
        """
        Call this immediately AFTER a successful post to Bluesky.
        Persists state so restarts won't spam.
        """
        now = int(event.get("timestamp") or self._time())
        dedupe_key = str(event.get("dedupe_key") or event.get("type") or "event")

        self._state.setdefault("post_timestamps", [])
        self._state["post_timestamps"].append(now)

        self._state.setdefault("last_post_by_key", {})
        self._state["last_post_by_key"][dedupe_key] = now

        self._state["last_post_at"] = now
        self._save_state()

    def reset_state(self) -> None:
        """Dangerous: clears posting memory. Useful for testing only."""
        self._state = {"post_timestamps": [], "last_post_by_key": {}, "last_post_at": 0}
        self._save_state()

    # -------------------------
    # Internals
    # -------------------------

    def _count_since(self, now: int, window_s: int) -> int:
        cutoff = now - window_s
        return sum(1 for ts in self._state.get("post_timestamps", []) if int(ts) >= cutoff)

    def _retry_after(self, now: int, window_s: int) -> int:
        """
        Estimate when the oldest post in the window falls out of the window.
        """
        cutoff = now - window_s
        in_window = sorted(int(ts) for ts in self._state.get("post_timestamps", []) if int(ts) >= cutoff)
        if not in_window:
            return 0
        oldest = in_window[0]
        return max(0, window_s - (now - oldest))

    def _prune(self, now: int) -> None:
        """
        Keep only last 24h timestamps (since max_per_day uses 24h).
        """
        cutoff = now - 24 * 60 * 60
        pts = [int(ts) for ts in self._state.get("post_timestamps", [])]
        self._state["post_timestamps"] = [ts for ts in pts if ts >= cutoff]

        # Optional: prune old last_post_by_key entries (not strictly necessary)
        lpk = self._state.get("last_post_by_key", {})
        if isinstance(lpk, dict):
            # If a key hasn't posted in 7 days, forget it
            key_cutoff = now - 7 * 24 * 60 * 60
            self._state["last_post_by_key"] = {k: int(v) for k, v in lpk.items() if int(v) >= key_cutoff}

    def _load_state(self) -> Dict[str, Any]:
        try:
            if self.state_path.exists():
                with self.state_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        # Ensure expected fields exist
                        data.setdefault("post_timestamps", [])
                        data.setdefault("last_post_by_key", {})
                        data.setdefault("last_post_at", 0)
                        return data
        except Exception:
            # If state file is corrupted, start clean (but don't crash the app)
            pass
        return {"post_timestamps": [], "last_post_by_key": {}, "last_post_at": 0}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")

        payload = {
            "post_timestamps": [int(ts) for ts in self._state.get("post_timestamps", [])],
            "last_post_by_key": {str(k): int(v) for k, v in self._state.get("last_post_by_key", {}).items()},
            "last_post_at": int(self._state.get("last_post_at") or 0),
        }

        # Atomic-ish write: write temp then replace
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp_path, self.state_path)