from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import TrackerConfig


class ConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> TrackerConfig:
        if not self.path.exists():
            return TrackerConfig()
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
            return TrackerConfig(
                growth_threshold=float(values.get("growth_threshold", 0.07)),
                moh30_threshold=float(values.get("moh30_threshold", 2.5)),
                moh90_threshold=float(values.get("moh90_threshold", 2.5)),
                beijing_codes=tuple(str(code).strip() for code in values.get("beijing_codes", TrackerConfig().beijing_codes)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return TrackerConfig()

    def save(self, config: TrackerConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
