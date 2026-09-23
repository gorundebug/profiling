# ARM64 unwind investigation, 2026-09-23

## Accepted baseline

The profiler captures matching binaries, debug information and the target vDSO.
The ARM64 PAC mask is captured from the kernel, checked against process identity
before/after recording, and bound to the recording hash. Offline decoding must
not invent a mask or reuse one from an unrelated recording.

The controlled PAC fixture recovered all 397 expected leaf-to-main chains.
The full pipeline fixture recovered all 399 expected chains. This establishes
coverage for those fixtures, not completeness for arbitrary native stacks.

The historical Boost framework recording contains 29,128 sample headers.
With matching debug information, vDSO and the independently verified same-boot
PAC mask, the libdw baseline has 824 unresolved physical frames in 478 samples,
with no unresolved sampled leaves. All original sample identities and periods
were retained. Unknown callers still exist; this is NOT complete unwinding.

## Rejected experiments

Stock libunwind 1.8.3 was tested with perf callbacks for symbol lookup and the
verified PAC mask. A separate diagnostic patch also applied the mask to return
addresses recovered by fallback unwinding. Neither experiment is enabled in
the production profiler.

On the identical interval 138384.9 through 138385.1 (287 samples):

| Decoder | Samples containing unresolved frames | Unresolved frames |
| --- | ---: | ---: |
| Accepted libdw/PAC baseline | 3 | 5 |
| libunwind with perf callbacks | 19 | 19 |
| libunwind with additional fallback PAC handling | 13 | 33 |

The alternative recovers deeper chains for sampled PLT/vDSO calls, but regresses
other stacks, notably around allocator calls. It must not replace the baseline
based on a few improved stacks. The completed cache-disabled follow-up produced
identical physical stacks for all 287 samples; disabling caching did not help.

The captured `__kernel_clock_gettime` has no unwind metadata and does not modify
SP/FP/LR in the inspected implementation. The libdw frame-pointer fallback
advances the frame record when returning through LR. This is not a correct
general solution for frameless leaf calls. Conversely, prologue scanning in
another unwinder is not sufficient evidence that all its recovered callers are
correct. Resolving the remaining cases requires validated unwind rules, not
filtering unknown addresses or selecting whichever output looks cleaner.

## Reproducible acceptance checks

Run from the profiling repository:

```sh
python3 -m unittest discover -s examples -p 'test_perf_*.py'
python3 examples/compare_perf_stacks.py baseline.script candidate.script
```

The comparison checks the ordered sample headers, including timestamps and
periods, preserves empty stacks, excludes inline annotations from physical
depth, and reports shortened chains, changed leaves and lost named addresses.
It fails on unsupported input rather than silently ignoring it. Exit status 0
means only that sample identities/weights match, NOT that unwinding is correct.
Address comparisons require the same address representation: an older script
using DSO-relative addresses cannot be directly compared with virtual addresses
from a newer perf version. Sample identities and periods remain comparable.

Do not ship an alternative until known-chain fixtures (PAC, frameless leaf,
PLT, vDSO and signal frames) and real framework/native recordings are checked.
No measured service, runtime library, or recording is changed by this work.
