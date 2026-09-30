# primitive-sandbox-run

The `unicon/sandbox-run` primitive: it runs one compiled binary once per test,
feeding each test's input on standard input, and reports what the binary
printed, the CPU time and memory it used, and an outcome. It is the second
step of the `unicon/classic` workflow, between `unicon/compile` and
`unicon/diff-check`.

This repo holds the image, `ghcr.io/uniconhq/primitive-sandbox-run`, built
from the `Dockerfile`; the program the image runs, `src/sandbox_run.py`; the
small C program every binary is started through, `src/sandbox-exec.c`; and
`primitive.yaml`, the declaration the forge compiler reads to type-check a
workflow that uses the primitive. The program speaks the primitive contract,
`primitive.schema.json` version 3, published by the
[runner](https://github.com/uniconhq/runner).

## What it takes and returns

The primitive takes a batch (`batch: true`): one container runs every test,
so a hundred tests cost one container start rather than a hundred. Each item
of the batch has these inputs and outputs.

| | Name | Type | What it is |
|---|---|---|---|
| Input | `binary` | file | The binary from compile, in one of the formats below |
| Input | `input` | file | What the binary reads on standard input |
| Input | `time_limit` | number | Seconds of CPU time the run may use |
| Input | `memory_limit` | number | Megabytes of memory the run may use |
| Output | `output` | file | `out/<test id>/output`, what the binary printed on standard output |
| Output | `time_ms` | number | CPU time used, user plus system, in milliseconds |
| Output | `memory_kb` | number | Peak resident memory of the binary's process, in kilobytes |
| Output | `outcome` | outcome | `accepted`, `time_limit`, `memory_limit`, `output_limit` or `runtime_error` |

A run that goes over a limit or crashes is an ordinary result for that test,
never a failure of the step: the other tests still run, and the harness skips
the later steps of that test only.

`outputs.json` carries `error` instead, and nothing else, only when the
primitive could not work at all: `inputs.json` is missing, is not JSON or is
for another contract version; a file is missing or lies outside `in/`; a
limit is not a positive number; two items share an id, or an id cannot name
a directory; the binary is in none of the three formats; the interpreter a
Python or Java binary needs could not be started; the machine's kernel does
not offer Landlock, or a run could not be put under its Landlock rules; or
processes a run left behind could not be stopped. The harness turns an error into a `system_error` verdict, so no
contestant is graded by a broken step. A native binary the kernel refuses to
run as built is the contestant's program failing, a `runtime_error`.

## How one run goes

For each item, in order:

1. A fresh, empty directory is made under `/tmp` and becomes the run's
   working directory, `HOME` and `TMPDIR`. The environment holds only those,
   `PATH` and `LANG=C.UTF-8`.
2. The input file is opened as standard input, `out/<id>/output` as standard
   output, and standard error goes nowhere.
3. The binary is started through `sandbox-exec`, with its limits already set
   (below), and watched until it ends.
4. Every process the run left behind is killed and reaped, and the directory
   is removed.

Everything a run prints is kept, up to the output limit, even when it failed.

## Limits and outcomes

| Limit | How it is held |
|---|---|
| CPU time: `time_limit` seconds | Every process of the run, added up, read every 5 ms from `/proc` and the binary killed once over; `RLIMIT_CPU` at the limit rounded up plus a second as a backstop. The time reported is the binary's own with its waited-for children, from `wait4`, plus every process it left behind, which this program reaps, so work hidden in a child that is never waited for still counts |
| Wall-clock time: twice `time_limit` plus one second | Checked every 5 ms. It stops a run that sleeps or waits without using the CPU; the margin keeps a busy machine from turning a fast run into a time limit |
| Memory: `memory_limit` megabytes | The highest peak resident memory of any one process of the run, read every 5 ms from `/proc` and the binary killed once over, then checked again against the final peak. Forked processes share pages, so they are not added up; the container's own limit holds their total. Java also gets `-Xmx` at the limit |
| Output: 32 MB | Checked every 5 ms, and `RLIMIT_FSIZE` just above it, so no write can go further; an output that went over is cut to 32 MB |
| Stack | `RLIMIT_STACK` set to the memory limit, so deep recursion is bounded by memory rather than the default 8 MB |

The outcome is decided in this order: the limit the run was stopped for, if
it was; `output_limit` if the output went over or the binary was killed by
`SIGXFSZ`; `time_limit` if the CPU time or the wall-clock time went over or
the binary was killed by `SIGXCPU`; `memory_limit` if the peak memory went
over, or a Java binary exited with code 3, which is how
`-XX:+ExitOnOutOfMemoryError` ends a JVM that ran out of heap; then
`runtime_error` for any other non-zero exit or signal; otherwise `accepted`.

Memory is resident memory, the pages the binary actually touched, so an
array that is declared but never used does not count against it.

**Why `sandbox-exec`.** Linux carries a process's peak resident memory across
`exec`, so a binary started straight from the Python program would report at
least the interpreter's own 20 MB. `sandbox-exec` is a static C program of a
few hundred kilobytes: it forks, puts the child under the run's Landlock
rules, runs the binary in it, waits for it and reports its exit status, CPU
time and peak memory from `wait4` on a pipe the binary does not inherit. A C++ program that does nothing reports about
1.5 MB, which is what it used.

## Keeping runs apart

Every test is a new process in a new, empty directory, and nothing a run does
is visible to the next one:

- **Leftover processes.** The program is the child subreaper of everything it
  starts, so a process that outlives its run, even one that started its own
  session, becomes the program's child. After every run each such child is
  killed and reaped before the next run starts.
- **Files.** Every run is put under [Landlock](https://docs.kernel.org/userspace-api/landlock.html).
  It may create, change or remove files only inside its own directory, plus
  write to `/dev/null`. It may read only its own directory, its binary, the
  system's directories (`/usr`, `/bin`, `/sbin`, the `/lib` directories,
  `/etc`, `/proc`, `/sys`), `/dev/null`, `/dev/zero` and the random devices,
  and for a Python binary the interpreter's installation; its input arrives
  already open on standard input. It cannot read or list `/work` or `/tmp`,
  so it cannot see another test's input or output or `inputs.json`, nor
  change the files `outputs.json` is built from, and nothing it writes is
  there for a later test to read. Running a file needs reading it, so a run
  can start its binary and the system's programs and nothing else.
- **The program that watches it.** A run cannot trace the program or
  `sandbox-exec`, or open their memory or file descriptors through `/proc`:
  both make themselves not dumpable, and Landlock, which `sandbox-exec`
  enforces on the binary alone just before starting it, forbids tracing
  anything outside the run as well. From Landlock ABI 6 a run cannot signal
  either of them. On an older kernel it can kill `sandbox-exec`, its parent;
  it then ends as its own `runtime_error`, judged on what the program
  measured itself. It cannot stop the program: the program is the
  container's first process, which the kernel keeps signals from unless it
  handles them, and it ignores the one Python would handle, SIGINT.
- **Memory pressure.** Every run's processes carry the highest out-of-memory
  score, so when the container reaches its memory limit the kernel kills
  the run rather than the program.

**The kernel it needs.** Landlock at ABI 1 or later: Linux 5.13 or later,
with Landlock among the kernel's security modules (`lsm=`; Docker Desktop's
kernel has it, and `/sys/kernel/security/lsm` on a machine lists it).
Without it nothing runs: `outputs.json` carries an `error`, which the harness
turns into a `system_error`, never a grade. ABI 1 already confines reads and
writes, which is what keeps tests apart, so the program asks for no more.
Later ABIs add to it where the kernel has them: from ABI 2 a run may rename
within its own directory (below it, Landlock refuses every rename across
directories), from ABI 3 `truncate` of a file named by path is confined too
(below it a run could shorten or lengthen a file it cannot open, such as
another test's output, spoiling only its own results), and from ABI 6 it
cannot signal `sandbox-exec`, as above. Requiring ABI 6 would turn away most
machines on a 6.1 to 6.11 kernel for a gap that costs only the run itself.
The worker's self-test on a grading machine (feature 12) should check the
kernel offers Landlock before the machine takes runs.

What a run can still learn of the tests before it is none of their files'
contents: that a file it can name exists, and its size, mode and times,
which Landlock does not confine and which a run can change on a file of its
own user it can name; the time on the clock; and the free space of `/work`.

The container itself is the harness's: no network, a read-only root
filesystem, every capability dropped, `no-new-privileges`, Docker's built-in
seccomp profile and a non-root user; the image's user is 65532. The only
writable places are `/work` and a small tmpfs at `/tmp`. Native binaries are
copied into a scratch directory under `/work`, removed before the program
exits, because `/tmp` may be mounted `noexec`.

## The container's limits

`limits` in `primitive.yaml` are per test: 5 s of time and CPU, 256 MB of
memory, 128 processes and 64 MB of output. `limits_from` raises them from
each test's own limits so the container never dies before the program's own
limit does: time and CPU to `2 × time_limit + 3` seconds (the wall-clock
limit plus room to start and clean up), and memory to `memory_limit + 256` MB
(room for the program itself and for the moment between two memory
readings). The harness multiplies time and CPU by the number of tests in the
batch.

## The binary format

`binary` is one file, in one of three formats. compile writes them, and
sandbox-run tells them apart by their content.

| Language | The binary | How sandbox-run runs it |
|---|---|---|
| `c`, `cpp` | A statically linked ELF executable | Copied to a scratch directory, marked executable and run directly |
| `python` | A Python zip application: a zip holding the source as `__main__.py`, behind the line `#!/usr/bin/env python3` | `python3 -I -B binary`, on the image's Python 3.14 |
| `java` | A runnable jar: the compiled classes and a manifest naming the main class | `java -Xmx<memory_limit>m -Xss64m -XX:+UseSerialGC -XX:-UsePerfData -XX:+ExitOnOutOfMemoryError -Djava.io.tmpdir=. -jar binary`, on the image's OpenJDK 21 |

Static linking means a native binary needs nothing from the image it runs
in. A Python or Java binary needs the interpreter it was checked against, so
both images are built from the same pinned `python:3.14-slim` base and
install the same Debian OpenJDK 21 packages: the JDK in compile, the runtime
in sandbox-run. A change to any of the three formats, or to either image's
Python or Java, is made in both repos together.

## Layout

```
Dockerfile                  the image: python:3.14-slim, OpenJDK 21 runtime, sandbox-exec
src/sandbox_run.py          the program, installed as /usr/local/bin/sandbox-run
src/sandbox-exec.c          the launcher, built static and installed as /usr/local/bin/sandbox-exec
primitive.yaml              the declaration, without the image line
scripts/check_declaration.py  checks primitive.yaml against the runner's schema
tests/                      unit tests, and image tests that run it on Docker
```

`primitive.yaml` has no `image` line here: bootstrap writes the image by
digest, from the release manifest, into the version it creates at the forge.

## Running it locally

Python 3.14 with [uv](https://docs.astral.sh/uv/), and Docker for the image
tests.

```
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run python scripts/check_declaration.py path/to/primitive.schema.json
uv run pytest
```

`uv run pytest` runs everything: the unit tests, which need Linux and gcc
(they build `sandbox-exec` and run Python binaries on the interpreter running
the tests), and the image tests (marked `image`), which build the image and
run it under the harness's sandbox flags and the container limits the
declaration gives. Elsewhere only the image and declaration tests are
collected. Without Docker the image tests are skipped. They use
`PRIMITIVE_IMAGE` instead of building when it is set. They run the image on
a Docker volume holding the working directory, as the harness gives a step
one: Landlock's rules do not hold on a directory Docker Desktop shares from
Windows or macOS, whose files it cannot tell apart from one open to the next.

The image tests build their C++, Java and Python binaries with the compile
primitive's own image, so they are also the check that the two primitives
agree on the binary format. That image is `COMPILE_IMAGE`, or built from a
`primitive-compile` checkout beside this one; without either, those tests
are skipped. CI builds it from `primitive-compile`'s `main`. The checks on
every `inputs.json` and `outputs.json` the tests see read the schema from
`PRIMITIVE_SCHEMA`, or from a `runner` checkout beside this one.

CI runs the checks and unit tests in one job, against `primitive.schema.json`
from the runner release named in the workflow, and builds the image and runs
the image tests in another.

## Releasing

Push a tag `v1.2.3` on `main`. The release workflow refuses a tag whose
commit is not on `main`, a tag that differs from the version in
`pyproject.toml`, and a tag that is not a release of the version
`primitive.yaml` declares (`v1.2.3` is a release of `v1`). It runs the same
checks as CI, pushes the image as
`ghcr.io/uniconhq/primitive-sandbox-run:v1.2.3`, and creates a GitHub release
with `images.json`, which names the image by digest in the same shape as the
runner's, and `primitive.yaml` attached, and the digest in the notes.
`deploy/images.json` pins that digest, and bootstrap writes it into the
forge's copy of `primitive.yaml`.

The first push creates the package on the organisation as **private**.
Grading machines pull it anonymously, so someone has to open the package on
the organisation's Packages page once, set its visibility to public, and add
this repo under Manage Actions access so later releases can keep pushing to
it.
