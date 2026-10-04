"""The image, run on Docker under the harness's sandbox flags.

Python binaries are built here the way compile builds them; native and Java
binaries come from the compile primitive's own image, so these tests are also
the check that the two primitives agree on the binary format.
"""

import json
import time
from pathlib import Path

import pytest

from support import (
    PROGRAM,
    Check,
    Compiled,
    RunImage,
    batch_inputs,
    by_id,
    container_limits,
    peek_input,
    peeked,
    python_binary,
)

pytestmark = pytest.mark.image

CPP = """
#include <bits/stdc++.h>
int main() {
    std::string c; std::cin >> c;
    if (c == "double") { long long n; std::cin >> n; std::cout << n * 2 << "\\n"; }
    if (c == "spin") { volatile unsigned long x = 0; for (;;) x = x + 1; }
    if (c == "grow") {
        std::vector<std::vector<char>> v;
        for (;;) v.emplace_back(8 << 20, 'x');
    }
    if (c == "crash") { std::vector<int> v; return v.at(5); }
    if (c == "peek") {
        std::string p;
        while (std::cin >> p) {
            FILE* f = std::fopen(p.c_str(), "rb");
            std::cout << (f ? "read " : "refused ") << p << "\\n";
            if (f) std::fclose(f);
        }
    }
}
"""

C = """
#include <stdio.h>
int main(void) {
    long long n;
    if (scanf("%lld", &n) != 1) return 1;
    printf("%lld\\n", n * 2);
    return 0;
}
"""

JAVA = """
import java.util.*;
public class Main {
    public static void main(String[] args) {
        Scanner in = new Scanner(System.in);
        String c = in.next();
        if (c.equals("double")) System.out.println(in.nextLong() * 2);
        if (c.equals("grow")) {
            List<long[]> keep = new ArrayList<>();
            while (true) keep.add(new long[1 << 20]);
        }
        if (c.equals("crash")) throw new IllegalStateException("no");
    }
}
"""


def run_batch(
    run_image: RunImage,
    work: Path,
    binary: bytes,
    items: list[tuple[str, str]],
    time_limit: float = 1.0,
    memory_limit: float = 64,
) -> dict[str, dict[str, object]]:
    """Run one batch under the container limits the declaration gives it."""
    batch_inputs(work, binary, items, time_limit, memory_limit)
    limits = container_limits(time_limit, memory_limit, len(items))
    return by_id(run_image(work, limits))


def outcomes(outputs: dict[str, dict[str, object]]) -> dict[str, object]:
    """Each item's outcome."""
    return {item_id: entry["outcome"] for item_id, entry in outputs.items()}


def test_python_runs_reach_every_outcome(
    tmp_path: Path, run_image: RunImage, check: Check
) -> None:
    """One batch in the sandbox reaches each outcome and keeps each output."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("ok", "double\n21\n"),
        ("spin", "spin\n"),
        ("sleep", "sleep\n"),
        ("grow", "grow\n"),
        ("fail", "fail\n"),
    ]
    batch_inputs(work, binary, items, 0.5, 64)
    check(json.loads((work / "inputs.json").read_text()), "inputs_file")
    result = run_image(work, container_limits(0.5, 64, len(items)))
    check(result, "outputs_file")
    outputs = by_id(result)
    assert outcomes(outputs) == {
        "ok": "accepted",
        "spin": "time_limit",
        "sleep": "time_limit",
        "grow": "memory_limit",
        "fail": "runtime_error",
    }
    assert (work / "out" / "ok" / "output").read_text() == "42\n"
    assert (work / "out" / "fail" / "output").read_text() == "partial\n"
    assert sorted(path.name for path in work.iterdir()) == [
        "in",
        "inputs.json",
        "out",
        "outputs.json",
    ]


def test_output_over_the_limit_is_cut(tmp_path: Path, run_image: RunImage) -> None:
    """Printing past the output limit is `output_limit`, with the output cut there."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    outputs = run_batch(run_image, work, binary, [("flood", "flood\n")], time_limit=10)
    assert outcomes(outputs) == {"flood": "output_limit"}
    assert (work / "out" / "flood" / "output").stat().st_size == 32 * 1024 * 1024


def test_runs_are_kept_apart(tmp_path: Path, run_image: RunImage) -> None:
    """A run writes only its own directory, and nothing it leaves reaches the next."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("escape", "escape\n/work\n"),
        ("linger", "linger\n"),
        ("census", "census\n"),
    ]
    started = time.monotonic()
    outputs = run_batch(run_image, work, binary, items)
    assert time.monotonic() - started < 30
    assert outcomes(outputs) == {
        "escape": "accepted",
        "linger": "accepted",
        "census": "accepted",
    }
    assert (work / "out" / "escape" / "output").read_text().splitlines() == [
        "refused /work/escaped 13",
        "refused /work/inputs.json 13",
        "refused /tmp/escaped 13",
        "cwd ['here']",
    ]
    assert (work / "out" / "census" / "output").read_text() == "3\n"


def test_a_run_reads_nothing_another_run_left(
    tmp_path: Path, run_image: RunImage
) -> None:
    """In the sandbox, a later run cannot read an earlier run's output or its
    input, the step's own files, or list /work or /tmp; the system's files
    it can.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("first", "double\n21\n"), ("peek", peek_input("/work"))]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"first": "accepted", "peek": "accepted"}
    assert (work / "out" / "first" / "output").read_text() == "42\n"
    lines = (work / "out" / "peek" / "output").read_text().splitlines()
    assert lines == peeked("/work")


