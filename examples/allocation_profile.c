#define _GNU_SOURCE

#include <errno.h>
#include <dlfcn.h>
#include <execinfo.h>
#include <fcntl.h>
#include <malloc.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// Linux allocation instrumentation used by the Docker profiling image. It is
// intentionally not linked into production binaries. Allocation calls are
// forwarded with RTLD_NEXT so the service keeps using its linked allocator
// (glibc, jemalloc or another compatible implementation). The fixed binary
// record can be written safely from a signal handler and is converted to JSON
// by the profiling runner.

extern void* __libc_malloc(size_t size);
extern void* __libc_calloc(size_t count, size_t size);
extern void* __libc_realloc(void* pointer, size_t size);
extern void __libc_free(void* pointer);
extern void* __libc_memalign(size_t alignment, size_t size);

static void* (*next_malloc)(size_t);
static void* (*next_calloc)(size_t, size_t);
static void* (*next_realloc)(void*, size_t);
static void (*next_free)(void*);
static void* (*next_memalign)(size_t, size_t);
static void* (*next_aligned_alloc)(size_t, size_t);
static int (*next_posix_memalign)(void**, size_t, size_t);
static size_t (*next_malloc_usable_size)(void*);
static _Thread_local int resolving_symbols;
static _Thread_local int capturing_stack;

static void resolve_symbols(void);

static void* forwarded_malloc(size_t size) {
  resolve_symbols();
  return next_malloc != NULL ? next_malloc(size) : __libc_malloc(size);
}

static void* forwarded_calloc(size_t count, size_t size) {
  resolve_symbols();
  return next_calloc != NULL ? next_calloc(count, size)
                             : __libc_calloc(count, size);
}

static void* forwarded_realloc(void* pointer, size_t size) {
  resolve_symbols();
  return next_realloc != NULL ? next_realloc(pointer, size)
                              : __libc_realloc(pointer, size);
}

static void forwarded_free(void* pointer) {
  resolve_symbols();
  if (next_free != NULL)
    next_free(pointer);
  else
    __libc_free(pointer);
}

static void resolve_symbols(void) {
  if (next_malloc != NULL || resolving_symbols) return;
  resolving_symbols = 1;
  *(void**)(&next_malloc) = dlsym(RTLD_NEXT, "malloc");
  *(void**)(&next_calloc) = dlsym(RTLD_NEXT, "calloc");
  *(void**)(&next_realloc) = dlsym(RTLD_NEXT, "realloc");
  *(void**)(&next_free) = dlsym(RTLD_NEXT, "free");
  *(void**)(&next_memalign) = dlsym(RTLD_NEXT, "memalign");
  *(void**)(&next_aligned_alloc) = dlsym(RTLD_NEXT, "aligned_alloc");
  *(void**)(&next_posix_memalign) = dlsym(RTLD_NEXT, "posix_memalign");
  *(void**)(&next_malloc_usable_size) =
      dlsym(RTLD_NEXT, "malloc_usable_size");
  resolving_symbols = 0;
}

static size_t allocation_size(void* pointer) {
  if (pointer == NULL) return 0;
  resolve_symbols();
  if (next_malloc_usable_size != NULL)
    return next_malloc_usable_size(pointer);
  return malloc_usable_size(pointer);
}

enum { kCounterCount = 12 };

enum Counter {
  kMallocCalls,
  kCallocCalls,
  kReallocCalls,
  kMemalignCalls,
  kFreeCalls,
  kAllocationFailures,
  kMallocBytes,
  kCallocBytes,
  kReallocBytes,
  kMemalignBytes,
  kFreedBytes,
  kPeakLiveBytes,
};

struct Snapshot {
  char magic[8];
  uint32_t version;
  uint32_t counter_count;
  uint64_t pid;
  uint64_t counters[kCounterCount];
};

enum { kStackFrameCount = 48 };

enum AllocationKind {
  kAllocationMalloc,
  kAllocationCalloc,
  kAllocationRealloc,
  kAllocationMemalign,
};

struct StackHeader {
  char magic[8];
  uint32_t version;
  uint32_t frame_count;
  uint64_t pid;
  uint64_t sample_every;
};

struct StackRecord {
  uint64_t generation;
  uint64_t sequence;
  uint64_t usable_size;
  uint32_t kind;
  uint32_t depth;
  uint64_t frames[kStackFrameCount];
};

static _Atomic uint64_t counters[kCounterCount];
static _Atomic uint64_t live_bytes;
static _Atomic uint64_t stack_sequence;
static _Atomic uint64_t stack_generation;
static _Atomic int stack_sampling_enabled;
static int output_fd = -1;
static int stack_output_fd = -1;
static uint64_t stack_sample_every;

