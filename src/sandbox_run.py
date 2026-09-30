#!/usr/local/bin/python3 -I
"""The sandbox-run primitive: run one binary once per input and measure each run.

The harness mounts a working directory at `/work` (or the directory named by
the first argument) holding `inputs.json` and the files under `in/`. The
inputs are a batch: one item per test, each naming the binary, the input to
feed it on standard input, a time limit in seconds and a memory limit in
megabytes. For every item this program runs the binary, keeps what it printed
as `out/<id>/output`, and reports the CPU time, the peak memory and an
outcome, then writes `outputs.json` with one entry per item in the same order.

A run that goes over a limit is an ordinary result (`time_limit`,
`memory_limit`, `output_limit`, `runtime_error`), never an error of the step.
`error` in `outputs.json` is kept for the cases where the primitive could not
work at all, such as a missing input or a binary in no format it runs.

Each run is a fresh process in a fresh empty directory, with every process it
left behind killed before the next item starts. Every run is put under
Landlock: it may write only inside that directory and to /dev/null, read only
the system's own directories, its binary and that directory, and may not
trace anything outside itself, and from Landlock ABI 6 it may not signal
anything outside itself either. A kernel without Landlock is an error of the
step: the runs are not started unconfined.
"""

import ctypes
import errno
import json
import math
import os
import re
import resource
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

SCHEMA_VERSION = 3
OUTPUT_LIMIT = 32 * 1024 * 1024
POLL_SECONDS = 0.005
STRAY_SECONDS = 5.0
JAVA_OUT_OF_MEMORY = 3
RUN_PATH = "/usr/local/bin:/usr/bin:/bin"
HELPER = "/usr/local/bin/sandbox-exec"
ITEM_ID = re.compile(r"[^/\x00-\x1f]{1,255}")
REPORT_LINE = re.compile(rb"([PLER]) (-?[0-9]{1,20}(?: -?[0-9]{1,20}){0,3})\n")
REFUSED_BINARY = frozenset(
    {errno.ENOEXEC, errno.ENOMEM, errno.E2BIG, errno.EINVAL, errno.ELIBBAD}
)
"""What the kernel answers when it will not run a native binary as built: the
contestant's program, not the step, failed.
"""
ELF_MAGIC = b"\x7fELF"

PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
MINIMUM_LANDLOCK_ABI = 1
"""Reads and writes can both be confined from the first Landlock ABI, Linux
5.13. Later ABIs add what a run is kept from on top: renaming across
directories is refused outright below ABI 2, `truncate` of a file named by
path is confined from ABI 3, and signals from ABI 6.
"""
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_WRITES = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12))
FS_READS = FS_READ_FILE | FS_READ_DIR
FS_REFER = 1 << 13
FS_TRUNCATE = 1 << 14
READABLE_DIRECTORIES = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
    "/proc",
    "/sys",
)
"""What a run may read besides its binary and its own directory: the system's
programs, libraries and settings, which the interpreters need (Java its
configuration under /etc, its container support /proc/self and
/sys/fs/cgroup), and nothing under /work or /tmp. The image holds nothing
secret there. Those that do not exist on the machine are passed over.
"""
READABLE_DEVICES = ("/dev/zero", "/dev/random", "/dev/urandom")
"""Devices a run may read besides /dev/null, which it may also write."""
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1


class PrimitiveError(Exception):
    """The primitive could not do its work at all.

    The message is one sentence for a person and becomes `error` in
    `outputs.json`, which the harness turns into a `system_error` verdict.
    """


@dataclass(frozen=True)
class Item:
    """One test to run: its id, the binary, the input and the two limits."""

    id: str
    binary: Path
    input: Path
    time_limit: float
    memory_limit: float

    @property
    def wall_limit(self) -> float:
        """The wall-clock seconds a run may take: twice the time limit plus one.

        The time limit itself is on CPU time; the wall-clock limit stops a run
        that sleeps or waits without using the CPU.
        """
        return 2 * self.time_limit + 1

    @property
    def memory_limit_kb(self) -> int:
        """The memory limit in kilobytes, the unit peak memory is measured in."""
        return int(self.memory_limit * 1024)


