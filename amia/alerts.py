"""Alerts as GitHub issues (GitHub emails the repo owner). Deduplicated by title while open."""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sent: list[str] = []

    def alert(self, title: str, body: str) -> None:
        self.sent.append(title)
        log.warning("ALERT: %s | %s", title, body)
        s = self.settings
        if not (s.github_token and s.github_repository):
            return
        api = f"https://api.github.com/repos/{s.github_repository}/issues"
        headers = {"Authorization": f"Bearer {s.github_token}", "Accept": "application/vnd.github+json"}
        try:
            existing = httpx.get(api, params={"state": "open", "labels": "amia-alert", "per_page": 100}, headers=headers, timeout=30).json()
            for issue in existing if isinstance(existing, list) else []:
                if issue.get("title") == title:
                    httpx.post(f"{api}/{issue['number']}/comments", json={"body": body}, headers=headers, timeout=30)
                    return
            httpx.post(api, json={"title": title, "body": body, "labels": ["amia-alert"]}, headers=headers, timeout=30)
        except Exception as e:  # alerts must never crash the pipeline
            log.error("Could not create GitHub alert: %s", e)
