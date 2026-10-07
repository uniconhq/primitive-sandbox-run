"""Fixtures for running the image the way the harness does, and the contract checks.

The image tests build the image from this checkout (or use the one named by
`PRIMITIVE_IMAGE`) and start it with the flags every step container gets: no
network, a read-only root, every capability dropped, no new privileges,
Docker's built-in seccomp profile, user 65532, no swap, the declared memory
and pids limits, the CPU-time and file-size limits as `RLIMIT_CPU` and
`RLIMIT_FSIZE`, one CPU, a small noexec tmpfs at /tmp and the working
directory at /work. They are skipped when Docker is not reachable.

The contract checks use `primitive.schema.json` from `PRIMITIVE_SCHEMA`, or
from a runner checkout beside this one, when it is the version 5 contract.

Native and Java binaries come from the compile primitive's image, named by
`COMPILE_IMAGE` or built from a primitive-compile checkout beside this one;
the tests that need one are skipped when there is neither. The in-process
tests import the program, which needs Linux, so elsewhere only the image
tests are collected. The declaration is checked by the runner's shared
primitive CI.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from support import (
    NAME,
    ROOT,
    SCHEMA_VERSION,
    SIBLING_COMPILE,
    SIBLING_SCHEMA,
    Check,
    Compiled,
    RunImage,
    declaration,
    open_up,
    sandbox_flags,
)

collect_ignore = [] if sys.platform == "linux" else ["test_sandbox_run.py"]


@pytest.fixture(scope="session")
def image() -> str:
    """The image under test: `PRIMITIVE_IMAGE`, or built from this checkout."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker is not reachable")
    named = os.environ.get("PRIMITIVE_IMAGE")
    if named:
        return named
    tag = f"{NAME}:test"
    subprocess.run(["docker", "build", "--quiet", "--tag", tag, str(ROOT)], check=True)
    return tag


@pytest.fixture
def run_image(image: str) -> RunImage:
    """Run the image once over a working directory and return its outputs.json.

    The directory is copied into a Docker volume, run there and copied back,
    because the harness gives a step a volume: Landlock's rules hold on the
    volume's filesystem, and not on a bind mount of a directory Docker
    Desktop shares from another operating system, whose files it cannot tell
    apart from one open to the next.
    """

    def run(
        work: Path,
        limits: dict[str, int] | None = None,
        seconds: list[float] | None = None,
    ) -> dict[str, Any]:
        """Run the batch in `work`; `seconds`, when given, gets how long the
        sandboxed container took, from `docker run` to its exit."""
        limits = limits or declaration()["limits"]
        open_up(work)
        volume = f"{NAME}-test-{secrets.token_hex(4)}"
        docker("volume", "create", volume)
        try:
            holder = docker("create", "--volume", f"{volume}:/work", image).strip()
            try:
                docker("cp", f"{work}/.", f"{holder}:/work")
                docker(
                    "run",
                    "--rm",
                    "--user=0:0",
                    "--entrypoint=chmod",
                    "--volume",
                    f"{volume}:/work",
                    image,
                    "-R",
                    "a+rwX",
                    "/work",
                )
                command = ["docker", "run", "--rm", *sandbox_flags(limits)]
                command += ["--volume", f"{volume}:/work", image]
                started = time.monotonic()
                subprocess.run(
                    command, check=True, timeout=limits["time_ms"] / 1000 + 60
                )
                if seconds is not None:
                    seconds.append(time.monotonic() - started)
                docker("cp", f"{holder}:/work/.", str(work))
            finally:
                docker("rm", "--force", holder)
        finally:
            docker("volume", "rm", "--force", volume)
        document: dict[str, Any] = json.loads(
            (work / "outputs.json").read_text(encoding="utf-8")
        )
        return document

    return run


def docker(*arguments: str) -> str:
    """Run one docker command, raising with its error output when it fails."""
    done = subprocess.run(["docker", *arguments], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"docker {arguments[0]} failed: {done.stderr.strip()}")
    return done.stdout