@dataclass(frozen=True)
class Program:
    """A binary ready to run: its format and the file the command starts."""

    kind: str
    path: Path

    def readable(self) -> list[Path]:
        """What a run of this binary reads besides the system's directories:
        the binary, and for Python the interpreter's own installation, which
        is under /usr in the image. The interpreter is started by its real
        path, outside any virtual environment this program runs in, so it
        reads no `pyvenv.cfg` it could not be allowed.
        """
        if self.kind == "python":
            return [self.path, Path(sys.base_prefix)]
        return [self.path]

    def command(self, item: Item) -> list[str]:
        """The command line that runs this binary for one item."""
        if self.kind == "native":
            return [str(self.path)]
        if self.kind == "python":
            return [os.path.realpath(sys.executable), "-I", "-B", str(self.path)]
        java = shutil.which("java", path=RUN_PATH)
        if java is None:
            raise PrimitiveError("this image has no Java runtime to run a jar with")
        return [
            java,
            f"-Xmx{max(16, int(item.memory_limit))}m",
            "-Xss64m",
            "-XX:+UseSerialGC",
            "-XX:-UsePerfData",
            "-XX:+ExitOnOutOfMemoryError",
            "-Djava.io.tmpdir=.",
            "-jar",
            str(self.path),
        ]


@dataclass(frozen=True)
class Run:
    """What one run did: how it ended and what it used.

    `returncode` is the exit code, or minus the signal that killed it.
    `stopped` names the limit this program stopped it for, if any.
    """

    returncode: int
    cpu_seconds: float
    wall_seconds: float
    memory_kb: int
    output_bytes: int
    stopped: str | None


def main(argv: list[str]) -> int:
    """Run every item `inputs.json` names and write `outputs.json`.

    Exits 0 whenever `outputs.json` was written, error or not; the harness
    reads the outcomes and any error from that file.
    """
    root = Path(argv[1]) if len(argv) > 1 else Path("/work")
    try:
        items = read_inputs(root)
        document: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "batch": run_batch(root, items),
        }
    except PrimitiveError as error:
        print(f"sandbox-run: {error}", file=sys.stderr)
        document = {"schema_version": SCHEMA_VERSION, "error": str(error)}
    write_json(root / "outputs.json", document)
    return 0


def read_inputs(root: Path) -> list[Item]:
    """Read `inputs.json` and return its items in order."""
    try:
        document = json.loads((root / "inputs.json").read_bytes())
    except FileNotFoundError:
        raise PrimitiveError("inputs.json is not in the working directory") from None
    except ValueError:
        raise PrimitiveError("inputs.json is not valid JSON") from None
    if not isinstance(document, dict):
        raise PrimitiveError("inputs.json is not a JSON object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PrimitiveError(
            f"inputs.json is not written against contract version {SCHEMA_VERSION}"
        )
    batch = document.get("batch")
    if not isinstance(batch, list):
        raise PrimitiveError("inputs.json has no batch list")
    items = [read_item(root, entry) for entry in batch]
    if len({item.id for item in items}) != len(items):
        raise PrimitiveError("two items in the batch have the same id")
    return items


def read_item(root: Path, entry: object) -> Item:
    """Check one batch entry and return it as an item."""
    if not isinstance(entry, dict) or not isinstance(entry.get("inputs"), dict):
        raise PrimitiveError("a batch item has no inputs object")
    item_id = entry.get("id")
    if (
        not isinstance(item_id, str)
        or not ITEM_ID.fullmatch(item_id)
        or item_id in (".", "..")
    ):
        raise PrimitiveError(f"the batch item id {item_id!r} cannot name a directory")
    inputs = entry["inputs"]
    return Item(
        id=item_id,
        binary=input_file(root, inputs, "binary", item_id),
        input=input_file(root, inputs, "input", item_id),
        time_limit=positive_number(inputs, "time_limit", item_id),
        memory_limit=positive_number(inputs, "memory_limit", item_id),
    )


def input_file(root: Path, inputs: dict[str, object], name: str, item_id: str) -> Path:
    """Resolve a file input to a path, refusing anything outside `in/`."""
    value = inputs.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("file"), str):
        raise PrimitiveError(f"the input named {name} of item {item_id} is not a file")
    path = (root / str(value["file"])).resolve()
    if not path.is_relative_to((root / "in").resolve()):
        raise PrimitiveError(f"the input named {name} of item {item_id} is outside in/")
    if not path.is_file():
        raise PrimitiveError(
            f"the input named {name} of item {item_id} is not in the working directory"
        )
    return path


