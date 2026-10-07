"""The program's own logic, run in-process on Linux.

The runs use Python binaries on the interpreter running the tests, started
through a `sandbox-exec` built from this checkout with the host's gcc.
"""

import errno
import json
import os
import shutil
import signal
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

import sandbox_run
from support import (
    PROGRAM,
    batch_inputs,
    by_test,
    peek_input,
    peeked,
    printed,
    python_binary,
)


@pytest.fixture(scope="session")
def helper(tmp_path_factory: pytest.TempPathFactory) -> str:
    """`sandbox-exec` built from source."""
    if shutil.which("gcc") is None:
        pytest.skip("gcc is not installed")
    built = tmp_path_factory.mktemp("helper") / "sandbox-exec"
    source = Path(__file__).resolve().parent.parent / "src" / "sandbox-exec.c"
    command = ["gcc", "-O2", "-Wall", "-Werror", "-o", str(built), str(source)]
    subprocess.run(command, check=True)
    return str(built)


@pytest.fixture
def run(helper: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Run the program over a batch of the test program's cases."""
    monkeypatch.setattr(sandbox_run, "HELPER", helper)
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()

    def batch(
        items: list[tuple[str, str]],
        args: dict[str, str] | None = None,
        **limits: float,
    ) -> dict[str, Any]:
        work = tmp_path / "work"
        batch_inputs(work, binary, items, args=args, **limits)
        assert sandbox_run.main(["sandbox-run", str(work)]) == 0
        document: dict[str, Any] = json.loads(
            (work / "outputs.json").read_text(encoding="utf-8")
        )
        return document

    return batch


def make_run(**changes: Any) -> sandbox_run.Run:
    """A run that exited cleanly well inside every limit, with some fields changed."""
    fields: dict[str, Any] = {
        "returncode": 0,
        "cpu_seconds": 0.1,
        "wall_seconds": 0.2,
        "memory_kb": 1000,
        "output_bytes": 10,
        "stopped": None,
    }
    return sandbox_run.Run(**(fields | changes))


ITEM = sandbox_run.Item(
    "main/1", 1, Path("b"), Path("i"), time_limit=1.0, memory_limit=64
)


@pytest.mark.parametrize(
    ("limit", "ms"), [(1.0, 1000), (0.1, 100), (0.3, 300), (2.5, 2500), (0.0005, 1)]
)
def test_the_time_limit_in_milliseconds_is_rounded_up(limit: float, ms: int) -> None:
    item = sandbox_run.Item("main/1", 1, Path("b"), Path("i"), limit, 64)
    assert item.time_limit_ms == ms


@pytest.mark.parametrize(
    ("changes", "kind", "outcome"),
    [
        ({}, "native", "accepted"),
        ({"stopped": "time"}, "native", "time_limit"),
        ({"stopped": "memory", "returncode": -9}, "native", "memory_limit"),
        ({"stopped": "output", "returncode": -9}, "native", "output_limit"),
        ({"returncode": -signal.SIGXFSZ}, "native", "output_limit"),
        (
            {"output_bytes": sandbox_run.OUTPUT_LIMIT + 1, "returncode": 1},
            "python",
            "output_limit",
        ),
        ({"cpu_seconds": 1.01}, "native", "time_limit"),
        ({"wall_seconds": 3.5}, "native", "time_limit"),
        ({"returncode": -signal.SIGXCPU}, "native", "time_limit"),
        ({"memory_kb": 64 * 1024 + 1}, "native", "memory_limit"),
        ({"returncode": 3}, "java", "memory_limit"),
        ({"returncode": 3}, "native", "runtime_error"),
        ({"returncode": -signal.SIGSEGV}, "native", "runtime_error"),
    ],
)
def test_judge(changes: dict[str, Any], kind: str, outcome: str) -> None:
    """The stop reason first, then output, time and memory, then the exit."""
    assert sandbox_run.judge(make_run(**changes), ITEM, kind) == outcome


def test_detect(tmp_path: Path) -> None:
    """The three formats are told apart by content, and anything else is an error."""
    native = tmp_path / "native"
    native.write_bytes(b"\x7fELF" + bytes(60))
    python = python_binary(tmp_path / "python", "print(1)\n")
    java = tmp_path / "java"
    with zipfile.ZipFile(java, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n")
    other = tmp_path / "other"
    other.write_text("#!/bin/sh\necho hi\n")
    assert sandbox_run.detect(native) == "native"
    assert sandbox_run.detect(python) == "python"
    assert sandbox_run.detect(java) == "java"
    with pytest.raises(sandbox_run.PrimitiveError, match="not a native executable"):
        sandbox_run.detect(other)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"time_limit": 0}, "time_limit of test main/1 is not a positive number"),
        ({"time_limit": True}, "time_limit of test main/1 is not a positive number"),
        ({"memory_limit": "64"}, "memory_limit of test main/1 is not a positive"),
        ({"binary": {"file": "in/../inputs.json"}}, "binary of test main/1 is outside"),
        ({"input": {"file": "in/none"}}, "input of test main/1 is not in the working"),
        ({"input": "in/2/input"}, "input of test main/1 is not a file"),
        ({"args": 3}, "args of test main/1 is not text"),
    ],
)
def test_inputs_it_cannot_use_are_an_error(
    tmp_path: Path, inputs: dict[str, Any], message: str
) -> None:
    """Anything that stops the primitive working is `error` alone, in one sentence."""
    batch_inputs(tmp_path, b"", [("main/1", "")])
    document = json.loads((tmp_path / "inputs.json").read_text())
    document["batch"][0]["inputs"].update(inputs)
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    assert sandbox_run.main(["sandbox-run", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert set(result) == {"schema_version", "error"}
    assert message in result["error"]


@pytest.mark.parametrize(
    ("tests", "message"),
    [
        (["main/1", "main/1"], "for the same test"),
        (["main"], "is not a test id"),
        (["main/../1"], "is not a test id"),
        (["main/1/2"], "is not a test id"),
    ],
)
def test_items_must_be_for_distinct_tests(
    tmp_path: Path, tests: list[str], message: str
) -> None:
    """Each item names one test, `<group>/<test>`, and no two the same one."""
    batch_inputs(tmp_path, b"", [(f"main/{n}", "") for n in range(len(tests))])
    document = json.loads((tmp_path / "inputs.json").read_text())
    for entry, test in zip(document["batch"], tests, strict=True):
        entry["test"] = test
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    sandbox_run.main(["sandbox-run", str(tmp_path)])
    assert message in json.loads((tmp_path / "outputs.json").read_text())["error"]


def test_a_binary_in_no_known_format_is_an_error(tmp_path: Path) -> None:
    """A binary sandbox-run cannot run is the step failing, not the contestant."""
    batch_inputs(tmp_path, b"#!/bin/sh\n", [("main/1", "")])
    sandbox_run.main(["sandbox-run", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert "not a native executable" in result["error"]


def test_every_outcome(run: Any, tmp_path: Path) -> None:
    """One batch reaches each outcome, keeps each output and measures each run,
    answering in the batch's order under each item's test, with each output
    under the item's place in the batch, since a test id holds a `/`.
    """
    tests = ["samples/ok", "main/spin", "main/sleep", "main/grow", "main/fail"]
    cases = ["double\n21\n", "spin\n", "sleep\n", "grow\n", "fail\n"]
    items = list(zip(tests, cases, strict=True))
    result = run(items, time_limit=0.5, memory_limit=64)
    outputs = by_test(result)
    assert [entry["test"] for entry in result["batch"]] == tests
    assert {test: entry["outcome"] for test, entry in outputs.items()} == {
        "samples/ok": "accepted",
        "main/spin": "time_limit",
        "main/sleep": "time_limit",
        "main/grow": "memory_limit",
        "main/fail": "runtime_error",
    }
    work = tmp_path / "work"
    assert [entry["output"]["file"] for entry in outputs.values()] == [
        f"out/{number}/output" for number in range(1, 6)
    ]
    assert printed(work, outputs["samples/ok"]) == "42\n"
    assert printed(work, outputs["main/fail"]) == "partial\n"
    for entry in outputs.values():
        assert set(entry) == {"output", "time_ms", "memory_kb", "outcome"}
        assert isinstance(entry["time_ms"], int)
        assert isinstance(entry["memory_kb"], int)
        assert entry["memory_kb"] > 0
    assert outputs["main/grow"]["memory_kb"] > 64 * 1024
    assert outputs["samples/ok"]["memory_kb"] < 64 * 1024
    assert not list(work.glob(".sandbox-run-*"))


def test_a_run_stopped_at_its_time_limit_reports_at_least_the_limit(
    run: Any,
) -> None:
    """Stopped by the CPU limit or by the wall clock, a run reports at least
    the time limit, though one that slept used next to no CPU.
    """
    items = [("main/spin", "spin\n"), ("main/sleep", "sleep\n")]
    outputs = by_test(run(items, time_limit=0.3))
    assert outputs["main/spin"]["outcome"] == "time_limit"
    assert outputs["main/sleep"]["outcome"] == "time_limit"
    assert 300 <= outputs["main/spin"]["time_ms"] < 1300
    assert outputs["main/sleep"]["time_ms"] == 300


@pytest.mark.parametrize(
    ("args", "argv"),
    [
        ("--seed 7", ["--seed", "7"]),
        (
            "  --threshold   0.5 --heuristic true ",
            ["--threshold", "0.5", "--heuristic", "true"],
        ),
        ("", []),
        (None, []),
    ],
)
def test_args_reach_the_command_line_split_on_spaces(
    run: Any, tmp_path: Path, args: str | None, argv: list[str]
) -> None:
    """`args` is appended to the binary's command line, split on spaces, with
    no empty arguments; absent, the binary gets none.
    """
    given = {"main/1": args} if args is not None else None
    outputs = by_test(run([("main/1", "argv\n")], args=given))
    assert outputs["main/1"]["outcome"] == "accepted"
    assert printed(tmp_path / "work", outputs["main/1"]) == f"{argv!r}\n"


def test_args_are_each_items_own(run: Any, tmp_path: Path) -> None:
    items = [("main/1", "argv\n"), ("main/2", "argv\n"), ("main/3", "argv\n")]
    outputs = by_test(run(items, args={"main/1": "a b", "main/3": "c"}))
    work = tmp_path / "work"
    assert [printed(work, outputs[test]) for test, _ in items] == [
        "['a', 'b']\n",
        "[]\n",
        "['c']\n",
    ]


def test_args_no_command_line_can_carry_are_a_runtime_error(
    run: Any, tmp_path: Path
) -> None:
    """An argument holding a NUL character, or longer than the kernel takes,
    is the contestant's input failing, never an error of the step.
    """
    items = [("main/nul", "argv\n"), ("main/long", "argv\n"), ("main/ok", "argv\n")]
    args = {"main/nul": "a \0 b", "main/long": "x" * (256 * 1024)}
    outputs = by_test(run(items, args=args))
    assert {test: entry["outcome"] for test, entry in outputs.items()} == {
        "main/nul": "runtime_error",
        "main/long": "runtime_error",
        "main/ok": "accepted",
    }
    for entry in outputs.values():
        assert set(entry) == {"output", "time_ms", "memory_kb", "outcome"}
    assert printed(tmp_path / "work", outputs["main/nul"]) == ""


def test_output_over_the_limit(run: Any, tmp_path: Path) -> None:
    """Printing too much is an output limit, and the output is cut at the limit."""
    result = run([("main/flood", "flood\n")], time_limit=10)
    assert by_test(result)["main/flood"]["outcome"] == "output_limit"
    output = tmp_path / "work" / "out" / "1" / "output"
    assert output.stat().st_size == sandbox_run.OUTPUT_LIMIT


def test_processes_left_behind_are_killed(run: Any) -> None:
    """A process a run leaves behind, even in its own session, is killed."""
    result = run([("main/linger", "linger\n")])
    assert by_test(result)["main/linger"]["outcome"] == "accepted"
    assert sandbox_run.child_pids() == []


def test_runs_write_only_their_own_directory(run: Any, tmp_path: Path) -> None:
    """A run cannot write the working directory or the rest of /tmp."""
    work = tmp_path / "work"
    run([("main/escape", f"escape\n{work}\n")])
    lines = (work / "out" / "1" / "output").read_text().splitlines()
    assert lines == [
        f"refused {work}/escaped 13",
        f"refused {work}/inputs.json 13",
        "refused /tmp/escaped 13",
        "cwd ['here']",
    ]


def test_a_run_that_kills_its_launcher_is_a_runtime_error(
    run: Any, tmp_path: Path
) -> None:
    """From Landlock ABI 6 the run may not signal `sandbox-exec` at all; below
    it, killing `sandbox-exec` leaves no report and the run is judged on what
    the program measured itself. Either way it is the run's own runtime
    error, never an error of the step.
    """
    result = run([("main/parricide", "parricide\n"), ("main/ok", "double\n21\n")])
    outputs = by_test(result)
    assert outputs["main/parricide"]["outcome"] == "runtime_error"
    assert outputs["main/ok"]["outcome"] == "accepted"
    said = printed(tmp_path / "work", outputs["main/parricide"]).splitlines()
    if sandbox_run.landlock_abi() >= 6:
        assert said == [f"refused {errno.EPERM}"]


def test_a_run_that_stops_its_launcher_is_still_ended_on_time(run: Any) -> None:
    """A binary that sends SIGSTOP to `sandbox-exec`, where Landlock lets it,
    and then spins is stopped at its time limit, and the whole item is over
    within a second or so of that, not after a long wait for the helper.
    """
    started = time.monotonic()
    result = run([("main/freeze", "freeze\n")], time_limit=0.5)
    elapsed = time.monotonic() - started
    assert by_test(result)["main/freeze"]["outcome"] == "time_limit"
    assert elapsed < 4


def test_a_run_the_kernel_killed_for_memory_is_over_the_memory_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 2\n")
    monkeypatch.setattr(sandbox_run, "OOM_EVENTS", events)
    item = sandbox_run.Item(
        "main/x",
        1,
        tmp_path / "binary",
        tmp_path / "input",
        time_limit=1.0,
        memory_limit=64,
    )
    run = sandbox_run.Run(-9, 0.1, 0.2, 1000, 0, None, out_of_memory=True)

    assert sandbox_run.oom_kills() == 2
    assert sandbox_run.judge(run, item, "native") == "memory_limit"


def test_work_in_a_child_counts_against_the_time_limit(run: Any) -> None:
    """A child the binary never waits for still spends the run's time."""
    result = run([("main/hidden-work", "hidden-work\n")], time_limit=0.5)
    outputs = by_test(result)
    assert outputs["main/hidden-work"]["outcome"] == "time_limit"
    assert outputs["main/hidden-work"]["time_ms"] >= 500


def test_memory_in_a_child_counts_against_the_memory_limit(run: Any) -> None:
    result = run([("main/hidden-memory", "hidden-memory\n")], memory_limit=64)
    outputs = by_test(result)
    assert outputs["main/hidden-memory"]["outcome"] == "memory_limit"
    assert outputs["main/hidden-memory"]["memory_kb"] > 64 * 1024


def test_memory_spread_over_processes_is_added_up(run: Any) -> None:
    """Two processes that each hold 40 MB of their own at the same moment are
    over a 64 MB limit together, though each alone is under it.
    """
    result = run([("main/split", "split-memory\n")], time_limit=3, memory_limit=64)
    outputs = by_test(result)
    assert outputs["main/split"]["outcome"] == "memory_limit"
    assert outputs["main/split"]["memory_kb"] > 64 * 1024


def test_a_forked_child_is_not_charged_again_for_what_it_shares(run: Any) -> None:
    """A child forked from a binary holding 40 MB, which touches nothing of its
    own, costs the run almost nothing: the pages it shares with its parent
    count once, not once in each process.
    """
    items = [("main/alone", "hold-memory\n"), ("main/shared", "shared-memory\n")]
    outputs = by_test(run(items, time_limit=3, memory_limit=64))
    alone, shared = outputs["main/alone"], outputs["main/shared"]
    assert alone["outcome"] == "accepted"
    assert shared["outcome"] == "accepted"
    assert alone["memory_kb"] > 40 * 1024
    assert shared["memory_kb"] < alone["memory_kb"] + 4 * 1024


def test_what_the_cgroup_holds_beyond_the_start_of_a_run_counts(tmp_path: Path) -> None:
    """Everything charged to the run's cgroup beyond what it held when the
    reading began counts, but for pages of files on disk; files in a tmpfs,
    which the cgroup counts as file pages too, count.
    """
    (tmp_path / "memory.current").write_text(str(910 * 2**20))
    (tmp_path / "memory.stat").write_text(
        f"anon {10 * 2**20}\nfile {900 * 2**20}\nshmem 0\n"
    )
    memory = sandbox_run.RunMemory(cgroup=tmp_path)
    (tmp_path / "memory.current").write_text(str(84 * 2**20))
    (tmp_path / "memory.stat").write_text(
        f"anon {20 * 2**20}\nfile {60 * 2**20}\nshmem {50 * 2**20}\nsock {4 * 2**20}\n"
    )

    memory.read(set())

    assert memory.peak_kb == (84 - 10 - 10) * 1024


def test_a_cgroup_with_no_memory_accounting_adds_no_reading(tmp_path: Path) -> None:
    memory = sandbox_run.RunMemory(cgroup=tmp_path / "absent")

    memory.read(set())

    assert (memory.held_before, memory.peak_kb) == (None, 0)


def test_a_run_outside_a_cgroup_of_its_own_is_warned_about(
    run: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test machine's cgroup holds other processes too, so its memory is
    not read, and the program says so.
    """
    assert sandbox_run.own_cgroup() is None

    outputs = by_test(run([("main/ok", "double\n21\n")]))

    assert outputs["main/ok"]["outcome"] == "accepted"
    assert sandbox_run.NO_CGROUP in capsys.readouterr().err


def test_the_total_is_read_only_when_it_could_raise_the_peak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the processes' total walks every page they hold, so it is
    skipped while their resident memory added up is not above the peak, and
    read again only after `TOTAL_SPACING` times its own cost has passed.
    """
    resident = {1: (30_000, 30_000)}
    read: list[int] = []

    def proportional(pid: int, resident_kb: int) -> int:
        read.append(pid)
        return resident_kb

    monkeypatch.setattr(sandbox_run, "resident_memory", resident.__getitem__)
    monkeypatch.setattr(sandbox_run, "proportional_memory", proportional)
    memory = sandbox_run.RunMemory()
    memory.read({1})
    assert (memory.peak_kb, read) == (30_000, [])
    resident[2] = (40_000, 40_000)
    memory.read({1, 2})
    assert (memory.peak_kb, sorted(read)) == (70_000, [1, 2])
    memory.total_due = float("inf")
    memory.read({1, 2})
    assert len(read) == 2
    memory.total_due = 0.0
    resident.update({1: (30_000, 10_000), 2: (40_000, 10_000)})
    memory.read({1, 2})
    assert (memory.peak_kb, len(read)) == (70_000, 2)


def test_a_native_binary_the_kernel_will_not_run_is_a_runtime_error(
    helper: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox_run, "HELPER", helper)
    batch_inputs(tmp_path, b"\x7fELF" + bytes(60), [("main/1", "")])
    sandbox_run.main(["sandbox-run", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    outputs = by_test(result)["main/1"]
    assert outputs["outcome"] == "runtime_error"
    assert (outputs["time_ms"], outputs["memory_kb"]) == (0, 0)


def test_the_report_reads_only_the_lines_sandbox_exec_writes() -> None:
    report = b"P 12\nR 0 5 6 7\nR 0 0 0 0\nZ 1\n\xff\nE x\nR 1"
    assert sandbox_run.parse_report(report) == {"P": [12], "R": [0, 5, 6, 7]}


def test_a_json_file_is_written_whole_past_a_planted_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    (tmp_path / "outputs.json.partial").symlink_to(target)
    sandbox_run.write_json(tmp_path / "outputs.json", {"schema_version": 5})
    assert target.read_text() == "untouched"
    assert not (tmp_path / "outputs.json").is_symlink()
    assert json.loads((tmp_path / "outputs.json").read_text()) == {"schema_version": 5}


def test_a_run_reads_neither_another_runs_files_nor_the_steps(
    run: Any, tmp_path: Path
) -> None:
    """A later run cannot read an earlier run's output or input, the step's
    own files, or list the working directory or /tmp, so nothing one test
    leaves reaches the next; the system's files it can.
    """
    work = str(tmp_path / "work")
    result = run([("main/first", "double\n21\n"), ("main/peek", peek_input(work))])
    outputs = by_test(result)
    assert outputs["main/peek"]["outcome"] == "accepted"
    assert printed(tmp_path / "work", outputs["main/first"]) == "42\n"
    lines = printed(tmp_path / "work", outputs["main/peek"]).splitlines()
    assert lines == peeked(work)


def test_without_landlock_nothing_runs(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kernel without Landlock, or with Landlock turned off, is an error of
    the step, never a run left unconfined.
    """
    monkeypatch.setattr(sandbox_run, "landlock_abi", lambda: 0)
    result = run([("main/ok", "double\n21\n")])
    assert set(result) == {"schema_version", "error"}
    assert "Landlock" in result["error"]
    assert "5.13" in result["error"]


def test_a_ruleset_the_kernel_refuses_is_an_error(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(directory: Path, readable: list[Path], abi: int) -> int:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(sandbox_run, "landlock_ruleset", refuse)
    result = run([("main/ok", "double\n21\n")])
    assert "could not be confined with Landlock" in result["error"]


def test_the_launcher_starts_nothing_without_a_ruleset(helper: str) -> None:
    """`sandbox-exec` refuses to run a binary it has no Landlock ruleset for."""
    read_end, write_end = os.pipe()
    done = subprocess.run(
        [helper, str(write_end), "-1", "/bin/true"], pass_fds=(write_end,)
    )
    os.close(write_end)
    report = os.read(read_end, 64)
    os.close(read_end)
    assert done.returncode == 2
    assert sandbox_run.parse_report(report) == {"L": [errno.EBADF]}
