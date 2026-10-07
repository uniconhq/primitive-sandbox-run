"""What the tests share: the declaration, the sandbox flags and test inputs."""

import json
import math
import zipapp
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
NAME = "primitive-sandbox-run"
SCHEMA_VERSION = 5
SIBLING_SCHEMA = ROOT.parent / "runner" / "schemas" / "primitive.schema.json"
SIBLING_COMPILE = ROOT.parent / "primitive-compile"

RunImage = Callable[..., dict[str, Any]]
Check = Callable[[dict[str, Any], str], None]
Compiled = Callable[[str, str, str], bytes]


def declaration() -> dict[str, Any]:
    """This repo's primitive.yaml."""
    document: dict[str, Any] = yaml.safe_load((ROOT / "primitive.yaml").read_text())
    return document


def container_limits(
    time_limit: float, memory_limit: float, items: int
) -> dict[str, int]:
    """The container limits the harness gives a batch, from the declaration.

    Each limit is raised by `limits_from` for the item's inputs, rounded up;
    a batch's time and CPU are the per-item value times the number of items.
    """
    document = declaration()
    inputs = {"time_limit": time_limit, "memory_limit": memory_limit}
    limits: dict[str, int] = {}
    for name, value in document["limits"].items():
        rule = document.get("limits_from", {}).get(name)
        if rule:
            raised = inputs[rule["input"]] * rule.get("scale", 1) + rule.get("add", 0)
            value = max(value, math.ceil(raised))
        limits[name] = value * items if name in ("time_ms", "cpu_ms") else value
    return limits


def sandbox_flags(limits: dict[str, int]) -> list[str]:
    """The docker run flags the harness gives a step container."""
    memory = f"{limits['memory_mb']}m"
    cpu_seconds = math.ceil(limits["cpu_ms"] / 1000)
    file_bytes = limits["output_mb"] * 1024 * 1024
    return [
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--security-opt=seccomp=builtin",
        "--user=65532:65532",
        f"--memory={memory}",
        f"--memory-swap={memory}",
        f"--pids-limit={limits['pids']}",
        f"--ulimit=cpu={cpu_seconds}:{cpu_seconds}",
        f"--ulimit=fsize={file_bytes}:{file_bytes}",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
    ]


def open_up(work: Path) -> None:
    """Let user 65532 in the container write the working directory and read `in/`."""
    for path in [work, *work.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)


def python_binary(path: Path, source: str) -> Path:
    """Build a Python binary in compile's format at its simplest: a zip
    application whose root `__main__.py` is the program itself, where compile
    puts a launcher that runs the entry from the folder under `source/`.
    """
    app = path.parent / (path.name + ".app")
    app.mkdir(parents=True)
    (app / "__main__.py").write_text(source)
    zipapp.create_archive(app, path, interpreter="/usr/bin/env python3")
    return path


def batch_inputs(
    work: Path,
    binary: bytes,
    items: list[tuple[str, str]],
    time_limit: float = 1.0,
    memory_limit: float = 64,
    args: dict[str, str] | None = None,
) -> None:
    """Write inputs.json, the binary and one input per item under `in/`, as the
    harness does: the binary is `in/1/binary` and the nth item's input
    `in/<n + 1>/input`.

    `items` are (test, standard input) pairs, all run under the same limits;
    `args` gives the `args` input of the tests it names, and the others have
    none.
    """
    (work / "in" / "1").mkdir(parents=True)
    (work / "in" / "1" / "binary").write_bytes(binary)
    batch = []
    for number, (test, stdin) in enumerate(items, 2):
        (work / "in" / str(number)).mkdir()
        (work / "in" / str(number) / "input").write_text(stdin)
        inputs: dict[str, Any] = {
            "binary": {"file": "in/1/binary"},
            "input": {"file": f"in/{number}/input"},
            "time_limit": time_limit,
            "memory_limit": memory_limit,
        }
        if args and test in args:
            inputs["args"] = args[test]
        batch.append({"test": test, "inputs": inputs})
    document = {"schema_version": SCHEMA_VERSION, "batch": batch}
    (work / "inputs.json").write_text(json.dumps(document))


def peek_input(work: str) -> str:
    """The standard input of a `peek` run after a first run: the paths it
    tries to read, which are the other run's output and input, the step's own
    files and directories, the rest of /tmp, and one system file.
    """
    paths = [
        f"{work}/out/1/output",
        f"{work}/in/2/input",
        f"{work}/inputs.json",
        f"{work}/out",
        work,
        "/tmp",
        "/etc/os-release",
    ]
    return "\n".join(["peek", *paths, ""])


def peeked(work: str) -> list[str]:
    """What a `peek` run prints when only the system file can be read."""
    return [
        f"refused {work}/out/1/output 13",
        f"refused {work}/in/2/input 13",
        f"refused {work}/inputs.json 13",
        f"refused {work}/out 13",
        f"refused {work} 13",
        "refused /tmp 13",
        "read /etc/os-release",
    ]