def positive_number(inputs: dict[str, object], name: str, item_id: str) -> float:
    """Read a number input that must be above zero."""
    value = inputs.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise PrimitiveError(
            f"the input named {name} of item {item_id} is not a positive number"
        )
    return float(value)


def run_batch(root: Path, items: list[Item]) -> list[dict[str, object]]:
    """Run every item in order and return one outputs entry per item.

    This program makes itself the subreaper of everything it starts, and not
    dumpable, so no run can trace it or open its memory or file descriptors
    even without Landlock. It ignores SIGINT, the one signal Python would act
    on: as the container's first process the kernel already keeps every
    signal it has no handler for away from it, so a run cannot stop it.
    """
    prctl(PR_SET_CHILD_SUBREAPER, 1)
    prctl(PR_SET_DUMPABLE, 0)
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return run_all(root, items)
    finally:
        signal.signal(signal.SIGINT, previous)


def run_all(root: Path, items: list[Item]) -> list[dict[str, object]]:
    """Run the items one after another, each binary prepared once.

    Nothing runs unless the kernel offers Landlock at `MINIMUM_LANDLOCK_ABI`
    or later: without it a run could read the other tests' inputs and outputs
    and write the files `outputs.json` is built from.
    """
    abi = landlock_abi()
    if abi < MINIMUM_LANDLOCK_ABI:
        raise PrimitiveError(
            "this machine's kernel does not offer Landlock, which every run is "
            "confined with; it needs Linux 5.13 or later with Landlock enabled"
        )
    scratch = Path(tempfile.mkdtemp(prefix=".sandbox-run-", dir=root))
    programs: dict[Path, Program] = {}
    results: list[dict[str, object]] = []
    try:
        for item in items:
            if item.binary not in programs:
                programs[item.binary] = prepare(item.binary, scratch, len(programs))
            outputs = run_item(root, item, programs[item.binary], abi)
            results.append({"id": item.id, "outputs": outputs})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return results


def detect(binary: Path) -> str:
    """Tell which of the three binary formats a file is.

    `native` is an ELF executable, `python` a zip application with a
    `__main__.py`, `java` a jar with a manifest.
    """
    with binary.open("rb") as stream:
        if stream.read(4) == ELF_MAGIC:
            return "native"
    if zipfile.is_zipfile(binary):
        with zipfile.ZipFile(binary) as archive:
            names = set(archive.namelist())
        if "__main__.py" in names:
            return "python"
        if "META-INF/MANIFEST.MF" in names:
            return "java"
    raise PrimitiveError(
        "the binary is not a native executable, a Python zip application or a Java jar"
    )


def prepare(binary: Path, scratch: Path, number: int) -> Program:
    """Get a binary ready to run.

    A native executable is copied into the scratch directory and marked
    executable there, since the copy under `in/` may not be; the other two
    formats are read by their interpreter where they are.
    """
    kind = detect(binary)
    if kind != "native":
        return Program(kind, binary)
    path = scratch / f"program-{number}"
    shutil.copyfile(binary, path)
    path.chmod(0o755)
    return Program(kind, path)


def run_item(root: Path, item: Item, program: Program, abi: int) -> dict[str, object]:
    """Run one item and return its outputs."""
    out = root / "out" / item.id
    out.mkdir(parents=True, exist_ok=True)
    output = out / "output"
    cwd = Path(tempfile.mkdtemp(prefix="run-"))
    try:
        descriptor = os.open(
            output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644
        )
        with item.input.open("rb") as stdin, os.fdopen(descriptor, "wb") as stdout:
            run = execute(program, cwd, stdin, stdout, item, abi)
    finally:
        reap_strays()
        shutil.rmtree(cwd, ignore_errors=True)
    if run.output_bytes > OUTPUT_LIMIT:
        os.truncate(output, OUTPUT_LIMIT)
    return {
        "output": {"file": f"out/{item.id}/output"},
        "time_ms": round(run.cpu_seconds * 1000),
        "memory_kb": run.memory_kb,
        "outcome": judge(run, item, program.kind),
    }


