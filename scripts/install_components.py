#!/usr/bin/env python3
"""Install a pinned component, or all four general-purpose H3 additions."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.catalog import MediaCatalog
from engine.config import Settings

async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("components", nargs="*", default=[])
    args = parser.parse_args()
    settings = Settings(); settings.prepare()
    catalog = MediaCatalog(settings)
    ids = args.components or [x["id"] for x in catalog.manifest["components"]]
    for ident in ids:
        async def progress(value, phase):
            print(f"{ident}: {value:.0%} {phase}", flush=True)
        print(await catalog.install(ident, progress), flush=True)

if __name__ == "__main__":
    asyncio.run(main())