def by_test(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A batch's outputs keyed by test."""
    return {entry["test"]: entry["outputs"] for entry in result["batch"]}


def printed(work: Path, outputs: dict[str, Any]) -> str:
    """What one item's binary printed, from the file its `output` names."""
    return (work / str(outputs["output"]["file"])).read_text()


PROGRAM = """
import os, signal, sys, time
case = sys.stdin.readline().strip()
if case == "double":
    print(int(sys.stdin.readline()) * 2)
elif case == "spin":
    while True:
        pass
elif case == "sleep":
    time.sleep(60)
elif case == "grow":
    blocks = []
    while True:
        blocks.append(b"x" * (8 * 1024 * 1024))
elif case == "flood":
    line = "y" * 1023 + "\\n"
    while True:
        sys.stdout.write(line)
elif case == "fail":
    print("partial")
    sys.exit(3)
elif case == "argv":
    print(repr(sys.argv[1:]))
elif case == "escape":
    work = sys.stdin.readline().strip()
    for path in (work + "/escaped", work + "/inputs.json", "/tmp/escaped"):
        try:
            open(path, "w").close()
            print("wrote", path)
        except OSError as error:
            print("refused", path, error.errno)
    open("here", "w").write("fine")
    print("cwd", sorted(os.listdir(".")))
elif case == "peek":
    for path in sys.stdin.read().split():
        try:
            if os.path.isdir(path):
                os.listdir(path)
            else:
                open(path, "rb").close()
            print("read", path)
        except OSError as error:
            print("refused", path, error.errno)
elif case == "linger":
    if os.fork() == 0:
        os.setsid()
        time.sleep(60)
        os._exit(0)
    print("left one behind")
elif case == "census":
    print(len([p for p in os.listdir("/proc") if p.isdigit()]))
elif case == "parricide":
    try:
        os.kill(os.getppid(), 9)
    except OSError as error:
        print("refused", error.errno, flush=True)
        os._exit(1)
    print("after")
elif case == "freeze":
    try:
        os.kill(os.getppid(), signal.SIGSTOP)
    except OSError:
        pass
    while True:
        pass
elif case == "hidden-work":
    if os.fork() == 0:
        while True:
            pass
    time.sleep(30)
elif case == "hidden-memory":
    child = os.fork()
    if child == 0:
        blocks = []
        while True:
            blocks.append(b"x" * (8 * 1024 * 1024))
    os.waitpid(child, 0)
elif case in ("split-memory", "undumpable-split-memory"):
    if case.startswith("undumpable"):
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
    child = os.fork()
    block = b"x" * (40 * 1024 * 1024)
    time.sleep(0.5)
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)
    print("held", 2 * len(block) // 1024 // 1024)
elif case in ("hold-memory", "shared-memory"):
    block = b"x" * (40 * 1024 * 1024)
    if case == "shared-memory" and os.fork() == 0:
        time.sleep(0.5)
        os._exit(0)
    time.sleep(0.5)
    print("held", len(block) // 1024 // 1024)
elif case in ("file-memory", "unlinked-memory", "memfd-memory"):
    block = b"x" * (24 * 1024 * 1024)
    chunk = b"y" * (1024 * 1024)
    for name in ("one", "two"):
        if case == "memfd-memory":
            kept = os.memfd_create(name)
        else:
            kept = os.open(name, os.O_RDWR | os.O_CREAT)
            if case == "unlinked-memory":
                os.unlink(name)
        for _ in range(24):
            os.write(kept, chunk)
    time.sleep(0.5)
    print("held", (len(block) + 48 * len(chunk)) // 1024 // 1024)
elif case == "pipe-memory":
    import fcntl
    block = b"x" * (24 * 1024 * 1024)
    pipes = []
    for _ in range(48):
        read_end, write_end = os.pipe()
        fcntl.fcntl(write_end, fcntl.F_SETPIPE_SZ, 1024 * 1024)
        os.set_blocking(write_end, False)
        os.write(write_end, b"y" * (1024 * 1024))
        pipes.append((read_end, write_end))
    time.sleep(0.5)
    print("held", (len(block) + 48 * 1024 * 1024) // 1024 // 1024)
elif case == "socket-memory":
    import socket
    block = b"x" * (24 * 1024 * 1024)
    pairs, sent = [], 0
    while sent < 48 * 1024 * 1024 and len(pairs) < 400:
        one, two = socket.socketpair()
        one.setblocking(False)
        try:
            while True:
                sent += one.send(b"y" * 65536)
        except BlockingIOError:
            pass
        pairs.append((one, two))
    time.sleep(0.5)
    print("held", (len(block) + sent) // 1024 // 1024)
elif case == "small-file":
    block = b"x" * (40 * 1024 * 1024)
    with open("note", "wb") as note:
        note.write(b"z" * (1024 * 1024))
    time.sleep(0.5)
    print("held", len(block) // 1024 // 1024)
elif case == "prying":
    for path in ("/proc/%d/fd/3" % os.getppid(), "/proc/1/mem"):
        try:
            open(path, "rb").close()
            print("opened", path)
        except OSError as error:
            print("refused", error.errno)
elif case == "interrupt":
    import signal
    try:
        os.kill(1, signal.SIGINT)
    except OSError as error:
        print("refused", error.errno)
    print("still here")
"""
