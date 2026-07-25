"""Load the example data models from examples/models.yaml.

Definitions only — nothing is published, so no physical tables are created
until an administrator reviews and publishes each entity.

    python -m scripts.seed
"""
import pathlib
import sys
from typing import List

from app.db import session_scope
from app.services.model_io import (
    apply_import,
    parse_document,
    validate_document,
)

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples" / "models.yaml"


def seed_models(path: pathlib.Path = EXAMPLES, actor: str = "seed") -> List[str]:
    if not path.exists():
        return [f"no example file at {path}"]

    defs, errors = validate_document(parse_document(path.read_text()))
    blocking = [e for e in errors if "warning" not in e.lower()]
    if blocking:
        raise SystemExit("Example models failed validation:\n  "
                         + "\n  ".join(blocking))

    lines: List[str] = []
    with session_scope() as db:
        results = apply_import(
            db, [dict(d, attributes=list(d["attributes"])) for d in defs],
            actor=actor,
        )
    for r in results:
        lines.append(f"{r['entity']}: {r['action']} "
                     f"({r['attributes']} attributes, v{r['version']})")
    return lines


def main() -> int:
    for line in seed_models():
        print(line)
    print("\nDefinitions loaded as drafts. Publish each entity to create its "
          "physical tables.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
