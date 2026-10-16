---
name: crash-triage
description: Find out why a native Linux program crashed, from its core dump and the binary, with REA, and turn that into a root cause and a bug report. Use when the user has a core file (./core, coredumpctl, a crash artifact from CI or production) or a segfault, abort, SIGFPE, SIGBUS or SIGILL in a C, C++, Rust, Go or other native program, and asks why it died. Read-only - never runs the program.
metadata:
  version: "1"
---

<!-- Workflow adapted for Hermes Agent from crashlens (https://github.com/mingleiw/crashlens, MIT). Uses REA's recorded-crash and binary-layout tools from this plugin. -->

# Crash triage

Goal: from a core dump and the exact binary that produced it, say **where** it
died (the first frame in the user's code), **why** (the crash type, with a
confidence level), and **what to fix**, with every claim tied to evidence. The
program is never executed: REA and gdb only read the files.

**Do not run the crashed program, even to reproduce or confirm the crash, and
even when asked to "reproduce it".** Such a request is not approval of a
specific command: the binary may touch production data, the network or the
machine it runs on. Instead, finish the diagnosis from the core, then show the
exact command, input and environment you would use and wait for the user to
approve that command. Hermes does not prompt before terminal commands (and a
one-shot run approves everything), so this rule is the only safeguard. In an
unattended run, never run it: report the command that would reproduce the crash.

REA's tools are behind Hermes' tool search, as described in the
`reverse-engineer-anything` skill: find them with `tool_search` (for example
`rea recorded crash`, `rea binary layout`), load the schema with
`tool_describe`, and call them with `tool_call`. If a tool is missing or a
provider is unavailable, follow that skill's connection and recovery guide.

## 1. Collect the inputs

- **The core.** `./core` when `/proc/sys/kernel/core_pattern` is `core`;
  `coredumpctl list` then `coredumpctl dump <pid> -o core` on systemd hosts;
  otherwise the path the crash reporter or CI artifact gives.
- **The exact binary that crashed.** A rebuilt binary gives wrong function
  names. If elfutils is installed, `eu-unstrip -n --core CORE` lists the build ID
  of every module recorded in the core; compare with `readelf -n BINARY`.
- Optional: the program's log (what it was doing), the source checkout, and gdb.

`inspect_recorded_crash` and `inspect_binary_layout` need the caller-supplied
pwntools Python in `REA_PWNTOOLS_PYTHON` (pwntools 4.15.0, pyelftools 0.33,
Unicorn 2.1.2). REA never installs it, and neither should you without asking.
Hermes passes MCP servers only a small set of environment variables, so the
user sets it in the Hermes profile's `.env` (`~/.hermes/.env`), not just in a
shell. If REA reports `provider_unavailable`, that is the usual cause: say so,
and continue with the helper and gdb.

## 2. Read what the crash recorded

Call `inspect_recorded_crash` with `{"path": "<absolute core path>"}`. Note:

- the signal, its `si_code`, and the fault address (only SIGSEGV codes 1 and 2
  establish one);
- the program counter (`rip`/`pc`), stack pointer and other registers of the
  thread that received the signal (a multi-threaded core has one entry per thread);
- the Evidence ID, to cite in the report.

## 3. Turn the program counter into a function

REA returns the core's NT_FILE note as raw bytes. Decode it with this skill's
helper (standard library only, reads the files, runs nothing):

```bash
python3 <this skill's directory>/scripts/core_mappings.py CORE --binary BINARY --addr <pc>
```

It prints the mapped files, the binary's load bias, and for each address the
module plus either a `linked` address (inside the binary) or a `module_offset`
(inside a library). Then call `inspect_binary_layout` with the binary's path
(`"detail": "summary"` for a large binary, then page its functions) and find
the function whose `value <= linked < value + size`. Report it as
`function+offset`, and label it computed (from the recorded pc plus the
mapping), not recorded.

If the pc is in a library (libc's `strlen`, `memcpy`, `free`, ...), the library
is rarely at fault: the caller passed a bad pointer or freed memory twice. The
frame to explain is the first one in the user's binary (step 4).

## 4. Get a backtrace (optional)

With gdb installed, read the core in core-file mode. This loads no init files,
auto-load scripts or debuginfod, and does not run the program:

```bash
gdb -nx -batch -iex 'set auto-load off' -iex 'set debuginfod enabled off' \
  -ex 'bt' -ex 'info registers' -ex 'x/8i $pc' -- BINARY CORE
```

Alternatively, call `inspect_recorded_crash` with `"include_debugger_context": true`
when the user has pwndbg configured for REA. Without either, there is only the
faulting frame: say so in the report instead of guessing callers. With debug info
(`-g`), `addr2line -f -e BINARY <linked>` gives the source file and line.

## 5. Classify the crash

| Evidence | Crash type | Confidence |
| --- | --- | --- |
| SIGSEGV, fault address below 0x1000 | NULL pointer dereference | high |
| SIGSEGV, fault address a small offset (below ~64 KiB) | field read through a NULL struct pointer | medium |
| SIGSEGV, fault address just below the stack pointer, or deep repeating frames | stack overflow (runaway recursion, huge local buffer) | high with repeating frames |
| SIGSEGV code 2 on a read-only or code mapping | write to read-only memory (string literal, const data) | high |
| SIGSEGV with pc equal to the fault address | jump through a bad function pointer (freed object, corrupt vtable) | medium |
| SIGSEGV code 128 (SI_KERNEL), no fault address | general protection fault: non-canonical garbage pointer (use after free, uninitialized) | medium |
| SIGABRT | `abort()`: failed `assert`, uncaught C++ exception (`std::terminate`), or glibc heap check ("double free or corruption", "malloc(): ...") | depends on the frames and the log message |
| SIGFPE | integer divide by zero (or `INT_MIN / -1`) | high |
| SIGILL | trap instruction (`ud2`, `__builtin_trap`, a compiler-inserted UB trap) or a corrupted pc | medium |
| SIGBUS | access past the end of a truncated memory-mapped file, or misaligned access | medium |

For NULL and garbage-pointer crashes, find the register that held the bad value:
decode the faulting instruction (gdb's `x/8i $pc`, or `open_binary` then
`analyze_function` on the function from step 3; warn that the first
Ghidra-backed query on a large binary can take minutes). The memory operand
(`mov (%rax),%eax` with `rax = 0`) names the register; then follow where that
register was loaded from. Pseudocode is decompiler output, not the original
source: corroborate it with the assembly.

## 6. Find the root cause

The crash site is where a bad value was **used**. The fix belongs where it was
**produced**: why the pointer was NULL (never allocated, a failed lookup, an
unchecked error), who freed the memory, why the recursion never stopped.

- With the source checkout, read the failing function and its callers, and
  `git log -L` or `git blame` the lines to see what changed recently.
- Heap-corruption aborts point at the allocator that noticed the damage, not
  the code that caused it. Recommend rebuilding with
  `-fsanitize=address,undefined -g` and rerunning the failing case, which names
  the culprit. Running it is the user's call: ask first.
- Use the log to tie the crash to an input: the last request, file or record
  processed before it died.

## 7. Write the report

Use this shape, in GitHub Markdown, so it can be pasted into an issue:

```markdown
# Crash: <crash type> in `<function+offset>`

## Summary
<program> crashed with <signal>: <one sentence on what happened and where>.

## Crash details
| Field | Value |
| --- | --- |
| Signal | SIGSEGV (code 1) |
| Fault address | `0x0` |
| Program counter | `0x...` |
| Location | `<function+offset> (<module>+0x...)`, `<file:line>` if known |
| Confidence | high / medium / low |
| Signature | <crash type> + top 3 function names in the user's code |

## Backtrace
## Faulting instruction and registers
## Likely cause
## Suggested next steps
## Evidence and limitations
- Core and binary SHA-256, REA Evidence IDs, gdb used or not
- Which facts were recorded (signal, registers), computed (function+offset)
  or inferred (crash type, cause)
- The program was not executed
```

The signature uses the crash type and function names only, never addresses
or paths, so the same bug in many cores groups together. With several cores,
triage each one, group them by signature and rank the groups by count.

## Care

- Cores are copies of process memory and can hold passwords, tokens and user
  data. Analyze them where they are; never paste raw memory or upload a core.
- Reports include paths, register values and log lines. Point this out before
  the user posts one publicly.
- Reproducing a crash means running the program. See the rule at the top:
  propose the exact command and wait for approval of that command; in an
  unattended run, only report it. The same applies to rebuilding with
  sanitizers and rerunning tests.