def execute(
    program: Program,
    cwd: Path,
    stdin: BinaryIO,
    stdout: BinaryIO,
    item: Item,
    abi: int,
) -> Run:
    """Start one run and watch it until it and everything it started are gone.

    The binary is started through `sandbox-exec`, which forks it and reports
    its process id and, once it ends, its exit status, CPU time and peak
    memory. Every few milliseconds every process of the run is read: their
    CPU time together, the highest peak resident memory of any one of them,
    the wall-clock time and the output size, and the binary is killed as soon
    as one is over its limit. Resource limits set before the start back this
    up: CPU time, file size, stack, and no core dumps.

    The CPU time reported is the binary's own, as `sandbox-exec` saw it, plus
    that of every process the run left behind, which this program reaps, so a
    run cannot hide work in a child it never waits for. A run that kills
    `sandbox-exec` is judged on what this program measured itself.
    """
    env = {"PATH": RUN_PATH, "HOME": str(cwd), "TMPDIR": str(cwd), "LANG": "C.UTF-8"}
    try:
        ruleset = landlock_ruleset(cwd, program.readable(), abi)
    except OSError as error:
        raise PrimitiveError(
            f"the run could not be confined with Landlock: {error.strerror}"
        ) from None
    read_end, write_end = os.pipe()
    passed = (write_end, ruleset)
    started = time.monotonic()
    try:
        helper = subprocess.Popen(
            [HELPER, str(write_end), str(ruleset), *program.command(item)],
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            pass_fds=passed,
            start_new_session=True,
            preexec_fn=child_setup(item),
        )
    except (OSError, subprocess.SubprocessError) as error:
        os.close(read_end)
        raise PrimitiveError(f"the binary could not be started: {error}") from None
    finally:
        os.close(write_end)
        os.close(ruleset)
    os.set_blocking(read_end, False)
    report = bytearray()
    pid = 0
    stopped: str | None = None
    stopped_at = 0.0
    peak_kb = 0
    cpu = 0.0
    me = os.getpid()
    while True:
        report += drain(read_end)
        pid = pid or parse_report(report).get("P", [0])[0]
        done, status, _ = os.wait4(helper.pid, os.WNOHANG)
        if done:
            break
        cpu, memory_kb = sample(me, helper.pid)
        peak_kb = max(peak_kb, memory_kb)
        now = time.monotonic()
        if stopped is None:
            if cpu > item.time_limit or now - started > item.wall_limit:
                stopped = "time"
            elif memory_kb > item.memory_limit_kb:
                stopped = "memory"
            elif os.fstat(stdout.fileno()).st_size > OUTPUT_LIMIT:
                stopped = "output"
            if stopped:
                stopped_at = now
                kill(pid or -helper.pid)
        elif now - stopped_at > STRAY_SECONDS:
            kill(-helper.pid)
        time.sleep(POLL_SECONDS)
    wall = time.monotonic() - started
    helper.returncode = os.waitstatus_to_exitcode(status)
    report += drain(read_end)
    os.close(read_end)
    lines = parse_report(report)
    before_strays = children_cpu()
    reap_strays()
    strays = children_cpu() - before_strays
    if "L" in lines:
        raise PrimitiveError(
            f"the run could not be confined with Landlock: {os.strerror(lines['L'][0])}"
        )
    if "E" in lines:
        refused = lines["E"][0]
        if program.kind != "native" or refused not in REFUSED_BINARY:
            raise PrimitiveError(
                f"the binary could not be started: {os.strerror(refused)}"
            )
        return Run(127, 0.0, wall, 0, 0, stopped)
    if "R" in lines:
        ended, user_us, system_us, maxrss_kb = lines["R"]
        returncode = os.waitstatus_to_exitcode(ended)
        reported = (user_us + system_us) / 1e6
    elif os.WIFSIGNALED(status):
        returncode, reported, maxrss_kb = -signal.SIGKILL, 0.0, 0
    else:
        raise PrimitiveError("a run ended without sandbox-exec reporting what it used")
    return Run(
        returncode=returncode,
        cpu_seconds=max(reported + strays, cpu),
        wall_seconds=wall,
        memory_kb=max(maxrss_kb, peak_kb),
        output_bytes=os.fstat(stdout.fileno()).st_size,
        stopped=stopped,
    )


def drain(fd: int) -> bytes:
    """Read whatever a non-blocking pipe holds right now."""
    data = bytearray()
    while True:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            break
        if not chunk:
            break
        data += chunk
    return bytes(data)


