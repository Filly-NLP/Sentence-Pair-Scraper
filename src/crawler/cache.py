import os
import json
import hashlib
from typing import Dict, Any, Optional

class HTTPCache:
    def __init__(self, cache_dir: str = "data/cache"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cache_paths(self, url: str) -> tuple[str, str]:
        # Hash URL to generate secure filename
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        meta_path = os.path.join(self.cache_dir, f"{url_hash}.meta")
        body_path = os.path.join(self.cache_dir, f"{url_hash}.html")
        return meta_path, body_path

    def get(self, url: str) -> Optional[Dict[str, Any]]:
        meta_path, body_path = self._get_cache_paths(url)
        if not os.path.exists(meta_path) or not os.path.exists(body_path):
            return None
        
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            with open(body_path, "r", encoding="utf-8") as f:
                html = f.read()
            return {"meta": meta, "html": html}
        except Exception:
            return None

    def set(self, url: str, html: str, headers: Dict[str, str]) -> None:
        meta_path, body_path = self._get_cache_paths(url)
        # Extract headers helpful for conditional requests
        meta = {
            "etag": headers.get("etag") or headers.get("ETag"),
            "last_modified": headers.get("last-modified") or headers.get("Last-Modified"),
            "fetched_at": datetime.utcnow().isoformat()
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
            with open(body_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass
from datetime import datetime
