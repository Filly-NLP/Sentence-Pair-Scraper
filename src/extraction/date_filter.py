from datetime import datetime
from typing import Optional

class DateFilter:
    def __init__(self, cutoff_date: datetime):
        self.cutoff_date = cutoff_date

    def parse_date(self, date_str: str) -> Optional[datetime]:
        """Convert ISO 8601 publication timestamps to python datetimes."""
        if not date_str:
            return None
        
        # Clean string
        date_str = date_str.strip()
        
        # Handle trailing Z or offsets
        try:
            if "T" in date_str:
                return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            else:
                return datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            # Fallback formatting formats common in RSS feeds
            for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(date_str, fmt)
                except Exception:
                    pass
        return None

    def is_valid(self, pub_date: Optional[datetime]) -> bool:
        """Reject articles published before January 1, 2022."""
        if not pub_date:
            return False
        
        # Strip timezone offsets for comparison uniformity (UTC baseline)
        if pub_date.tzinfo is not None:
            pub_date = pub_date.replace(tzinfo=None)
            
        return pub_date >= self.cutoff_date