@pytest.fixture(scope="session")
def compile_image(image: str) -> str:
    """The compile primitive's image: `COMPILE_IMAGE`, or built from its checkout."""
    named = os.environ.get("COMPILE_IMAGE")
    if named:
        return named
    if not (SIBLING_COMPILE / "Dockerfile").is_file():
        pytest.skip("no compile image to build binaries with; set COMPILE_IMAGE")
    tag = "primitive-compile:test"
    command = ["docker", "build", "--quiet", "--tag", tag, str(SIBLING_COMPILE)]
    subprocess.run(command, check=True)
    return tag


@pytest.fixture
def compiled(compile_image: str, tmp_path_factory: pytest.TempPathFactory) -> Compiled:
    """Compile a source with the compile primitive and return the binary.

    The source goes in as compile's `source` folder holding the one file, in
    contract version 5. A compile image that answers it speaks another
    version, as the compile v1 image speaks 4 with `source` a file, is asked
    again in that shape, so these tests run against either.
    """

    def build(language: str, name: str, source: str) -> bytes:
        work = source_in(tmp_path_factory, name, source)
        inputs = {"source": {"folder": "in/1/source"}, "language": language}
        result = compile_once(
            compile_image, work, {"schema_version": SCHEMA_VERSION, "inputs": inputs}
        )
        if "contract version" in result.get("error", ""):
            # A directory of its own: what the first run wrote belongs to the
            # container's user, which this one cannot open up again.
            work = source_in(tmp_path_factory, name, source)
            inputs = {"source": {"file": f"in/1/source/{name}"}, "language": language}
            document = {"schema_version": 4, "inputs": inputs}
            result = compile_once(compile_image, work, document)
        assert result["outputs"]["outcome"] == "accepted", result
        return (work / str(result["outputs"]["binary"]["file"])).read_bytes()

    return build


def source_in(factory: pytest.TempPathFactory, name: str, source: str) -> Path:
    """A fresh working directory holding the source as `in/1/source/<name>`."""
    work = factory.mktemp("compile")
    folder = work / "in" / "1" / "source"
    folder.mkdir(parents=True)
    (folder / name).write_text(source)
    return work


def compile_once(
    compile_image: str, work: Path, document: dict[str, Any]
) -> dict[str, Any]:
    """Run the compile image once over `work` with `document` as inputs.json."""
    (work / "inputs.json").write_text(json.dumps(document))
    open_up(work)
    flags = sandbox_flags(
        {"cpu_ms": 60000, "memory_mb": 1024, "pids": 128, "output_mb": 64}
    )
    command = ["docker", "run", "--rm", *flags, "--volume", f"{work}:/work"]
    subprocess.run([*command, compile_image], check=True, timeout=120)
    result: dict[str, Any] = json.loads(
        (work / "outputs.json").read_text(encoding="utf-8")
    )
    return result


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    """The runner's primitive.schema.json at contract version 5."""
    named = os.environ.get("PRIMITIVE_SCHEMA")
    path = Path(named) if named else SIBLING_SCHEMA
    if not path.is_file():
        pytest.skip("no primitive.schema.json; set PRIMITIVE_SCHEMA")
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    inputs = document.get("$defs", {}).get("inputs_file", {})
    version = inputs.get("properties", {}).get("schema_version", {}).get("const")
    if version != SCHEMA_VERSION:
        pytest.skip(f"{path} is not the version {SCHEMA_VERSION} contract")
    return document


@pytest.fixture
def check(schema: dict[str, Any]) -> Check:
    """Validate a document against one part of the contract.

    For `outputs_file`, every entry must also carry each output the
    declaration names as required, and nothing it does not name.
    """
    registry: Registry[Any] = Registry().with_resource(
        schema["$id"], Resource.from_contents(schema)
    )

    def validate(document: dict[str, Any], part: str) -> None:
        reference = {"$ref": f"{schema['$id']}#/$defs/{part}"}
        Draft202012Validator(reference, registry=registry).validate(document)
        if part != "outputs_file" or "error" in document:
            return
        declared = declaration()["outputs"]
        required = {name for name, port in declared.items() if not port.get("optional")}
        if "batch" in document:
            entries = [entry["outputs"] for entry in document["batch"]]
        else:
            entries = [document["outputs"]]
        for outputs in entries:
            assert required <= set(outputs) <= set(declared), outputs

    return validate
