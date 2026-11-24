"""Capture ONE live response per keyed API for the fixtures (#19). The PM runs it from the main
checkout with the real keys in the environment (`set -a; . ./.env; set +a`); tests never do.

    uv run python scripts/capture_keyed_fixtures.py [adzuna|reed|itjobs ...]

Writes tests/fixtures/<api>/live-<n>.json with every key removed (the same scrub the connectors
use), at most 150 KB each, and prints only file names.
One call per API, outside the DB call budget: run it once.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cvboost.connectors import REGISTRY
from cvboost.connectors.keyed import KeyedAPIConnector, scrub_payload
from cvboost.http import HttpClient

OUT = Path("tests/fixtures")
MAX_BYTES = 150 * 1024
PICK = {"adzuna": 0, "reed": 0, "itjobs": 0}  # index into plan(): the first query of each


def main(names: list[str]) -> int:
    http = HttpClient()
    try:
        for name in names or list(PICK):
            c = REGISTRY[name]()
            assert isinstance(c, KeyedAPIConnector)
            creds = c.credentials()
            if creds is None:
                print(f"{name}: not configured, skipped")
                continue
            call = c.plan()[PICK[name]]
            params, headers = c.auth(creds)
            try:
                resp = http.get(
                    call.url,
                    params={**call.params, "results_per_page": "10", "resultsToTake": "10"}
                    | ({"limit": "10"} if name == "itjobs" else {})
                    | params,
                    headers=headers,
                    min_delay_s=c.min_delay_s,
                    allowed_hosts=c.allowed_hosts,
                )
            except Exception as exc:  # never print the raw error: it may carry the URL
                print(f"{name}: failed ({type(exc).__name__})")
                continue
            data = scrub_payload(json.loads(resp.content), c.secrets())
            text = json.dumps(data, ensure_ascii=False, indent=1)
            if len(text.encode()) > MAX_BYTES:
                print(f"{name}: response over {MAX_BYTES} bytes, not written")
                continue
            path = OUT / name / "live-1.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"{name}: wrote {path}")
    finally:
        http.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