static void maybe_sample_allocation(enum AllocationKind kind, void* pointer) {
  if (pointer == NULL || stack_output_fd < 0 || stack_sample_every == 0 ||
      capturing_stack ||
      !atomic_load_explicit(&stack_sampling_enabled, memory_order_relaxed))
    return;
  const uint64_t sequence =
      atomic_fetch_add_explicit(&stack_sequence, 1, memory_order_relaxed) + 1;
  if (sequence % stack_sample_every != 0) return;

  struct StackRecord record;
  memset(&record, 0, sizeof(record));
  record.generation =
      atomic_load_explicit(&stack_generation, memory_order_relaxed);
  record.sequence = sequence;
  record.usable_size = (uint64_t)allocation_size(pointer);
  record.kind = (uint32_t)kind;
  capturing_stack = 1;
  const int depth = backtrace((void**)record.frames, kStackFrameCount);
  capturing_stack = 0;
  record.depth = depth > 0 ? (uint32_t)depth : 0;
  (void)write(stack_output_fd, &record, sizeof(record));
}

static void update_peak(uint64_t live) {
  uint64_t peak = atomic_load_explicit(&counters[kPeakLiveBytes],
                                       memory_order_relaxed);
  while (live > peak &&
         !atomic_compare_exchange_weak_explicit(
             &counters[kPeakLiveBytes], &peak, live, memory_order_relaxed,
             memory_order_relaxed)) {
  }
}

static void record_allocation(enum Counter calls, enum Counter bytes,
                              void* pointer) {
  atomic_fetch_add_explicit(&counters[calls], 1, memory_order_relaxed);
  if (pointer == NULL) {
    atomic_fetch_add_explicit(&counters[kAllocationFailures], 1,
                              memory_order_relaxed);
    return;
  }
  const uint64_t usable = (uint64_t)allocation_size(pointer);
  atomic_fetch_add_explicit(&counters[bytes], usable, memory_order_relaxed);
  const uint64_t live =
      atomic_fetch_add_explicit(&live_bytes, usable, memory_order_relaxed) +
      usable;
  update_peak(live);
}

static void record_free(void* pointer) {
  if (pointer == NULL) return;
  const uint64_t usable = (uint64_t)allocation_size(pointer);
  atomic_fetch_add_explicit(&counters[kFreeCalls], 1, memory_order_relaxed);
  atomic_fetch_add_explicit(&counters[kFreedBytes], usable,
                            memory_order_relaxed);
  uint64_t live = atomic_load_explicit(&live_bytes, memory_order_relaxed);
  while (live != 0) {
    const uint64_t next = usable >= live ? 0 : live - usable;
    if (atomic_compare_exchange_weak_explicit(
            &live_bytes, &live, next, memory_order_relaxed,
            memory_order_relaxed))
      break;
  }
}

void* malloc(size_t size) {
  if (capturing_stack) return forwarded_malloc(size);
  if (resolving_symbols) return __libc_malloc(size);
  void* pointer = forwarded_malloc(size);
  record_allocation(kMallocCalls, kMallocBytes, pointer);
  maybe_sample_allocation(kAllocationMalloc, pointer);
  return pointer;
}

void* calloc(size_t count, size_t size) {
  if (capturing_stack) return forwarded_calloc(count, size);
  if (resolving_symbols) return __libc_calloc(count, size);
  void* pointer = forwarded_calloc(count, size);
  record_allocation(kCallocCalls, kCallocBytes, pointer);
  maybe_sample_allocation(kAllocationCalloc, pointer);
  return pointer;
}

void* realloc(void* old_pointer, size_t size) {
  if (capturing_stack) return forwarded_realloc(old_pointer, size);
  if (resolving_symbols) return __libc_realloc(old_pointer, size);
  if (old_pointer != NULL) record_free(old_pointer);
  void* pointer = forwarded_realloc(old_pointer, size);
  record_allocation(kReallocCalls, kReallocBytes, pointer);
  maybe_sample_allocation(kAllocationRealloc, pointer);
  return pointer;
}

void free(void* pointer) {
  if (capturing_stack) {
    forwarded_free(pointer);
    return;
  }
  if (resolving_symbols) {
    __libc_free(pointer);
    return;
  }
  record_free(pointer);
  forwarded_free(pointer);
}

void* memalign(size_t alignment, size_t size) {
  if (capturing_stack)
    return next_memalign != NULL ? next_memalign(alignment, size)
                                 : __libc_memalign(alignment, size);
  if (resolving_symbols) return __libc_memalign(alignment, size);
  resolve_symbols();
  void* pointer = next_memalign != NULL
                      ? next_memalign(alignment, size)
                      : __libc_memalign(alignment, size);
  record_allocation(kMemalignCalls, kMemalignBytes, pointer);
  maybe_sample_allocation(kAllocationMemalign, pointer);
  return pointer;
}

void* aligned_alloc(size_t alignment, size_t size) {
  if (capturing_stack)
    return next_aligned_alloc != NULL ? next_aligned_alloc(alignment, size)
                                      : __libc_memalign(alignment, size);
  if (resolving_symbols) return __libc_memalign(alignment, size);
  resolve_symbols();
  void* pointer = next_aligned_alloc != NULL
                      ? next_aligned_alloc(alignment, size)
                      : __libc_memalign(alignment, size);
  record_allocation(kMemalignCalls, kMemalignBytes, pointer);
  maybe_sample_allocation(kAllocationMemalign, pointer);
  return pointer;
}