def test_a_hostile_run_cannot_reach_the_program_that_watches_it(
    tmp_path: Path, run_image: RunImage
) -> None:
    """In the sandbox: killing the launcher is the run's own runtime error,
    work and memory hidden in a child still count, the launcher's descriptors
    and the program's memory cannot be opened, and SIGINT does not stop the
    program.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("parricide", "parricide\n"),
        ("hidden-work", "hidden-work\n"),
        ("hidden-memory", "hidden-memory\n"),
        ("prying", "prying\n"),
        ("interrupt", "interrupt\n"),
        ("ok", "double\n21\n"),
    ]
    outputs = run_batch(run_image, work, binary, items, time_limit=0.5)
    assert outcomes(outputs) == {
        "parricide": "runtime_error",
        "hidden-work": "time_limit",
        "hidden-memory": "memory_limit",
        "prying": "accepted",
        "interrupt": "accepted",
        "ok": "accepted",
    }
    assert (work / "out" / "prying" / "output").read_text().splitlines() == [
        "refused 13",
        "refused 13",
    ]
    interrupted = (work / "out" / "interrupt" / "output").read_text().splitlines()
    assert interrupted[-1] == "still here"
    assert (work / "out" / "ok" / "output").read_text() == "42\n"


def test_memory_is_the_whole_runs(tmp_path: Path, run_image: RunImage) -> None:
    """In the sandbox, two processes holding 40 MB each are over a 64 MB limit
    together, also when both make themselves not dumpable so their pages
    cannot be read, and a child forked from a binary holding 40 MB is not
    charged again for the pages it shares with it.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("split", "split-memory\n"),
        ("undumpable", "undumpable-split-memory\n"),
        ("alone", "hold-memory\n"),
        ("shared", "shared-memory\n"),
    ]
    outputs = run_batch(run_image, work, binary, items, time_limit=3)
    assert outcomes(outputs) == {
        "split": "memory_limit",
        "undumpable": "memory_limit",
        "alone": "accepted",
        "shared": "accepted",
    }
    alone = outputs["alone"]["memory_kb"]
    shared = outputs["shared"]["memory_kb"]
    assert isinstance(alone, int) and isinstance(shared, int)
    assert shared < alone + 4 * 1024


def test_native_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled, check: Check
) -> None:
    """A C++ binary from compile runs, is measured by its own size, and hits limits."""
    binary = compiled("cpp", "main.cpp", CPP)
    work = tmp_path / "work"
    items = [
        ("ok", "double 21"),
        ("spin", "spin"),
        ("grow", "grow"),
        ("crash", "crash"),
    ]
    batch_inputs(work, binary, items, 1.0, 64)
    result = run_image(work, container_limits(1.0, 64, len(items)))
    check(result, "outputs_file")
    outputs = by_id(result)
    assert outcomes(outputs) == {
        "ok": "accepted",
        "spin": "time_limit",
        "grow": "memory_limit",
        "crash": "runtime_error",
    }
    assert (work / "out" / "ok" / "output").read_text() == "42\n"
    assert isinstance(outputs["ok"]["memory_kb"], int)
    assert outputs["ok"]["memory_kb"] < 8 * 1024


def test_a_native_run_reads_nothing_another_run_left(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """The same confinement holds for a native binary: it can read its own
    input on standard input and the system's files, and no other test's.
    """
    binary = compiled("cpp", "main.cpp", CPP)
    work = tmp_path / "work"
    paths = [
        "/work/out/first/output",
        "/work/in/2/first.in",
        "/work/inputs.json",
        "/etc/os-release",
    ]
    items = [("first", "double 21"), ("peek", " ".join(["peek", *paths]))]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"first": "accepted", "peek": "accepted"}
    lines = (work / "out" / "peek" / "output").read_text().splitlines()
    assert lines == [
        "refused /work/out/first/output",
        "refused /work/in/2/first.in",
        "refused /work/inputs.json",
        "read /etc/os-release",
    ]


def test_c_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """A C binary from compile runs under the sandbox's rules."""
    binary = compiled("c", "main.c", C)
    work = tmp_path / "work"
    outputs = run_batch(run_image, work, binary, [("1", "21\n"), ("2", "-4\n")])
    assert outcomes(outputs) == {"1": "accepted", "2": "accepted"}
    assert (work / "out" / "1" / "output").read_text() == "42\n"
    assert (work / "out" / "2" / "output").read_text() == "-8\n"


def test_java_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """A jar from compile runs on the image's Java; running out of heap is memory."""
    binary = compiled("java", "Main.java", JAVA)
    work = tmp_path / "work"
    items = [("ok", "double 21"), ("grow", "grow"), ("crash", "crash")]
    outputs = run_batch(run_image, work, binary, items, time_limit=3, memory_limit=128)
    assert outcomes(outputs) == {
        "ok": "accepted",
        "grow": "memory_limit",
        "crash": "runtime_error",
    }
    assert (work / "out" / "ok" / "output").read_text() == "42\n"


def test_python_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """A zip application from compile runs on the image's Python."""
    binary = compiled("python", "main.py", "print(int(input()) * 2)\n")
    work = tmp_path / "work"
    outputs = run_batch(run_image, work, binary, [("1", "21\n"), ("2", "-4\n")])
    assert outcomes(outputs) == {"1": "accepted", "2": "accepted"}
    assert (work / "out" / "2" / "output").read_text() == "-8\n"
