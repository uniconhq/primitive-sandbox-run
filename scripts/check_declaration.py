"""Check primitive.yaml against the runner's primitive contract.

    uv run python scripts/check_declaration.py PRIMITIVE_SCHEMA_JSON

The schema is `primitive.schema.json` from the runner release this repo is
built against. `primitive.yaml` in this repo has no image line: bootstrap
writes the image by digest from the release manifest into the version it
creates at the forge. So this check refuses an image line, fills in a
placeholder digest, validates the result against the schema's declaration,
and checks that the declared entrypoint is the one the Dockerfile starts.
Exits 1 and lists every problem when there is one.
"""

import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


def load_declaration(root: Path = ROOT) -> dict[str, Any]:
    """Read primitive.yaml as it is in the repo."""
    document = yaml.safe_load((root / "primitive.yaml").read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("primitive.yaml is not a mapping")
    return document


def with_placeholder_image(declaration: dict[str, Any]) -> dict[str, Any]:
    """The declaration with the image line bootstrap would write, by a fake digest."""
    name = str(declaration.get("name", "")).rpartition("/")[2]
    image = f"ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}"
    return {**declaration, "image": image}


def dockerfile_entrypoint(root: Path = ROOT) -> list[str]:
    """The exec-form ENTRYPOINT of the image's last stage."""
    lines = (root / "Dockerfile").read_text(encoding="utf-8").splitlines()
    entrypoints = [line for line in lines if line.startswith("ENTRYPOINT ")]
    if not entrypoints:
        return []
    value = json.loads(entrypoints[-1].removeprefix("ENTRYPOINT "))
    return [str(part) for part in value]


def problems(schema: dict[str, Any], root: Path = ROOT) -> list[str]:
    """Everything wrong with this repo's declaration, one line each."""
    declaration = load_declaration(root)
    found = []
    if "image" in declaration:
        found.append(
            "primitive.yaml has an image line; the release manifest supplies it"
        )
    registry: Registry[Any] = Registry().with_resource(
        schema["$id"], Resource.from_contents(schema)
    )
    validator = Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/declaration"}, registry=registry
    )
    for error in validator.iter_errors(with_placeholder_image(declaration)):
        where = "/".join(str(part) for part in error.absolute_path) or "(top)"
        found.append(f"{where}: {error.message}")
    declared, started = declaration.get("entrypoint"), dockerfile_entrypoint(root)
    if declared != started:
        found.append(f"entrypoint {declared} is not the Dockerfile's {started}")
    return found


def main(argv: list[str]) -> int:
    """Check the declaration against the schema file named on the command line."""
    if len(argv) != 2:
        print("usage: check_declaration.py PRIMITIVE_SCHEMA_JSON", file=sys.stderr)
        return 2
    schema = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    found = problems(schema)
    for problem in found:
        print(f"primitive.yaml: {problem}", file=sys.stderr)
    if found:
        return 1
    print("primitive.yaml conforms to the declaration in primitive.schema.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