int posix_memalign(void** result, size_t alignment, size_t size) {
  if (capturing_stack) {
    if (next_posix_memalign != NULL)
      return next_posix_memalign(result, alignment, size);
    void* pointer = __libc_memalign(alignment, size);
    if (pointer == NULL) return ENOMEM;
    *result = pointer;
    return 0;
  }
  if (resolving_symbols) {
    void* pointer = __libc_memalign(alignment, size);
    if (pointer == NULL) return ENOMEM;
    *result = pointer;
    return 0;
  }
  resolve_symbols();
  int status;
  if (next_posix_memalign != NULL) {
    status = next_posix_memalign(result, alignment, size);
  } else {
    *result = __libc_memalign(alignment, size);
    status = *result == NULL ? ENOMEM : 0;
  }
  record_allocation(kMemalignCalls, kMemalignBytes,
                    status == 0 ? *result : NULL);
  maybe_sample_allocation(kAllocationMemalign,
                          status == 0 ? *result : NULL);
  return status;
}

static void reset_counters(int signal_number) {
  (void)signal_number;
  for (size_t index = 0; index < kCounterCount; ++index)
    atomic_store_explicit(&counters[index], 0, memory_order_relaxed);
  atomic_store_explicit(&live_bytes, 0, memory_order_relaxed);
  atomic_store_explicit(&stack_sequence, 0, memory_order_relaxed);
  atomic_fetch_add_explicit(&stack_generation, 1, memory_order_relaxed);
  atomic_store_explicit(&stack_sampling_enabled, 1, memory_order_release);
}

static void write_snapshot(int signal_number) {
  (void)signal_number;
  atomic_store_explicit(&stack_sampling_enabled, 0, memory_order_release);
  if (stack_output_fd >= 0) (void)fsync(stack_output_fd);
  if (output_fd < 0) return;
  struct Snapshot snapshot = {
      .magic = {'S', 'L', 'A', 'L', 'L', 'O', 'C', '\0'},
      .version = 1,
      .counter_count = kCounterCount,
      .pid = (uint64_t)getpid(),
  };
  for (size_t index = 0; index < kCounterCount; ++index)
    snapshot.counters[index] =
        atomic_load_explicit(&counters[index], memory_order_relaxed);
  (void)pwrite(output_fd, &snapshot, sizeof(snapshot), 0);
  (void)fsync(output_fd);
}

__attribute__((constructor)) static void initialize_profiler(void) {
  resolve_symbols();
  capturing_stack = 1;
  void* warmup_frames[1];
  (void)backtrace(warmup_frames, 1);
  capturing_stack = 0;
  const char* path = getenv("SERVICELIB_ALLOCATION_PROFILE_PATH");
  if (path != NULL && path[0] != '\0')
    output_fd = open(path, O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC, 0666);
  const char* stack_path = getenv("SERVICELIB_ALLOCATION_STACK_PATH");
  const char* sample_every =
      getenv("SERVICELIB_ALLOCATION_STACK_SAMPLE_EVERY");
  if (stack_path != NULL && stack_path[0] != '\0' && sample_every != NULL) {
    char* end = NULL;
    const unsigned long long parsed = strtoull(sample_every, &end, 10);
    if (end != sample_every && *end == '\0' && parsed > 0) {
      stack_sample_every = (uint64_t)parsed;
      stack_output_fd =
          open(stack_path, O_CREAT | O_TRUNC | O_WRONLY | O_APPEND | O_CLOEXEC,
               0666);
      if (stack_output_fd >= 0) {
        const struct StackHeader header = {
            .magic = {'S', 'L', 'A', 'S', 'T', 'K', '\0', '\0'},
            .version = 1,
            .frame_count = kStackFrameCount,
            .pid = (uint64_t)getpid(),
            .sample_every = stack_sample_every,
        };
        (void)write(stack_output_fd, &header, sizeof(header));
      }
    }
  }
  if (output_fd < 0 && stack_output_fd < 0) return;
  struct sigaction reset_action;
  memset(&reset_action, 0, sizeof(reset_action));
  reset_action.sa_handler = reset_counters;
  sigemptyset(&reset_action.sa_mask);
  (void)sigaction(SIGUSR1, &reset_action, NULL);
  struct sigaction snapshot_action;
  memset(&snapshot_action, 0, sizeof(snapshot_action));
  snapshot_action.sa_handler = write_snapshot;
  sigemptyset(&snapshot_action.sa_mask);
  (void)sigaction(SIGUSR2, &snapshot_action, NULL);
}

__attribute__((destructor)) static void finalize_profiler(void) {
  write_snapshot(0);
  if (output_fd >= 0) close(output_fd);
  if (stack_output_fd >= 0) close(stack_output_fd);
}
