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
from support import PROGRAM, batch_inputs, by_id, peek_input, peeked, python_binary


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

    def batch(items: list[tuple[str, str]], **limits: float) -> dict[str, Any]:
        work = tmp_path / "work"
        batch_inputs(work, binary, items, **limits)
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


ITEM = sandbox_run.Item("1", Path("b"), Path("i"), time_limit=1.0, memory_limit=64)


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
        ({"time_limit": 0}, "time_limit of item 1 is not a positive number"),
        ({"time_limit": True}, "time_limit of item 1 is not a positive number"),
        ({"memory_limit": "64"}, "memory_limit of item 1 is not a positive number"),
        ({"binary": {"file": "in/../inputs.json"}}, "binary of item 1 is outside in/"),
        ({"input": {"file": "in/none"}}, "input of item 1 is not in the working"),
        ({"input": "in/2/1.in"}, "input of item 1 is not a file"),
    ],
)
def test_inputs_it_cannot_use_are_an_error(
    tmp_path: Path, inputs: dict[str, Any], message: str
) -> None:
    """Anything that stops the primitive working is `error` alone, in one sentence."""
    batch_inputs(tmp_path, b"", [("1", "")])
    document = json.loads((tmp_path / "inputs.json").read_text())
    document["batch"][0]["inputs"].update(inputs)
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    assert sandbox_run.main(["sandbox-run", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert set(result) == {"schema_version", "error"}
    assert message in result["error"]


@pytest.mark.parametrize(
    ("ids", "message"),
    [(["1", "1"], "same id"), ([".."], "cannot name a directory"), (["a/b"], "cannot")],
)
def test_item_ids_must_name_distinct_directories(
    tmp_path: Path, ids: list[str], message: str
) -> None:
    """Each item's output goes to out/<id>/, so an id must be a distinct plain name."""
    batch_inputs(tmp_path, b"", [(str(n), "") for n in range(len(ids))])
    document = json.loads((tmp_path / "inputs.json").read_text())
    for entry, item_id in zip(document["batch"], ids, strict=True):
        entry["id"] = item_id
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    sandbox_run.main(["sandbox-run", str(tmp_path)])
    assert message in json.loads((tmp_path / "outputs.json").read_text())["error"]


def test_a_binary_in_no_known_format_is_an_error(tmp_path: Path) -> None:
    """A binary sandbox-run cannot run is the step failing, not the contestant."""
    batch_inputs(tmp_path, b"#!/bin/sh\n", [("1", "")])
    sandbox_run.main(["sandbox-run", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert "not a native executable" in result["error"]


def test_every_outcome(run: Any, tmp_path: Path) -> None:
    """One batch reaches each outcome, keeps each output and measures each run."""
    result = run(
        [
            ("ok", "double\n21\n"),
            ("spin", "spin\n"),
            ("sleep", "sleep\n"),
            ("grow", "grow\n"),
            ("fail", "fail\n"),
        ],
        time_limit=0.5,
        memory_limit=64,
    )
    outputs = by_id(result)
    assert [entry["id"] for entry in result["batch"]] == [
        "ok",
        "spin",
        "sleep",
        "grow",
        "fail",
    ]
    assert {name: entry["outcome"] for name, entry in outputs.items()} == {
        "ok": "accepted",
        "spin": "time_limit",
        "sleep": "time_limit",
        "grow": "memory_limit",
        "fail": "runtime_error",
    }
    work = tmp_path / "work"
    assert (work / "out" / "ok" / "output").read_text() == "42\n"
    assert (work / "out" / "fail" / "output").read_text() == "partial\n"
    assert outputs["ok"]["output"] == {"file": "out/ok/output"}
    assert 500 <= outputs["spin"]["time_ms"] < 1500
    assert outputs["sleep"]["time_ms"] < 500
    assert outputs["grow"]["memory_kb"] > 64 * 1024
    assert 0 < outputs["ok"]["memory_kb"] < 64 * 1024
    assert not list(work.glob(".sandbox-run-*"))


def test_output_over_the_limit(run: Any, tmp_path: Path) -> None:
    """Printing too much is an output limit, and the output is cut at the limit."""
    result = run([("flood", "flood\n")], time_limit=10)
    assert by_id(result)["flood"]["outcome"] == "output_limit"
    output = tmp_path / "work" / "out" / "flood" / "output"
    assert output.stat().st_size == sandbox_run.OUTPUT_LIMIT


def test_processes_left_behind_are_killed(run: Any) -> None:
    """A process a run leaves behind, even in its own session, is killed."""
    result = run([("linger", "linger\n")])
    assert by_id(result)["linger"]["outcome"] == "accepted"
    assert sandbox_run.child_pids() == []


def test_runs_write_only_their_own_directory(run: Any, tmp_path: Path) -> None:
    """A run cannot write the working directory or the rest of /tmp."""
    work = tmp_path / "work"
    run([("escape", f"escape\n{work}\n")])
    lines = (work / "out" / "escape" / "output").read_text().splitlines()
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
    result = run([("parricide", "parricide\n"), ("ok", "double\n21\n")])
    outputs = by_id(result)
    assert outputs["parricide"]["outcome"] == "runtime_error"
    assert outputs["ok"]["outcome"] == "accepted"
    said = (tmp_path / "work" / "out" / "parricide" / "output").read_text().splitlines()
    if sandbox_run.landlock_abi() >= 6:
        assert said == [f"refused {errno.EPERM}"]


def test_a_run_that_stops_its_launcher_is_still_ended_on_time(run: Any) -> None:
    """A binary that sends SIGSTOP to `sandbox-exec`, where Landlock lets it,
    and then spins is stopped at its time limit, and the whole item is over
    within a second or so of that, not after a long wait for the helper.
    """
    started = time.monotonic()
    result = run([("freeze", "freeze\n")], time_limit=0.5)
    elapsed = time.monotonic() - started
    assert by_id(result)["freeze"]["outcome"] == "time_limit"
    assert elapsed < 4


def test_a_run_the_kernel_killed_for_memory_is_over_the_memory_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 2\n")
    monkeypatch.setattr(sandbox_run, "OOM_EVENTS", events)
    item = sandbox_run.Item(
        "x", tmp_path / "binary", tmp_path / "input", time_limit=1.0, memory_limit=64
    )
    run = sandbox_run.Run(-9, 0.1, 0.2, 1000, 0, None, out_of_memory=True)

    assert sandbox_run.oom_kills() == 2
    assert sandbox_run.judge(run, item, "native") == "memory_limit"


def test_work_in_a_child_counts_against_the_time_limit(run: Any) -> None:
    """A child the binary never waits for still spends the run's time."""
    result = run([("hidden-work", "hidden-work\n")], time_limit=0.5)
    outputs = by_id(result)
    assert outputs["hidden-work"]["outcome"] == "time_limit"
    assert outputs["hidden-work"]["time_ms"] >= 500


def test_memory_in_a_child_counts_against_the_memory_limit(run: Any) -> None:
    result = run([("hidden-memory", "hidden-memory\n")], memory_limit=64)
    outputs = by_id(result)
    assert outputs["hidden-memory"]["outcome"] == "memory_limit"
    assert outputs["hidden-memory"]["memory_kb"] > 64 * 1024


def test_memory_spread_over_processes_is_added_up(run: Any) -> None:
    """Two processes that each hold 40 MB of their own at the same moment are
    over a 64 MB limit together, though each alone is under it.
    """
    result = run([("split", "split-memory\n")], time_limit=3, memory_limit=64)
    outputs = by_id(result)
    assert outputs["split"]["outcome"] == "memory_limit"
    assert outputs["split"]["memory_kb"] > 64 * 1024


def test_a_forked_child_is_not_charged_again_for_what_it_shares(run: Any) -> None:
    """A child forked from a binary holding 40 MB, which touches nothing of its
    own, costs the run almost nothing: the pages it shares with its parent
    count once, not once in each process.
    """
    items = [("alone", "hold-memory\n"), ("shared", "shared-memory\n")]
    outputs = by_id(run(items, time_limit=3, memory_limit=64))
    assert outputs["alone"]["outcome"] == "accepted"
    assert outputs["shared"]["outcome"] == "accepted"
    assert outputs["alone"]["memory_kb"] > 40 * 1024
    assert outputs["shared"]["memory_kb"] < outputs["alone"]["memory_kb"] + 4 * 1024


def test_memory_files_count_against_the_memory_limit(run: Any) -> None:
    """24 MB held and 48 MB more in memory files with no path is over a 64 MB
    limit, while 40 MB held is not. On a machine whose temporary directory is
    on disk, files a run writes there are not memory; the image tests check
    them in the step's tmpfs.
    """
    items = [("memfd", "memfd-memory\n"), ("alone", "hold-memory\n")]
    outputs = by_id(run(items, time_limit=3, memory_limit=64))
    assert outputs["memfd"]["outcome"] == "memory_limit"
    assert outputs["alone"]["outcome"] == "accepted"


def test_what_the_cgroup_holds_beyond_the_start_of_a_run_counts(tmp_path: Path) -> None:
    """The anonymous and shared memory the run's cgroup holds beyond what it
    held when the reading began counts, and pages of files on disk do not.
    """
    stat = tmp_path / "memory.stat"
    stat.write_text("anon 10485760\nfile 900000000\nshmem 0\n")
    memory = sandbox_run.RunMemory(stat=stat)
    stat.write_text("anon 20971520\nfile 0\nshmem 52428800\n")

    memory.read(set())

    assert memory.peak_kb == (20 + 50 - 10) * 1024


def test_a_cgroup_with_no_memory_stat_adds_no_reading(tmp_path: Path) -> None:
    memory = sandbox_run.RunMemory(stat=tmp_path / "absent")

    memory.read(set())

    assert (memory.held_before, memory.peak_kb) == (None, 0)


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
    batch_inputs(tmp_path, b"\x7fELF" + bytes(60), [("1", "")])
    sandbox_run.main(["sandbox-run", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert by_id(result)["1"]["outcome"] == "runtime_error"


def test_the_report_reads_only_the_lines_sandbox_exec_writes() -> None:
    report = b"P 12\nR 0 5 6 7\nR 0 0 0 0\nZ 1\n\xff\nE x\nR 1"
    assert sandbox_run.parse_report(report) == {"P": [12], "R": [0, 5, 6, 7]}


def test_a_json_file_is_written_whole_past_a_planted_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    (tmp_path / "outputs.json.partial").symlink_to(target)
    sandbox_run.write_json(tmp_path / "outputs.json", {"schema_version": 4})
    assert target.read_text() == "untouched"
    assert not (tmp_path / "outputs.json").is_symlink()
    assert json.loads((tmp_path / "outputs.json").read_text()) == {"schema_version": 4}


def test_a_run_reads_neither_another_runs_files_nor_the_steps(
    run: Any, tmp_path: Path
) -> None:
    """A later run cannot read an earlier run's output or input, the step's
    own files, or list the working directory or /tmp, so nothing one test
    leaves reaches the next; the system's files it can.
    """
    work = str(tmp_path / "work")
    result = run([("first", "double\n21\n"), ("peek", peek_input(work))])
    outputs = by_id(result)
    assert outputs["peek"]["outcome"] == "accepted"
    assert (tmp_path / "work" / "out" / "first" / "output").read_text() == "42\n"
    lines = (tmp_path / "work" / "out" / "peek" / "output").read_text().splitlines()
    assert lines == peeked(work)


def test_without_landlock_nothing_runs(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kernel without Landlock, or with Landlock turned off, is an error of
    the step, never a run left unconfined.
    """
    monkeypatch.setattr(sandbox_run, "landlock_abi", lambda: 0)
    result = run([("ok", "double\n21\n")])
    assert set(result) == {"schema_version", "error"}
    assert "Landlock" in result["error"]
    assert "5.13" in result["error"]


def test_a_ruleset_the_kernel_refuses_is_an_error(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(directory: Path, readable: list[Path], abi: int) -> int:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(sandbox_run, "landlock_ruleset", refuse)
    result = run([("ok", "double\n21\n")])
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
