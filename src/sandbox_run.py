#!/usr/local/bin/python3 -I
"""The sandbox-run primitive: run one binary once per input and measure each run.

The harness mounts a working directory at `/work` (or the directory named by
the first argument) holding `inputs.json` and the files under `in/`. The
inputs are a batch: one item per test, each naming the binary, the input to
feed it on standard input, optional arguments for its command line, a time
limit in seconds and a memory limit in megabytes. For every item this program
runs the binary, keeps what it printed as `out/<n>/output`, `n` being the
item's place in the batch, and reports the CPU time, the peak memory and an
outcome, then writes `outputs.json` with one entry per item in the same order.

A run that goes over a limit is an ordinary result (`time_limit`,
`memory_limit`, `output_limit`, `runtime_error`), never an error of the step,
and every item reports its time and memory whatever its outcome.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

SCHEMA_VERSION = 5
OUTPUT_LIMIT = 32 * 1024 * 1024
POLL_SECONDS = 0.005
TOTAL_SPACING = 4.0
"""How long to wait before reading a run's total memory again, as a multiple
of how long the last reading took. A reading walks every page the run holds,
about 25 microseconds a megabyte where it was measured, so at 4 this
program spends at most a fifth of its time on it however much memory the run
holds, and the run, which shares the container's one CPU with it, keeps the
rest.
"""
STRAY_SECONDS = 1.0
"""How long a stopped run's helper has to report before its whole group is
killed."""
REAP_SECONDS = 5.0
OOM_EVENTS = Path("/sys/fs/cgroup/memory.events")
CGROUP = Path("/sys/fs/cgroup")
NO_CGROUP = (
    "sandbox-run: this program is not alone in a cgroup of its own with cgroup v2 "
    "memory accounting, so memory a run keeps outside its processes, in files, "
    "pipes or sockets, is not counted against its limit"
)
JAVA_OUT_OF_MEMORY = 3
RUN_PATH = "/usr/local/bin:/usr/bin:/bin"
HELPER = "/usr/local/bin/sandbox-exec"
TEST_ID = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+")
"""A test's id, `<group>/<test>`. It holds a `/`, so no file is named after
it: an item's output goes under its place in the batch.
"""
REPORT_LINE = re.compile(rb"([PLER]) (-?[0-9]{1,20}(?: -?[0-9]{1,20}){0,3})\n")
REFUSED_BINARY = frozenset(
    {errno.ENOEXEC, errno.ENOMEM, errno.E2BIG, errno.EINVAL, errno.ELIBBAD}
)
"""What the kernel answers when it will not run a native binary as built: the
contestant's program, not the step, failed.
"""
REFUSED_ARGUMENTS = frozenset({errno.E2BIG})
"""What the kernel answers when the command line is too long for it, which
only `args` can make it: the contestant's input, not the step, failed.
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
    `outputs.json`, which the harness turns into a `system_error`.
    """


@dataclass(frozen=True)
class Item:
    """One test to run: its id, its place in the batch from 1, the binary, the
    input, the two limits and the arguments.
    """

    test: str
    number: int
    binary: Path
    input: Path
    time_limit: float
    memory_limit: float
    args: tuple[str, ...] = ()

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

    @property
    def time_limit_ms(self) -> int:
        """The time limit in whole milliseconds, rounded up: the least time a
        run stopped at it reports. Rounding to a microsecond first keeps a
        limit such as 0.1 s, a little over 100 ms as a float, at 100.
        """
        return math.ceil(round(self.time_limit * 1000, 3))


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
        """The command line that runs this binary for one item, the item's
        arguments last.
        """
        if self.kind == "native":
            return [str(self.path), *item.args]
        if self.kind == "python":
            interpreter = os.path.realpath(sys.executable)
            return [interpreter, "-I", "-B", str(self.path), *item.args]
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
            *item.args,
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
    out_of_memory: bool = False


@dataclass
class RunMemory:
    """The memory of one run's processes, read while the run goes on.

    `peak_kb` is the highest of three readings, in kilobytes. One is the peak
    resident memory of each process alone, which the kernel keeps itself
    (`VmHWM`), so no process's own peak between two readings is missed. The
    second is the resident memory of all the run's processes added up, with
    each page several of them share divided among them (`Pss`): a child
    forked from the binary shares its parent's pages until it changes them,
    so it is charged only for the pages it changes, while two processes that
    each fill memory of their own are charged for both. The third is what
    `cgroup`, the step container's, holds beyond what it held when the
    reading began, before the run started (`held_kb`): that counts what no
    process's pages do, the files a run keeps in its own directory, which is
    in a tmpfs, files it deleted and keeps open, memory files, and pipe and
    socket buffers, and only one run goes at a time. The kernel keeps no peak
    of the second and the third, so memory held for less than the time
    between two readings is not seen; the container's own memory limit holds
    it.

    Reading the total walks every page the run holds, so it is read only
    when it could raise the peak, that is when the processes' resident memory
    added up, which the total never exceeds, is above the peak: never for a
    run of one process. It is read again only once `TOTAL_SPACING` times as
    long as the last reading took has passed.
    """

    cgroup: Path | None = None
    peak_kb: int = 0
    total_due: float = 0.0
    held_before: int | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.cgroup is not None:
            self.held_before = held_kb(self.cgroup)

    def read(self, processes: set[int]) -> None:
        """Read the memory of the run's processes and raise the peak."""
        held = held_kb(self.cgroup) if self.cgroup is not None else None
        if held is not None and self.held_before is not None:
            self.peak_kb = max(self.peak_kb, held - self.held_before)
        resident: dict[int, int] = {}
        for pid in processes:
            highest, resident[pid] = resident_memory(pid)
            self.peak_kb = max(self.peak_kb, highest)
        started = time.monotonic()
        if sum(resident.values()) <= self.peak_kb or started < self.total_due:
            return
        total = sum(proportional_memory(pid, kb) for pid, kb in resident.items())
        self.peak_kb = max(self.peak_kb, total)
        ended = time.monotonic()
        self.total_due = ended + TOTAL_SPACING * (ended - started)


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
    items = [read_item(root, entry, number) for number, entry in enumerate(batch, 1)]
    if len({item.test for item in items}) != len(items):
        raise PrimitiveError("two items in the batch are for the same test")
    return items


