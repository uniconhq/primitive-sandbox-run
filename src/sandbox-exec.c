/*
 * sandbox-exec: start one program as a child and report what it used.
 *
 *     sandbox-exec REPORT_FD RULESET_FD PROGRAM [ARGUMENT...]
 *
 * sandbox-run starts this small static program instead of the binary itself,
 * with the run's limits already set, and this program forks and runs the
 * binary. The reason is memory: Linux carries a process's peak resident set
 * across exec, so a binary started straight from sandbox-run's Python would
 * report at least the interpreter's own size. Forked from this program it
 * starts from almost nothing and reports its own peak.
 *
 * RULESET_FD is a Landlock ruleset sandbox-run made for the run; without one
 * nothing is started. The child enforces it on itself just before exec, so
 * the binary and everything it starts are confined and this program is not:
 * from Landlock ABI 6 the binary cannot signal it, and from ABI 1 it cannot
 * trace it. This program also makes itself not dumpable, so the binary,
 * which runs as the same user, cannot open its file descriptors, the report
 * pipe among them, through /proc.
 *
 * Lines written to REPORT_FD, which the binary does not inherit:
 *
 *     P <pid>                                 the binary's process id
 *     L <errno>                               the ruleset could not be enforced
 *     E <errno>                               the binary could not be started
 *     R <status> <user_us> <system_us> <maxrss_kb>   how it ended and what it used
 */

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef __NR_landlock_restrict_self
#define __NR_landlock_restrict_self 446
#endif

static long long microseconds(struct timeval t)
{
    return (long long)t.tv_sec * 1000000 + t.tv_usec;
}

int main(int argc, char **argv)
{
    if (argc < 4)
        return 2;
    int report = atoi(argv[1]);
    int ruleset = atoi(argv[2]);
    if (fcntl(report, F_SETFD, FD_CLOEXEC) != 0)
        return 2;
    if (ruleset < 0 || fcntl(ruleset, F_SETFD, FD_CLOEXEC) != 0) {
        dprintf(report, "L %d\n", EBADF);
        return 2;
    }
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0)
        return 2;

    pid_t pid = fork();
    if (pid < 0)
        return 2;
    if (pid == 0) {
        if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0
            || syscall(__NR_landlock_restrict_self, ruleset, 0) != 0) {
            dprintf(report, "L %d\n", errno);
            _exit(127);
        }
        close(ruleset);
        execv(argv[3], argv + 3);
        dprintf(report, "E %d\n", errno);
        _exit(127);
    }
    close(ruleset);
    dprintf(report, "P %d\n", (int)pid);

    int status;
    struct rusage usage;
    while (wait4(pid, &status, 0, &usage) < 0)
        if (errno != EINTR)
            return 2;
    dprintf(report, "R %d %lld %lld %ld\n", status, microseconds(usage.ru_utime),
            microseconds(usage.ru_stime), usage.ru_maxrss);
    return 0;
}
