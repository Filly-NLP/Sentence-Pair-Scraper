import yaml
from pathlib import Path
from typing import Any, Dict

class CrawlerConfig:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Crawler configuration file not found at: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}

    def get(self, key: str, default: Any = None) -> Any:
        # Support dot-notation access (e.g. get("http.user_agent"))
        parts = key.split(".")
        val = self.config
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return default
            if val is None:
                return default
        return val
