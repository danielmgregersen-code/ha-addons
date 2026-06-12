import os
import requests


class HAClient:
    """Read entity states from Home Assistant via the Supervisor core proxy.

    Uses the SUPERVISOR_TOKEN provided to add-ons at runtime. No-op (returns
    None) when the token is unavailable, so callers degrade gracefully when
    running outside a Supervisor environment.
    """

    BASE_URL = "http://supervisor/core/api"

    def __init__(self, token: str = None):
        self.token = token or os.getenv("SUPERVISOR_TOKEN", "")

    def get_state(self, entity_id: str) -> dict | None:
        """Fetch a single entity's state. Returns a trimmed dict, or None if
        not configured / unavailable / the request fails."""
        if not self.token or not entity_id:
            return None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            r = requests.get(
                f"{self.BASE_URL}/states/{entity_id}",
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
        except requests.RequestException:
            return None
        data = r.json()
        return {
            "entity_id": data.get("entity_id"),
            "state": data.get("state"),
            "attributes": data.get("attributes", {}),
            "last_updated": data.get("last_updated"),
        }