def parse_report(report: bytes | bytearray) -> dict[str, list[int]]:
    """The complete lines of `sandbox-exec`'s report, by tag, as their numbers.

    A tag's first line counts: `sandbox-exec` writes each once. Anything that
    is not a line it writes is passed over.
    """
    lines: dict[str, list[int]] = {}
    for line in bytes(report).splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        matched = REPORT_LINE.fullmatch(line)
        if matched is None:
            continue
        tag = matched.group(1).decode()
        if tag not in lines:
            lines[tag] = [int(number) for number in matched.group(2).split()]
    return lines


def children_cpu() -> float:
    """CPU seconds of every child this program has reaped so far."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def child_setup(item: Item) -> Callable[[], None]:
    """The function the child runs between fork and exec."""

    def setup() -> None:
        """Set the run's resource limits, put the run first in line for the
        kernel's out-of-memory killer, and let SIGINT act again.
        """
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_LIMIT + 1, OUTPUT_LIMIT + 1))
        cpu = math.ceil(item.time_limit) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        stack = item.memory_limit_kb * 1024
        _, hard = resource.getrlimit(resource.RLIMIT_STACK)
        if hard == resource.RLIM_INFINITY or stack <= hard:
            resource.setrlimit(resource.RLIMIT_STACK, (stack, hard))
        try:
            Path("/proc/self/oom_score_adj").write_text("1000")
        except OSError:
            pass
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    return setup


def judge(run: Run, item: Item, kind: str) -> str:
    """Give a run its outcome.

    The limit the run was stopped for comes first. Otherwise the output limit,
    the time limits and the memory limit are checked in that order against
    what was measured and how the run ended, and any other failure is a
    runtime error. A jar that ran out of Java heap exits with code 3.
    """
    if run.stopped:
        return {"time": "time_limit", "memory": "memory_limit"}.get(
            run.stopped, "output_limit"
        )
    if run.returncode == -signal.SIGXFSZ or run.output_bytes > OUTPUT_LIMIT:
        return "output_limit"
    if (
        run.returncode == -signal.SIGXCPU
        or run.cpu_seconds > item.time_limit
        or run.wall_seconds > item.wall_limit
    ):
        return "time_limit"
    if run.memory_kb > item.memory_limit_kb or (
        kind == "java" and run.returncode == JAVA_OUT_OF_MEMORY
    ):
        return "memory_limit"
    if run.returncode != 0:
        return "runtime_error"
    return "accepted"


def sample(me: int, helper: int) -> tuple[float, int]:
    """The run's CPU seconds so far and its peak resident memory in kilobytes.

    The run is every process descended from this program but `sandbox-exec`:
    the binary, whatever it started, and whatever it left behind, which comes
    back to this program as the subreaper. Their CPU time is added up, with
    what each has reaped of its own children; the memory is the highest peak
    of any one of them, since forked processes share pages and a sum would
    count those twice. The container's own memory limit holds the total.
    """
    parents: dict[int, int] = {}
    ticks: dict[int, int] = {}
    for entry in (path.name for path in Path("/proc").iterdir()):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2 :].split()
        parents[int(entry)] = int(fields[1])
        ticks[int(entry)] = sum(int(field) for field in fields[11:15])
    run = descendants(me, parents) - {helper}
    cpu = sum(ticks[pid] for pid in run) / os.sysconf("SC_CLK_TCK")
    return cpu, max((peak_memory(pid) for pid in run), default=0)


def descendants(root: int, parents: dict[int, int]) -> set[int]:
    """Every process below `root`, from each process's parent."""
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    found: set[int] = set()
    waiting = list(children.get(root, []))
    while waiting:
        pid = waiting.pop()
        if pid not in found:
            found.add(pid)
            waiting.extend(children.get(pid, []))
    return found


def peak_memory(pid: int) -> int:
    """A process's peak resident memory in kilobytes, 0 when it has none."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return 0
    for line in status.splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    return 0


def kill(pid: int) -> None:
    """Kill one process, or a whole process group when `pid` is negative."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        pass


def reap_strays() -> None:
    """Kill and reap every process a run left behind.

    This program is the subreaper of everything it starts, so a process that
    outlived the run, even one that left its session, is one of its children.
    """
    deadline = time.monotonic() + STRAY_SECONDS
    while time.monotonic() < deadline:
        for pid in child_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        time.sleep(POLL_SECONDS)
    raise PrimitiveError("processes left behind by a run could not be stopped")


