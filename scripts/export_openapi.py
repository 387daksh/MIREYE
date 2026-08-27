from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.main import app


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the current FastAPI OpenAPI contract.")
    parser.add_argument("output", nargs="?", default="build/openapi.json")
    output = Path(parser.parse_args().output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True), encoding="utf-8")
    print(output.resolve())


if __name__ == "__main__":
    main()