def read_item(root: Path, entry: object, number: int) -> Item:
    """Check one batch entry, the `number`th, and return it as an item."""
    if not isinstance(entry, dict) or not isinstance(entry.get("inputs"), dict):
        raise PrimitiveError("a batch item has no inputs object")
    test = entry.get("test")
    if not isinstance(test, str) or len(test) > 255 or not TEST_ID.fullmatch(test):
        raise PrimitiveError(f"the batch item test {test!r} is not a test id")
    inputs = entry["inputs"]
    return Item(
        test=test,
        number=number,
        binary=input_file(root, inputs, "binary", test),
        input=input_file(root, inputs, "input", test),
        time_limit=positive_number(inputs, "time_limit", test),
        memory_limit=positive_number(inputs, "memory_limit", test),
        args=arguments(inputs, test),
    )


def input_file(root: Path, inputs: dict[str, object], name: str, test: str) -> Path:
    """Resolve a file input to a path, refusing anything outside `in/`."""
    value = inputs.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("file"), str):
        raise PrimitiveError(f"the input named {name} of test {test} is not a file")
    path = (root / str(value["file"])).resolve()
    if not path.is_relative_to((root / "in").resolve()):
        raise PrimitiveError(f"the input named {name} of test {test} is outside in/")
    if not path.is_file():
        raise PrimitiveError(
            f"the input named {name} of test {test} is not in the working directory"
        )
    return path


def positive_number(inputs: dict[str, object], name: str, test: str) -> float:
    """Read a number input that must be above zero."""
    value = inputs.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise PrimitiveError(
            f"the input named {name} of test {test} is not a positive number"
        )
    return float(value)