def child_pids() -> list[int]:
    """List the processes whose parent is this program."""
    me = os.getpid()
    children = []
    for entry in (path.name for path in Path("/proc").iterdir()):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        if int(stat[stat.rindex(")") + 2 :].split()[1]) == me:
            children.append(int(entry))
    return children


def libc() -> ctypes.CDLL:
    """The C library, for prctl and the Landlock system calls."""
    return ctypes.CDLL(None, use_errno=True)


def prctl(option: int, value: int) -> None:
    """Call prctl with one argument, raising on failure."""
    unused = ctypes.c_ulong(0)
    if libc().prctl(option, ctypes.c_ulong(value), unused, unused, unused) != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def syscall(number: int, *arguments: int | ctypes.Array[ctypes.c_char] | None) -> int:
    """Make a raw system call, raising on failure."""
    function = libc().syscall
    function.restype = ctypes.c_long
    converted = [ctypes.c_long(a) if isinstance(a, int) else a for a in arguments]
    result: int = function(ctypes.c_long(number), *converted)
    if result < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return result


def landlock_abi() -> int:
    """The Landlock ABI version the kernel offers, or 0 when it has none."""
    try:
        return syscall(
            SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION
        )
    except OSError:
        return 0


def landlock_ruleset(directory: Path, readable: list[Path], abi: int) -> int:
    """A Landlock ruleset for one run in `directory` that reads `readable`.

    Writing, creating, removing and renaming are allowed under `directory`,
    and writing to /dev/null. Reading is allowed under `directory`, of each
    file in `readable` and beneath each directory in it (the binary, and the
    interpreter it needs), beneath the system directories in
    READABLE_DIRECTORIES, and of /dev/null and the devices in
    READABLE_DEVICES, and nowhere else: not the other tests' inputs under
    `in/`, their outputs under `out/`, nor `inputs.json`. From
    ABI 6 a process under it also may not signal, or reach an abstract Unix
    socket of, anything outside its own Landlock domain; from ABI 1 it may not
    trace anything outside it. Running a file needs reading it, so only the
    binary and the system's programs can be run. Files already open, standard
    input and output, stay usable. `sandbox-exec` enforces it on the binary
    alone, just before exec, so neither it nor this program is inside.
    """
    handled = (
        FS_WRITES
        | FS_READS
        | (FS_REFER if abi >= 2 else 0)
        | (FS_TRUNCATE if abi >= 3 else 0)
    )
    scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if abi >= 6 else 0
    size = 8 if abi < 4 else 16 if abi < 6 else 24
    attr = ctypes.create_string_buffer(
        struct.pack("=QQQ", handled, 0, scoped)[:size], size
    )
    truncate = FS_TRUNCATE if abi >= 3 else 0
    rules: list[tuple[str, int]] = [
        (str(directory), handled),
        ("/dev/null", FS_READ_FILE | FS_WRITE_FILE | truncate),
    ]
    rules += [
        (str(path), FS_READS if path.is_dir() else FS_READ_FILE) for path in readable
    ]
    rules += [(path, FS_READS) for path in READABLE_DIRECTORIES if Path(path).is_dir()]
    rules += [(path, FS_READ_FILE) for path in READABLE_DEVICES]
    ruleset = syscall(SYS_LANDLOCK_CREATE_RULESET, attr, size, 0)
    try:
        for path, access in rules:
            try:
                parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            rule = ctypes.create_string_buffer(struct.pack("=Qi", access, parent), 12)
            try:
                syscall(
                    SYS_LANDLOCK_ADD_RULE, ruleset, LANDLOCK_RULE_PATH_BENEATH, rule, 0
                )
            finally:
                os.close(parent)
    except OSError:
        os.close(ruleset)
        raise
    return ruleset


def write_json(path: Path, document: dict[str, object]) -> None:
    """Write a JSON file whole: to a temporary name first, then renamed. The
    temporary file is made afresh, never opened through a link a run left.
    """
    partial = path.with_name(path.name + ".partial")
    partial.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(partial, flags, 0o644), "w", encoding="utf-8") as stream:
        stream.write(json.dumps(document, ensure_ascii=False))
    partial.replace(path)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
