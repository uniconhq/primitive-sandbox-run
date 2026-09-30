"""primitive.yaml against the runner's contract."""

from pathlib import Path
from typing import Any

import check_declaration


def test_the_declaration_conforms(schema: dict[str, Any]) -> None:
    """The repo's primitive.yaml, with a placeholder digest, is a valid declaration."""
    assert check_declaration.problems(schema) == []


def test_an_image_line_is_refused(schema: dict[str, Any], tmp_path: Path) -> None:
    """The image comes from the release manifest, never from the repo file."""
    root = check_declaration.ROOT
    text = (root / "primitive.yaml").read_text()
    (tmp_path / "primitive.yaml").write_text(
        text + "image: ghcr.io/x/y@sha256:" + "1" * 64
    )
    (tmp_path / "Dockerfile").write_text((root / "Dockerfile").read_text())
    found = check_declaration.problems(schema, tmp_path)
    assert found == [
        "primitive.yaml has an image line; the release manifest supplies it"
    ]


def test_the_entrypoint_is_the_dockerfiles(
    schema: dict[str, Any], tmp_path: Path
) -> None:
    """A declared entrypoint the image does not start is caught."""
    root = check_declaration.ROOT
    (tmp_path / "primitive.yaml").write_text((root / "primitive.yaml").read_text())
    (tmp_path / "Dockerfile").write_text('FROM scratch\nENTRYPOINT ["/elsewhere"]\n')
    found = check_declaration.problems(schema, tmp_path)
    assert len(found) == 1
    assert "is not the Dockerfile's ['/elsewhere']" in found[0]