def arguments(inputs: dict[str, object], test: str) -> tuple[str, ...]:
    """Read `args`, the optional text appended to the binary's command line,
    split on spaces: a run of spaces separates two arguments, and spaces at
    either end separate nothing. There is no quoting, so no argument holds a
    space. Absent, the binary gets no arguments.
    """
    value = inputs.get("args")
    if value is None:
        return ()
    if not isinstance(value, str):
        raise PrimitiveError(f"the input named args of test {test} is not text")
    return tuple(part for part in value.split(" ") if part)


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
    if own_cgroup() is None:
        print(NO_CGROUP, file=sys.stderr)
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
            results.append({"test": item.test, "outputs": outputs})
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
    """Run one item and return its outputs, all of them whatever the outcome:
    what the binary printed, its CPU time and its peak memory.

    The time is the CPU time, and on `time_limit` at least the limit: a run
    the wall clock stopped, one that slept or waited, used little CPU. An
    argument holding a NUL character, which no command line can carry, is a
    run that could not start, a `runtime_error`, like a binary the kernel
    will not run.
    """
    out = root / "out" / str(item.number)
    out.mkdir(parents=True, exist_ok=True)
    output = out / "output"
    cwd = Path(tempfile.mkdtemp(prefix="run-"))
    try:
        descriptor = os.open(
            output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644
        )
        with item.input.open("rb") as stdin, os.fdopen(descriptor, "wb") as stdout:
            if any("\0" in argument for argument in item.args):
                run = Run(127, 0.0, 0.0, 0, 0, None)
            else:
                run = execute(program, cwd, stdin, stdout, item, abi)
    finally:
        reap_strays()
        shutil.rmtree(cwd, ignore_errors=True)
    if run.output_bytes > OUTPUT_LIMIT:
        os.truncate(output, OUTPUT_LIMIT)
    outcome = judge(run, item, program.kind)
    time_ms = round(run.cpu_seconds * 1000)
    if outcome == "time_limit":
        time_ms = max(time_ms, item.time_limit_ms)
    return {
        "output": {"file": f"out/{item.number}/output"},
        "time_ms": time_ms,
        "memory_kb": run.memory_kb,
        "outcome": outcome,
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
    CPU time together, their memory (see `RunMemory`), the wall-clock time
    and the output size, and the binary is killed as soon as one is over its
    limit. Resource limits set before the start back this up: CPU time, file
    size, stack, and no core dumps.

    The memory reported is the higher of the binary's own peak, as
    `sandbox-exec` saw it, and the highest this program read while the run
    went on.

    The CPU time reported is the binary's own, as `sandbox-exec` saw it, plus
    that of every process the run left behind, which this program reaps, so a
    run cannot hide work in a child it never waits for. A run that kills
    `sandbox-exec` is judged on what this program measured itself.

    A command line too long for the kernel, which only the item's arguments
    can make, is a run that could not start, judged as a native binary the
    kernel will not run is.
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
    memory = RunMemory(cgroup=own_cgroup())
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
        if isinstance(error, OSError) and error.errno in REFUSED_ARGUMENTS:
            return Run(127, 0.0, 0.0, 0, 0, None)
        raise PrimitiveError(f"the binary could not be started: {error}") from None
    finally:
        os.close(write_end)
        os.close(ruleset)
    os.set_blocking(read_end, False)
    report = bytearray()
    pid = 0
    stopped: str | None = None
    stopped_at = 0.0
    killed_before = oom_kills()
    cpu = 0.0
    me = os.getpid()
    while True:
        report += drain(read_end)
        pid = pid or parse_report(report).get("P", [0])[0]
        done, status, _ = os.wait4(helper.pid, os.WNOHANG)
        if done:
            break
        cpu, processes = sample(me, helper.pid)
        memory.read(processes)
        now = time.monotonic()
        if stopped is None:
            if cpu > item.time_limit or now - started > item.wall_limit:
                stopped = "time"
            elif memory.peak_kb > item.memory_limit_kb:
                stopped = "memory"
            elif os.fstat(stdout.fileno()).st_size > OUTPUT_LIMIT:
                stopped = "output"
            if stopped:
                stopped_at = now
                kill(pid or -helper.pid)
                # The run may have stopped `sandbox-exec` with SIGSTOP where
                # Landlock does not scope signals, and a stopped helper never
                # reaps the binary; woken, it reports at once, and whatever
                # is left goes after STRAY_SECONDS.
                wake(helper.pid)
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
        contestants = REFUSED_BINARY if program.kind == "native" else frozenset()
        if refused not in contestants | REFUSED_ARGUMENTS:
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
        memory_kb=max(maxrss_kb, memory.peak_kb),
        output_bytes=os.fstat(stdout.fileno()).st_size,
        stopped=stopped,
        out_of_memory=returncode == -signal.SIGKILL and oom_kills() > killed_before,
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
    runtime error. A jar that ran out of Java heap exits with code 3, and a
    run the kernel killed for the container's memory is over the memory
    limit too: it held more than the container allows, between two readings
    or where this program could not count it.
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
    if (
        run.memory_kb > item.memory_limit_kb
        or run.out_of_memory
        or (kind == "java" and run.returncode == JAVA_OUT_OF_MEMORY)
    ):
        return "memory_limit"
    if run.returncode != 0:
        return "runtime_error"
    return "accepted"


def sample(me: int, helper: int) -> tuple[float, set[int]]:
    """The run's CPU seconds so far and the process ids of the run.

    The run is every process descended from this program but `sandbox-exec`:
    the binary, whatever it started, and whatever it left behind, which comes
    back to this program as the subreaper. Their CPU time is added up, with
    what each has reaped of its own children.
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
    return cpu, run


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


def resident_memory(pid: int) -> tuple[int, int]:
    """A process's peak and present resident memory in kilobytes, 0 and 0 when
    it has none.
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return 0, 0
    found = {"VmHWM": 0, "VmRSS": 0}
    for line in status.splitlines():
        name, _, value = line.partition(":")
        if name in found:
            found[name] = int(value.split()[0])
    return found["VmHWM"], found["VmRSS"]


def proportional_memory(pid: int, resident_kb: int) -> int:
    """A process's resident memory in kilobytes with each page it shares
    divided among the processes that share it (`Pss`), 0 once it has ended.

    A process whose pages this program may not read, one that made itself not
    dumpable, is counted at `resident_kb`, its whole resident memory, so it
    cannot hide what it holds from the total.
    """
    try:
        rollup = Path(f"/proc/{pid}/smaps_rollup").read_text()
    except PermissionError:
        return resident_kb
    except OSError:
        return 0
    for line in rollup.splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1])
    return 0


