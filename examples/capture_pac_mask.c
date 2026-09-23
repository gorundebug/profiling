/* Read the kernel's instruction PAC mask before sampling, then detach.
 * No guessed VA width and no target code/register modifications.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>
#if defined(__aarch64__)
#include <asm/ptrace.h>
#include <elf.h>
#include <stdint.h>
#endif

int main(int argc, char **argv)
{
    char *end;
    long parsed;
    if (argc != 2) {
        fprintf(stderr, "usage: capture-pac-mask PID\n");
        return 1;
    }
    errno = 0;
    parsed = strtol(argv[1], &end, 10);
    if (errno || end == argv[1] || *end || parsed <= 0 || parsed > INT_MAX) {
        fprintf(stderr, "Invalid target PID\n");
        return 1;
    }
#if !defined(__aarch64__)
    puts("unsupported");
    return 0;
#else
    pid_t pid = (pid_t)parsed;
    int status, saved_error = 0, detach_signal = 0;
    struct user_pac_mask mask = {0};
    struct iovec io = { .iov_base = &mask, .iov_len = sizeof(mask) };
    if (ptrace(PTRACE_SEIZE, pid, NULL, NULL) == -1) {
        perror("PTRACE_SEIZE");
        return 1;
    }
    if (ptrace(PTRACE_INTERRUPT, pid, NULL, NULL) == -1) {
        perror("PTRACE_INTERRUPT");
        /* Exiting the tracer detaches an unstopped tracee as well. */
        return 1;
    }
    pid_t waited;
    do {
        waited = waitpid(pid, &status, __WALL);
    } while (waited == -1 && errno == EINTR);
    if (waited != pid || !WIFSTOPPED(status)) {
        fprintf(stderr, "Target exited before PAC capture\n");
        return 1;
    }
    /* Do not swallow a signal-delivery stop or a pre-existing group stop. */
    if ((unsigned)status >> 16 != PTRACE_EVENT_STOP || WSTOPSIG(status) != SIGTRAP) {
        detach_signal = WSTOPSIG(status);
        saved_error = EINTR;
    } else if (ptrace(PTRACE_GETREGSET, pid, (void *)(uintptr_t)NT_ARM_PAC_MASK, &io) == -1) {
        saved_error = errno;
    } else if (io.iov_len != sizeof(mask)) {
        saved_error = EIO;
    }
    if (ptrace(PTRACE_DETACH, pid, NULL, (void *)(intptr_t)detach_signal) == -1) {
        perror("PTRACE_DETACH");
        return 1;
    }
    if (saved_error == EINVAL || saved_error == ENODEV || saved_error == ENOSYS) {
        puts("unsupported");
        return 0;
    }
    if (saved_error) {
        fprintf(stderr, "PAC capture: %s\n", strerror(saved_error));
        return 1;
    }
    printf("0x%llx\n", (unsigned long long)mask.insn_mask);
    return 0;
#endif
}
