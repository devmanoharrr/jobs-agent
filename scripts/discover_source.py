"""Identify the ATS for one career URL. Does not enable a source or walk the site.

python -m scripts.discover_source https://company.example/careers
"""

import argparse
import asyncio
import sys

from app.discovery.ats_detector import detect
from app.discovery.fetch import DiscoveryError, fetch_career_page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Identify the ATS behind one career URL.")
    parser.add_argument("url")
    args = parser.parse_args(argv)
    try:
        html = asyncio.run(fetch_career_page(args.url))
    except DiscoveryError as exc:
        print(exc, file=sys.stderr)
        return 1
    result = detect(args.url, html)
    source_type = result.source_type or "unsupported"
    print(f"status: {result.status}")
    print(f"source_type: {source_type}")
    print(f"external_key: {result.external_key or ''}")
    print(f"evidence: {result.evidence}")
    print("enabled: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