def own_cgroup() -> Path | None:
    """The cgroup that holds this program and its runs and nothing else: the
    step container's, which sees its own cgroup as the root of its cgroup
    namespace (the socket filter refuses a step any other), with cgroup v2's
    memory accounting. None anywhere else, such as a test run on a machine,
    whose cgroup holds other processes too.
    """
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return None
    if "0::/" not in lines or not (CGROUP / "memory.current").is_file():
        return None
    return CGROUP


def held_kb(cgroup: Path) -> int | None:
    """What the cgroup holds that a run can hold on purpose, in kilobytes:
    everything charged to it (`memory.current`) but the pages of files on
    disk, which the kernel takes back when it needs them (`file` in
    `memory.stat`, less `shmem`, which it counts there too and cannot take
    back). That is anonymous memory, files in a tmpfs, memory files, pipe and
    socket buffers, and the kernel's own memory for the processes. Both
    files are readable even where the cgroup is not writable; none where
    they are missing.
    """
    try:
        current = int((cgroup / "memory.current").read_text())
        stat = (cgroup / "memory.stat").read_text()
    except OSError, ValueError:
        return None
    counts = dict(line.partition(" ")[::2] for line in stat.splitlines())
    try:
        reclaimable = int(counts.get("file", "0")) - int(counts.get("shmem", "0"))
    except ValueError:
        return None
    return (current - reclaimable) // 1024


def oom_kills() -> int:
    """How many processes the kernel has killed in this container for want
    of memory, from its cgroup's `memory.events`, readable even where the
    cgroup is not writable; 0 where there is no such file.
    """
    try:
        events = OOM_EVENTS.read_text()
    except OSError:
        return 0
    for line in events.splitlines():
        name, _, count = line.partition(" ")
        if name == "oom_kill":
            return int(count)
    return 0


def wake(pid: int) -> None:
    """Continue a process that may have been stopped."""
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError, PermissionError:
        pass


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
    deadline = time.monotonic() + REAP_SECONDS
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
