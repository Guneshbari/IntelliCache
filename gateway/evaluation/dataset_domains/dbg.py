"""
Domain 7: Software Debugging (21 intent groups).
Covers Memory & Resource Leaks, Sanitizers & Concurrency Races,
Profiling & Bottleneck Analysis, Crash Dumps & Syscall Tracing.
"""

from typing import Any, Dict, List

DEBUGGING_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 127. Python Memory Leaks & Cyclic Garbage Collection
    {
        "intent_group_id": "ig_dbg_python_memory_leak_cyclic_gc",
        "domain": "software_debugging",
        "subtopic": "Memory & Resource Leaks",
        "task_kind": "debugging",
        "dimensions": {"language": {"status": "known", "value": "python"}},
        "canonical_query": "Why does a long-running Python daemon process leak memory despite objects leaving local scope?",
        "canonical_response": "Python uses reference counting complemented by a generational cyclic garbage collector. Memory leaks in long-running processes typically occur due to cyclic references involving custom `__del__` methods (in older Python), global or module-level registries/caches retaining references, closures holding onto outer scope frames, or memory fragmentation in pymalloc/glibc malloc where freed memory is not returned to the OS.",
        "positive_paraphrase_1": {
            "query": "What causes continuous memory growth and apparent leaks in long-running Python services?",
            "reason": "Direct paraphrase asking about root causes of memory growth/leaks in persistent Python processes.",
        },
        "positive_paraphrase_2": {
            "query": "In Python, why do objects that are no longer needed fail to release RSS memory back to the operating system?",
            "reason": "Syntactic variation focusing on Python memory retention, object lifetimes, and OS RSS behavior.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you programmatically inspect and break unreachable reference cycles using Python's gc module?",
            "response": "Enable cyclic debugging with `gc.set_debug(gc.DEBUG_UNCOLLECTABLE)`, invoke `gc.collect()`, and inspect `gc.garbage` to identify objects that the cyclic detector cannot free.",
            "reason": "Asks how to programmatically use the gc module to debug cycles rather than explaining root causes of memory leaks.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script using tracemalloc to take two memory snapshots and print the top 10 allocating lines.",
            "response": "```python\nimport tracemalloc\ntracemalloc.start()\n# ... run workload ...\nsnapshot = tracemalloc.take_snapshot()\nfor stat in snapshot.statistics('lineno')[:10]:\n    print(stat)\n```",
            "reason": "Requests specific code implementation using tracemalloc rather than conceptual explanation of Python memory leaks.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "Why does a Go service experience increasing RSS heap usage despite Go runtime garbage collection runs?",
            "response": "Go's runtime retains freed heap pages in its scavenged pool before slowly releasing them to the OS via MADV_DONTNEED, and retained pointers in global slices or sync.Pool instances can keep memory reachable.",
            "reason": "Asks about Go heap and scavenger memory behavior instead of Python memory management.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Python immediate reference counting reclaim isolated acyclic objects immediately without running cyclic GC?",
            "response": "When an object has no cycles, its reference count reaches zero immediately upon exiting scope, causing deallocation and running `tp_dealloc` synchronously without waiting for generational GC sweeps.",
            "reason": "Focuses on immediate acyclic deallocation mechanics rather than persistent daemon memory leaks.",
        },
    },

    # 128. Valgrind Memcheck Uninitialized Memory Reads
    {
        "intent_group_id": "ig_dbg_valgrind_uninitialized_reads",
        "domain": "software_debugging",
        "subtopic": "Memory & Resource Leaks",
        "task_kind": "debugging",
        "dimensions": {"language": {"status": "known", "value": "c"}},
        "canonical_query": "What does Valgrind Memcheck mean by 'Conditional jump or move depends on uninitialised value(s)' in C programs?",
        "canonical_response": "This Valgrind error occurs when branch logic (`if`, `switch`, loop conditions) or conditional instructions depend on a memory location or variable that was allocated (on stack or heap) but never explicitly initialized before being read. Valgrind tracks definedness per bit: using undefined data in calculations is allowed, but making a control-flow decision based on undefined data triggers this warning.",
        "positive_paraphrase_1": {
            "query": "Explain the cause of Valgrind's 'Conditional jump depends on uninitialised value' error report.",
            "reason": "Direct paraphrase explaining the Valgrind uninitialized conditional jump error.",
        },
        "positive_paraphrase_2": {
            "query": "Why does Valgrind flag conditional branching on variables that were never initialized in C?",
            "reason": "Syntactic variation focusing on uninitialized variables triggering branch warnings in Valgrind.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure Valgrind suppression files to ignore false positives in third-party shared libraries?",
            "response": "Run Valgrind with `--gen-suppressions=all`, copy the generated suppression block into a `.supp` file, and pass it on subsequent runs with `--suppressions=my_rules.supp`.",
            "reason": "Asks how to write and use Valgrind suppression files rather than explaining uninitialized read semantics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide a C code snippet that intentionally triggers an uninitialized conditional jump in Valgrind.",
            "response": "```c\n#include <stdio.h>\nint main(void) {\n    int uninit_var;\n    if (uninit_var == 42) printf(\"Match\\n\");\n    return 0;\n}\n```",
            "reason": "Requests C code reproducing the bug instead of conceptual explanation of the Valgrind diagnostic.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does LLVM MemorySanitizer (MSan) detect uninitialized memory reads at compile-time instrumentation?",
            "response": "MSan instruments all memory allocations and loads with shadow memory bitmasks that track whether each byte has been written, checking branch operands at runtime with roughly 2-3x overhead.",
            "reason": "Asks about LLVM MemorySanitizer instrumentation rather than Valgrind dynamic emulation.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What does Valgrind report for 'Invalid read of size N' compared to 'Conditional jump on uninitialised value'?",
            "response": "'Invalid read of size N' indicates an illegal memory access outside allocated bounds (such as a buffer overrun or use-after-free), whereas uninitialized read warnings occur when accessing validly allocated but unwritten memory.",
            "reason": "Compares two distinct Valgrind error types (out-of-bounds invalid read vs uninitialized jump).",
        },
    },

    # 129. File Descriptor Leaks
    {
        "intent_group_id": "ig_dbg_file_descriptor_leak",
        "domain": "software_debugging",
        "subtopic": "Memory & Resource Leaks",
        "task_kind": "debugging",
        "dimensions": {"operating_system": {"status": "known", "value": "linux"}},
        "canonical_query": "How do you identify and diagnose a file descriptor leak causing 'EMFILE: too many open files' on Linux?",
        "canonical_response": "Identify the leaking process using `lsof -p <PID>` or `ls -l /proc/<PID>/fd` to inspect the count and targets of open file descriptors (sockets, pipes, regular files). Compare descriptor counts over time with `ls -l /proc/<PID>/fd | wc -l`. A leak is diagnosed by observing steadily growing descriptor counts pointing to unclosed TCP sockets or opened files where exception handlers or error branches bypass `close()` or context managers.",
        "positive_paraphrase_1": {
            "query": "What steps do you take to track down which resources are leaking file descriptors leading to EMFILE errors on Linux?",
            "reason": "Direct paraphrase asking how to diagnose file descriptor exhaustion and EMFILE errors on Linux.",
        },
        "positive_paraphrase_2": {
            "query": "How can you use /proc/<pid>/fd and lsof to pinpoint a file descriptor leak in a running server process?",
            "reason": "Syntactic variation focusing on using /proc and lsof to locate unclosed descriptors in a server.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you increase the system-wide and process-level file descriptor limits using ulimit and sysctl?",
            "response": "Increase process limits with `ulimit -n <limit>` and `/etc/security/limits.conf`, and system-wide limits via `sysctl -w fs.file-max=<number>` in `/etc/sysctl.conf`.",
            "reason": "Asks how to raise OS file descriptor limits rather than diagnosing and locating an application leak.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a bash shell one-liner that lists the top 5 Linux processes consuming the highest number of open file descriptors.",
            "response": "```bash\nfor pid in /proc/[0-9]*; do echo \"$(ls $pid/fd 2>/dev/null | wc -l) ${pid##*/}\"; done | sort -rn | head -n 5\n```",
            "reason": "Requests a specific bash script to rank processes instead of explaining debugging methodology.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Windows handle handle leaks and how do you monitor them with Sysinternals Handle or Process Explorer?",
            "response": "On Windows, kernel objects (files, events, mutexes) are tracked as handles; use Sysinternals `handle.exe -p <PID>` or Process Explorer's handle view to inspect unclosed HANDLE objects.",
            "reason": "Asks about Windows handle leaks and Sysinternals tooling rather than Linux file descriptor debugging.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between EMFILE (process limit exceeded) and ENFILE (system-wide limit exceeded) on Linux?",
            "response": "EMFILE indicates that the single calling process has reached its per-process `RLIMIT_NOFILE` limit, whereas ENFILE indicates that the entire Linux operating system has exhausted the global `fs.file-max` table.",
            "reason": "Contrasts the error codes EMFILE vs ENFILE rather than explaining how to track down an application descriptor leak.",
        },
    },

    # 130. AddressSanitizer Use-After-Free
    {
        "intent_group_id": "ig_dbg_asan_use_after_free",
        "domain": "software_debugging",
        "subtopic": "Sanitizers & Concurrency Races",
        "task_kind": "debugging",
        "dimensions": {"language": {"status": "known", "value": "cpp"}},
        "canonical_query": "How does LLVM AddressSanitizer (ASan) detect and report use-after-free bugs in C++?",
        "canonical_response": "AddressSanitizer maps 1/8th of the virtual address space to shadow memory and surrounds heap allocations with 'redzones' while poisoning freed memory into a 'quarantine' pool. When memory is freed, ASan poisons its shadow bytes. Any subsequent read or write to that quarantined memory triggers an immediate ASan trap, printing stack traces for the point of invalid access, the earlier deallocation (`free`/`delete`), and the original allocation.",
        "positive_paraphrase_1": {
            "query": "Explain how ASan shadow memory and quarantine pools identify use-after-free crashes in C++.",
            "reason": "Direct paraphrase explaining the ASan shadow memory and quarantine mechanism for use-after-free bugs.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does AddressSanitizer catch access to deallocated pointers and display allocation/deallocation traces?",
            "reason": "Syntactic variation focusing on ASan tracking deallocated memory and printing backtraces.",
        },
        "hard_negative_diff_operation": {
            "query": "What compiler flags and environment variables are needed to enable AddressSanitizer and LeakSanitizer in Clang?",
            "response": "Compile and link with `-fsanitize=address -g -O1`, and customize runtime options with `ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 ./binary`.",
            "reason": "Asks for compiler compilation flags and runtime environment variables rather than explaining detection mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a minimal C++ program containing a dangling pointer that triggers an ASan heap-use-after-free report.",
            "response": "```cpp\nint main() {\n    int* ptr = new int(10);\n    delete ptr;\n    return *ptr; // ASan heap-use-after-free\n}\n```",
            "reason": "Requests C++ source code to trigger the error rather than conceptual explanation of ASan architecture.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Rust's ownership and borrow checker prevent use-after-free vulnerabilities statically at compile-time?",
            "response": "Rust's borrow checker tracks lifetimes and ownership statically: once an owner goes out of scope or is moved, references to it become invalid, causing compilation errors before binary generation.",
            "reason": "Asks about Rust compile-time borrow checking instead of C++ runtime sanitizers.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "How does AddressSanitizer detect stack-buffer-overflow versus heap-use-after-free?",
            "response": "For stack buffer overflows, ASan inserts poisoned redzones directly between local stack frame variables during compilation, whereas use-after-free relies on poisoning heap memory upon deallocation.",
            "reason": "Contrasts stack buffer overflows with heap use-after-free detection in ASan.",
        },
    },

    # 131. Stack Overflow and Unbounded Recursion
    {
        "intent_group_id": "ig_dbg_stack_overflow_unbounded_recursion",
        "domain": "software_debugging",
        "subtopic": "Memory & Resource Leaks",
        "task_kind": "debugging",
        "dimensions": {"concept": {"status": "known", "value": "call_stack"}},
        "canonical_query": "What happens at the operating system and architecture level when a program triggers a stack overflow from unbounded recursion?",
        "canonical_response": "Each function call pushes a stack frame (return address, saved base pointer, local variables) onto the thread's call stack, growing towards lower memory addresses. When unbounded recursion exhausts the thread's allocated stack memory size, the stack pointer hits an unmapped, read/write-protected memory boundary called a 'guard page'. The CPU MMU triggers a page fault that the OS kernel cannot resolve as valid stack growth, causing the OS to send a SIGSEGV signal terminating the process.",
        "positive_paraphrase_1": {
            "query": "Explain what occurs at the OS and CPU memory level when deep or infinite recursion overflows the call stack.",
            "reason": "Direct paraphrase explaining guard pages, stack limits, and SIGSEGV on stack overflow.",
        },
        "positive_paraphrase_2": {
            "query": "How do CPU stack pointers and Linux guard pages interact to produce a segmentation fault during unbounded recursion?",
            "reason": "Syntactic variation focusing on the interaction between the stack pointer, guard page, and SIGSEGV.",
        },
        "hard_negative_diff_operation": {
            "query": "How can you convert recursive algorithms into iterative loops using an explicit heap-allocated stack data structure?",
            "response": "Replace recursive calls with a while loop that pushes and pops state tuples or stack frame structs from a heap-allocated `std::vector` or slice, eliminating call stack limits.",
            "reason": "Asks how to refactor recursion to an iterative heap stack rather than explaining low-level stack overflow mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Show how to increase the default thread stack size in a POSIX pthreads C program using pthread_attr_setstacksize.",
            "response": "```c\npthread_attr_t attr;\npthread_attr_init(&attr);\npthread_attr_setstacksize(&attr, 16 * 1024 * 1024); // 16MB\npthread_create(&thread, &attr, worker_func, NULL);\n```",
            "reason": "Requests specific C pthread API implementation code to set stack size instead of explaining stack overflow causes.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Python handle unbounded recursion with sys.getrecursionlimit() and RecursionError instead of crashing with SIGSEGV?",
            "response": "Python tracks call depth in the interpreter evaluation loop (`PyEval_EvalFrameEx`); when depth exceeds `sys.getrecursionlimit()`, it raises a `RecursionError` exception safely before hitting OS stack guard pages.",
            "reason": "Asks about Python interpreter recursion limit handling rather than OS and CPU stack overflow architecture.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "How does tail-call optimization (TCO) allow certain recursive functions to execute indefinitely without consuming stack space?",
            "response": "In languages supporting TCO, if the recursive call is in the final tail position, the compiler reuses the existing stack frame via a jump instruction instead of pushing a new frame.",
            "reason": "Focuses on compiler tail-call elimination rather than stack overflow failure semantics.",
        },
    },

    # 132. ThreadSanitizer Data Race Detection
    {
        "intent_group_id": "ig_dbg_tsan_data_race_detection",
        "domain": "software_debugging",
        "subtopic": "Sanitizers & Concurrency Races",
        "task_kind": "debugging",
        "dimensions": {"language": {"status": "known", "value": "go"}},
        "canonical_query": "How does ThreadSanitizer (TSan / Go race detector) detect concurrent data races at runtime?",
        "canonical_response": "ThreadSanitizer uses vector clocks, shadow memory, and state machines to track synchronization events. For each 8-byte application memory word, TSan maintains shadow cells recording the thread ID, access epoch, access size, and read/write flag. It intercepts mutex locks/unlocks, atomic operations, channel sends/receives, and goroutine/thread spawns to maintain 'happens-before' relationships. If two threads access the same memory location concurrently where at least one access is a write and no happens-before relation exists, TSan flags a data race.",
        "positive_paraphrase_1": {
            "query": "Explain the vector clock and shadow memory mechanism that Go's race detector (-race) uses to catch data races.",
            "reason": "Direct paraphrase explaining TSan vector clocks, happens-before, and shadow memory tracking.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does ThreadSanitizer verify happens-before order across threads to identify unsynchronized concurrent writes?",
            "reason": "Syntactic variation focusing on happens-before ordering and unsynchronized memory accesses in TSan.",
        },
        "hard_negative_diff_operation": {
            "query": "What runtime CPU and memory overhead does running Go tests with the -race flag introduce?",
            "response": "Go's race detector typically increases memory usage by 5x-10x and slows down execution time by 2x-10x due to shadow memory and state synchronization overhead.",
            "reason": "Asks about performance overhead benchmarks rather than the internal algorithmic detection mechanism.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a minimal Go test function that produces a data race detectable by go test -race.",
            "response": "```go\nfunc TestRace(t *testing.T) {\n    var x int\n    go func() { x = 1 }()\n    x = 2\n}\n```",
            "reason": "Requests code producing a detectable race rather than an explanation of how TSan tracks memory accesses.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Java's Concurrency API and volatile keyword prevent data races according to the Java Memory Model?",
            "response": "In Java, declaring a variable `volatile` guarantees that writes happen-before subsequent reads, establishing visibility and ordering barriers across threads.",
            "reason": "Asks about Java Memory Model volatile semantics rather than TSan shadow memory architecture.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between a data race (unsynchronized memory access) and a race condition (logic-level timing flaw)?",
            "response": "A data race is an undefined, low-level concurrent access where two instructions read/write memory without synchronization. A race condition is a high-level logical flaw where program correctness depends on non-deterministic thread scheduling despite individual memory accesses being atomic.",
            "reason": "Contrasts definitions of data race vs logical race condition rather than explaining TSan detection.",
        },
    },

    # 133. Java Thread Dumps and Deadlock Analysis
    {
        "intent_group_id": "ig_dbg_java_thread_dump_deadlocks",
        "domain": "software_debugging",
        "subtopic": "Sanitizers & Concurrency Races",
        "task_kind": "debugging",
        "dimensions": {"language": {"status": "known", "value": "java"}},
        "canonical_query": "How do you capture and analyze a Java thread dump to find a deadlock between threads?",
        "canonical_response": "Capture a thread dump using `jcmd <PID> Thread.print` or `jstack <PID>`. In the output, locate threads in the `BLOCKED` state waiting for monitors (`waiting to lock <0x...>`). Look for the JVM's automated deadlock detection section at the bottom ('Found one Java-level deadlock:'), which shows the cyclic dependency chain where Thread A holds Lock 1 and waits for Lock 2, while Thread B holds Lock 2 and waits for Lock 1.",
        "positive_paraphrase_1": {
            "query": "What is the procedure for using jstack or jcmd to inspect JVM thread dumps for circular lock deadlocks?",
            "reason": "Direct paraphrase asking how to capture and inspect JVM thread dumps for deadlocks.",
        },
        "positive_paraphrase_2": {
            "query": "How can you interpret BLOCKED thread states and object monitor IDs in a Java thread dump to resolve deadlocks?",
            "reason": "Syntactic variation focusing on interpreting BLOCKED states and monitor addresses in thread dumps.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you prevent deadlocks programmatically in Java using Lock.tryLock with timeouts from java.util.concurrent?",
            "response": "Use `ReentrantLock.tryLock(timeout, unit)`: if the lock cannot be acquired within the timeout window, release any previously held locks and back off before retrying.",
            "reason": "Asks how to avoid deadlocks programmatically via tryLock timeouts rather than how to capture/analyze thread dumps.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Java program that creates two threads acquiring two synchronized locks in opposite order to produce a deadlock.",
            "response": "```java\nObject lock1 = new Object(), lock2 = new Object();\nnew Thread(() -> { synchronized(lock1) { synchronized(lock2) {} } }).start();\nnew Thread(() -> { synchronized(lock2) { synchronized(lock1) {} } }).start();\n```",
            "reason": "Requests Java code reproducing a deadlock rather than explaining thread dump inspection tools.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do you inspect deadlocks in Go goroutines using SIGQUIT stack traces and runtime/pprof?",
            "response": "Send `kill -QUIT <PID>` to dump all goroutine stack traces to stderr, and check if goroutines are stuck in `chan send`, `chan receive`, or `sync.Mutex.Lock` states.",
            "reason": "Asks about Go goroutine deadlock dumps rather than Java thread dump analysis.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference in thread dumps between threads in BLOCKED state versus WAITING state?",
            "response": "A thread is `BLOCKED` when waiting to enter or re-enter a synchronized block blocked by an intrinsic monitor held by another thread. It is `WAITING` when sleeping or waiting indefinitely for a notification (`Object.wait()`, `LockSupport.park()`).",
            "reason": "Contrasts JVM thread states BLOCKED vs WAITING rather than explaining deadlock diagnosis procedures.",
        },
    },

    # 134. Livelock vs Deadlock
    {
        "intent_group_id": "ig_dbg_livelock_vs_deadlock",
        "domain": "software_debugging",
        "subtopic": "Sanitizers & Concurrency Races",
        "task_kind": "explanation",
        "dimensions": {"concept": {"status": "known", "value": "concurrency"}},
        "canonical_query": "What is the technical difference between a deadlock and a livelock in concurrent software?",
        "canonical_response": "In a deadlock, two or more processes or threads are permanently blocked in a waiting state, consuming zero CPU cycles, each waiting for a resource held by another. In a livelock, the processes or threads actively change their state and execute instructions (often consuming 100% CPU), but make zero forward progress because each process responds to the actions of the other by continuously giving way, rolling back, or yielding resources in lockstep.",
        "positive_paraphrase_1": {
            "query": "Compare deadlock and livelock: how do thread states and CPU consumption differ between them?",
            "reason": "Direct paraphrase comparing deadlock and livelock in terms of thread state and CPU usage.",
        },
        "positive_paraphrase_2": {
            "query": "Why does a livelocked process consume CPU without making progress whereas a deadlocked process remains dormant?",
            "reason": "Syntactic variation focusing on CPU consumption differences between livelock and deadlock.",
        },
        "hard_negative_diff_operation": {
            "query": "How does adding randomized exponential backoff resolve livelock in distributed lock retry loops?",
            "response": "Randomized exponential backoff introduces asynchronous jitter into retry intervals, breaking the lockstep synchronization where competing nodes continuously yield at the exact same time.",
            "reason": "Asks how to mitigate livelock using jitter/backoff rather than comparing theoretical definitions.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python simulation of two workers stuck in a livelock loop trying to share a resource.",
            "response": "```python\nwhile True:\n    if other.needs_resource:\n        resource.release()\n        time.sleep(0.01)\n    else:\n        resource.acquire()\n```",
            "reason": "Requests Python code simulating a livelock rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Dining Philosophers problem illustrate deadlock prevention through resource hierarchy ordering?",
            "response": "Assigning a total global order to resources (e.g. acquire lower-indexed fork first) eliminates circular wait, proving Dijkstra's condition for deadlock prevention.",
            "reason": "Asks about Dijkstra's resource hierarchy in Dining Philosophers rather than comparing livelock with deadlock.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is thread starvation, and how does it differ from both deadlock and livelock?",
            "response": "Starvation occurs when a thread is perpetually denied CPU time or resources because greedy higher-priority threads monopolize them, whereas other threads in the system continue making progress.",
            "reason": "Introduces thread starvation and compares it with livelock/deadlock.",
        },
    },

    # 135. TOCTOU Race Conditions
    {
        "intent_group_id": "ig_dbg_toctou_race_conditions",
        "domain": "software_debugging",
        "subtopic": "Sanitizers & Concurrency Races",
        "task_kind": "debugging",
        "dimensions": {"security": {"status": "known", "value": "filesystem"}},
        "canonical_query": "What is a Time-of-Check to Time-of-Use (TOCTOU) race condition in file system operations and how is it mitigated?",
        "canonical_response": "A TOCTOU vulnerability occurs when a program checks the state or permissions of a resource (e.g. checking if a file exists or is not a symlink with `access()` or `stat()`) and then performs an operation on that resource (e.g. `open()` or `write()`) at a later time. An attacker can replace the file with a symbolic link in the time window between the check and use. It is mitigated by performing atomic operations using file descriptors, such as `open()` with `O_CREAT | O_EXCL` or `O_NOFOLLOW`, and operating on the descriptor with `fstat()` and `flock()`.",
        "positive_paraphrase_1": {
            "query": "Explain how TOCTOU vulnerabilities occur during file access checks and how atomic open flags prevent them.",
            "reason": "Direct paraphrase explaining TOCTOU file race conditions and atomic open mitigation.",
        },
        "positive_paraphrase_2": {
            "query": "Why is checking file existence before opening it a race condition, and how do file descriptors fix this?",
            "reason": "Syntactic variation focusing on the gap between check and open, and using file descriptors.",
        },
        "hard_negative_diff_operation": {
            "query": "How do Linux secure temporary directories created with mkstemp() prevent symlink replacement attacks?",
            "response": "`mkstemp()` generates a unique name and opens the file atomically with `O_RDWR | O_CREAT | O_EXCL` with mode 0600, ensuring no attacker could have pre-created the file or symlinked it.",
            "reason": "Asks specifically about mkstemp() implementation rather than general TOCTOU principles and mitigations.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a C function that securely creates a new file without TOCTOU race conditions using open flags.",
            "response": "```c\nint fd = open(\"/tmp/secure.dat\", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);\nif (fd < 0) { perror(\"open\"); return -1; }\n```",
            "reason": "Requests C source code implementation rather than explaining TOCTOU mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do database isolation levels and SELECT FOR UPDATE prevent TOCTOU race conditions in SQL transactions?",
            "response": "In SQL, checking a balance and subsequently updating it causes a race unless done inside a transaction with `SELECT ... FOR UPDATE` or Serializable isolation to lock the rows during check and use.",
            "reason": "Asks about database concurrency and SELECT FOR UPDATE rather than filesystem TOCTOU.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does openat() with directory file descriptors mitigate symlink path traversal races compared to path-based open()?",
            "response": "`openat()` resolves relative paths starting from an already-opened directory descriptor (`O_DIRECTORY`), avoiding path components that an attacker could swap in parent directories during traversal.",
            "reason": "Focuses on openat() directory descriptors against traversal rather than general TOCTOU file checks.",
        },
    },

    # 136. Node.js Event Loop Starvation
    {
        "intent_group_id": "ig_dbg_nodejs_event_loop_starvation",
        "domain": "software_debugging",
        "subtopic": "Profiling & Bottleneck Analysis",
        "task_kind": "debugging",
        "dimensions": {"runtime": {"status": "known", "value": "nodejs"}},
        "canonical_query": "Why does a synchronous CPU-bound operation cause event loop starvation in Node.js, and how do you diagnose it?",
        "canonical_response": "Node.js executes JavaScript on a single thread. When synchronous CPU-intensive code (such as large JSON parsing, complex regex matching, synchronous crypto, or tight numerical loops) runs, it holds the call stack, preventing the libuv event loop from advancing to process I/O events, timers, or network sockets. It is diagnosed by monitoring event loop lag (e.g. using `perf_hooks` `monitorEventLoopDelay()`), observing skyrocketing response latencies, and inspecting CPU profiles to identify the blocking synchronous function.",
        "positive_paraphrase_1": {
            "query": "How does blocking synchronous computation starve the Node.js event loop and how can you measure the delay?",
            "reason": "Direct paraphrase explaining event loop starvation from synchronous operations and measuring lag.",
        },
        "positive_paraphrase_2": {
            "query": "What causes severe event loop delay in Node.js server applications and how do you profile blocking calls?",
            "reason": "Syntactic variation focusing on event loop delay causes and profiling blocked synchronous execution.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you offload CPU-heavy computation in Node.js using Worker Threads or child_process pools?",
            "response": "Instantiate `new Worker(path, { workerData })` to run heavy computation in a separate OS thread with its own V8 instance, communicating via `parentPort.postMessage()`.",
            "reason": "Asks how to implement Worker Threads to offload computation rather than explaining starvation causes and diagnostics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write Node.js code using perf_hooks to monitor and log event loop delay percentiles (p50, p99).",
            "response": "```javascript\nconst { monitorEventLoopDelay } = require('perf_hooks');\nconst h = monitorEventLoopDelay({ resolution: 20 });\nh.enable();\nsetInterval(() => console.log('p99:', h.percentile(99) / 1e6, 'ms'), 5000);\n```",
            "reason": "Requests specific JavaScript code using perf_hooks rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "Why does Python asyncio event loop block when running synchronous requests.get() inside async def functions?",
            "response": "Python's asyncio loop runs on a single thread; blocking network I/O or CPU work in an async function prevents `asyncio.run` from yielding to other coroutines until the call completes.",
            "reason": "Asks about Python asyncio event loop blocking rather than Node.js libuv event loop starvation.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between libuv thread pool exhaustion (UV_THREADPOOL_SIZE) and main thread event loop starvation?",
            "response": "Main thread starvation blocks all JS execution and timer callbacks. Thread pool exhaustion only delays operations queued to libuv's background thread pool (fs, crypto.pbkdf2, dns.lookup), while the JS main thread remains responsive.",
            "reason": "Contrasts libuv background worker pool exhaustion with main thread event loop starvation.",
        },
    },

    # 137. CPU Flamegraphs
    {
        "intent_group_id": "ig_dbg_cpu_flamegraphs",
        "domain": "software_debugging",
        "subtopic": "Profiling & Bottleneck Analysis",
        "task_kind": "debugging",
        "dimensions": {"tool": {"status": "known", "value": "flamegraph"}},
        "canonical_query": "How do you read and interpret a CPU flamegraph to identify performance bottlenecks in an application?",
        "canonical_response": "On a flamegraph, the Y-axis represents the call stack depth (with parent frames below child frames), and the X-axis spans the entire sample population sorted alphabetically (not chronologically). The width of each rectangular box is proportional to the percentage of time that function was present on the CPU during sampling. Wide plateaus at the top of towers represent functions consuming CPU directly (on-CPU leaf bottlenecks), while wide towers represent functions responsible for large cumulative execution time across their callees.",
        "positive_paraphrase_1": {
            "query": "Explain how the X and Y axes and box widths on a CPU flamegraph reveal CPU hotspots.",
            "reason": "Direct paraphrase explaining how to interpret flamegraph dimensions and identify hotspots.",
        },
        "positive_paraphrase_2": {
            "query": "What do wide boxes and stack tower heights indicate when analyzing CPU profiling flamegraphs?",
            "reason": "Syntactic variation focusing on wide boxes and stack heights on profiling flamegraphs.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you generate a flamegraph from Linux perf records using Brendan Gregg's FlameGraph perl scripts?",
            "response": "Capture samples with `perf record -F 99 -g -p <PID> -- sleep 30`, collapse stacks with `perf script | ./stackcollapse-perf.pl`, and render with `./flamegraph.pl out.collapsed > flamegraph.svg`.",
            "reason": "Asks for shell commands to generate flamegraphs with perf and Perl scripts rather than how to read them.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "How do off-CPU flamegraphs differ from on-CPU flamegraphs in analyzing latency bottlenecks?",
            "response": "On-CPU flamegraphs sample active CPU execution, whereas off-CPU flamegraphs trace blocked states (I/O, locks, sleep) using kernel scheduler tracepoints (`sched:sched_switch`).",
            "reason": "Contrasts on-CPU with off-CPU flamegraph methodologies rather than interpreting standard CPU flamegraphs.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do you capture and inspect flamegraphs in Go using go tool pprof web UI?",
            "response": "Capture a profile with `go tool pprof http://localhost:6060/debug/pprof/profile?seconds=30` and open the interactive flamegraph view with `go tool pprof -http=:8080 profile.pb.gz`.",
            "reason": "Asks about Go's specific built-in pprof web UI rather than general flamegraph interpretation.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does inlining by optimizing compilers distort stack depths on CPU flamegraphs, and how is it resolved?",
            "response": "Compiler inlining collapses function frames into the caller; compiling with `-g -fno-omit-frame-pointer` and using dwarf unwinding helps reconstruct the full inlined stack frames.",
            "reason": "Focuses on compiler inlining effects on stack unwinding rather than general interpretation of flamegraph geometry.",
        },
    },

    # 138. Sampling vs Tracing Profilers
    {
        "intent_group_id": "ig_dbg_sampling_vs_tracing_profilers",
        "domain": "software_debugging",
        "subtopic": "Profiling & Bottleneck Analysis",
        "task_kind": "explanation",
        "dimensions": {"concept": {"status": "known", "value": "profiling"}},
        "canonical_query": "What is the technical difference between sampling (statistical) profilers and instrumentation (tracing) profilers?",
        "canonical_response": "Sampling profilers periodically interrupt execution at a configured frequency (e.g. 99 Hz via timer interrupts or PMU hardware counters) to inspect the current call stack, introducing low overhead (typically 1-3%) making them suitable for production. Instrumentation (tracing) profilers inject hooks into every function entry and exit to record exact invocation counts and timestamps; this yields exact execution graphs but incurs massive runtime overhead (10x-50x) and distorts timing of short functions (observer effect).",
        "positive_paraphrase_1": {
            "query": "Compare statistical sampling profilers and deterministic tracing profilers in terms of accuracy and overhead.",
            "reason": "Direct paraphrase comparing sampling and tracing profilers regarding overhead and measurement precision.",
        },
        "positive_paraphrase_2": {
            "query": "Why are sampling profilers preferred in production environments compared to code-instrumenting tracing profilers?",
            "reason": "Syntactic variation focusing on overhead differences making sampling profilers viable in production.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure Linux perf with hardware performance counters (PMU) to sample cache misses?",
            "response": "Run `perf record -e cache-misses -c 1000 -p <PID>` to trigger a sample interrupt every 1,000 Level 3 cache misses counted by the CPU PMU.",
            "reason": "Asks for specific perf hardware PMU counter commands rather than comparing sampling vs tracing concepts.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python decorator that implements tracing by measuring and printing the execution duration of every call.",
            "response": "```python\nimport time\ndef trace(func):\n    def wrapper(*args, **kwargs):\n        t0 = time.perf_counter()\n        res = func(*args, **kwargs)\n        print(func.__name__, 'took', time.perf_counter() - t0)\n        return res\n    return wrapper\n```",
            "reason": "Requests Python decorator code implementing tracing rather than comparing profiler architectures.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does eBPF-based profiling with bpftrace sample kernel and userspace stacks without modifying binaries?",
            "response": "bpftrace attaches eBPF probes to the `profile:hz:99` kernel timer to sample the current user/kernel instruction pointer and stack trace directly in kernel ring buffers.",
            "reason": "Asks specifically about eBPF and bpftrace profiling rather than sampling vs tracing profiler comparison.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does sampling bias occur when the sampling frequency synchronizes with a periodic loop in the target program?",
            "response": "If the sampling timer frequency matches the loop period, samples consistently hit the same instruction while completely missing other phases, which is why profilers use odd frequencies like 99 Hz or 199 Hz.",
            "reason": "Focuses on sampling frequency aliasing and harmonic bias rather than general comparison with tracing.",
        },
    },

    # 139. PostgreSQL Query Profiling (EXPLAIN ANALYZE)
    {
        "intent_group_id": "ig_dbg_postgres_explain_analyze",
        "domain": "software_debugging",
        "subtopic": "Profiling & Bottleneck Analysis",
        "task_kind": "debugging",
        "dimensions": {"database": {"status": "known", "value": "postgres"}},
        "canonical_query": "How do you use EXPLAIN (ANALYZE, BUFFERS) in PostgreSQL to diagnose slow queries and disk I/O bottlenecks?",
        "canonical_response": "Running `EXPLAIN (ANALYZE, BUFFERS)` actually executes the query and prints the query execution plan with real timing and buffer statistics. Look for discrepancies between estimated `rows=X` and `actual rows=Y` (indicating stale planner statistics requiring `ANALYZE`). Inspect `Buffers: shared hit` vs `read` vs `dirtied`: high `shared read` indicates queries reading from disk rather than the PostgreSQL buffer cache, while high actual time in Seq Scan or Nested Loop nodes points to missing indexes.",
        "positive_paraphrase_1": {
            "query": "What key metrics should you examine in PostgreSQL EXPLAIN (ANALYZE, BUFFERS) output to locate query bottlenecks?",
            "reason": "Direct paraphrase explaining how to interpret PostgreSQL EXPLAIN ANALYZE and buffer statistics.",
        },
        "positive_paraphrase_2": {
            "query": "How do you detect disk I/O reads and planner row estimation errors using EXPLAIN BUFFERS in Postgres?",
            "reason": "Syntactic variation focusing on buffer reads and planner estimation errors in EXPLAIN output.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you enable pg_stat_statements in postgresql.conf to track slowest queries across the entire database?",
            "response": "Add `shared_preload_libraries = 'pg_stat_statements'` to `postgresql.conf`, restart Postgres, and create the extension with `CREATE EXTENSION pg_stat_statements;`.",
            "reason": "Asks how to install and configure the pg_stat_statements extension rather than analyzing a query plan.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide SQL to query pg_stat_activity to find currently running queries executing longer than 5 seconds.",
            "response": "```sql\nSELECT pid, now() - query_start AS duration, query FROM pg_stat_activity WHERE state != 'idle' AND (now() - query_start) > interval '5 seconds';\n```",
            "reason": "Requests SQL for inspecting pg_stat_activity rather than explaining EXPLAIN ANALYZE execution plan output.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do you use MySQL EXPLAIN FORMAT=JSON to analyze index lookups and temporary table creation?",
            "response": "In MySQL, `EXPLAIN FORMAT=JSON SELECT ...` outputs cost estimates, `using_temporary_table`, and `using_filesort` attributes to identify unindexed joins.",
            "reason": "Asks about MySQL query plan output rather than PostgreSQL EXPLAIN BUFFERS.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between EXPLAIN without ANALYZE versus EXPLAIN ANALYZE in PostgreSQL?",
            "response": "Plain `EXPLAIN` only simulates planning based on catalog statistics without running the query or modifying data, whereas `EXPLAIN ANALYZE` executes the query, recording actual timings and executing mutations.",
            "reason": "Contrasts plain EXPLAIN with EXPLAIN ANALYZE rather than diagnosing query bottlenecks from BUFFERS output.",
        },
    },

    # 140. Go pprof Heap Profiling
    {
        "intent_group_id": "ig_dbg_go_pprof_heap_profiling",
        "domain": "software_debugging",
        "subtopic": "Profiling & Bottleneck Analysis",
        "task_kind": "debugging",
        "dimensions": {"language": {"status": "known", "value": "go"}},
        "canonical_query": "How do you use Go pprof heap profiling to distinguish between inuse_space and alloc_space when debugging memory growth?",
        "canonical_response": "Go pprof heap profiles track memory allocations through sampling (by default 1 sample per 512 KB allocated). `inuse_space` (and `inuse_objects`) measures the memory currently held and reachable on the heap at the moment the profile was captured, which is the primary metric for debugging active memory leaks. `alloc_space` (and `alloc_objects`) measures the cumulative memory allocated since the process started, including objects already garbage-collected, which is used to diagnose high GC pressure and allocator churn.",
        "positive_paraphrase_1": {
            "query": "Explain the difference between -inuse_space and -alloc_space in Go pprof heap profiles for finding memory leaks.",
            "reason": "Direct paraphrase explaining inuse_space vs alloc_space in Go pprof for leak diagnosis.",
        },
        "positive_paraphrase_2": {
            "query": "When profiling Go memory with pprof, why should you use inuse_space for leaks and alloc_space for GC pressure?",
            "reason": "Syntactic variation focusing on using inuse_space for active leaks and alloc_space for GC churn.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you register net/http/pprof endpoints in a custom Go HTTP server?",
            "response": "Import `_ \"net/http/pprof\"` and mount its handlers or start a dedicated diagnostic server on `localhost:6060` with `http.ListenAndServe(\":6060\", nil)`.",
            "reason": "Asks how to expose the pprof HTTP endpoints rather than explaining heap profile metrics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the command to take a 30-second Go heap profile diff between two running instances.",
            "response": "```bash\ngo tool pprof -base http://localhost:6060/debug/pprof/heap?seconds=0 http://localhost:6060/debug/pprof/heap?seconds=30\n```",
            "reason": "Requests specific bash command to take a diff rather than explaining inuse_space vs alloc_space.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do you capture and inspect Java heap dumps using jcmd and Eclipse Memory Analyzer (MAT)?",
            "response": "Trigger a dump with `jcmd <PID> GC.heap_dump /path/dump.hprof`, open it in Eclipse MAT, and calculate the Dominator Tree to locate leak suspects.",
            "reason": "Asks about Java heap dumps and Eclipse MAT rather than Go pprof heap profiles.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between Go heap profile and goroutine profile in pprof?",
            "response": "Heap profiles sample memory allocations and retained bytes, whereas goroutine profiles dump active goroutine counts and their current blocking call stacks.",
            "reason": "Contrasts heap profiles with goroutine profiles rather than analyzing inuse_space vs alloc_space.",
        },
    },

    # 141. P99 Tail Latency Spikes and GC Pauses
    {
        "intent_group_id": "ig_dbg_tail_latency_gc_pauses",
        "domain": "software_debugging",
        "subtopic": "Profiling & Bottleneck Analysis",
        "task_kind": "debugging",
        "dimensions": {"concept": {"status": "known", "value": "latency"}},
        "canonical_query": "Why do garbage collection stop-the-world pauses disproportionately impact P99 tail latency in web services?",
        "canonical_response": "Stop-the-world (STW) GC pauses freeze all application worker threads simultaneously. If a service handles 1,000 requests per second and experiences a 50 ms STW pause, every request arriving or in-flight during that 50 ms window is delayed by up to 50 ms. While median (P50) latency remains unaffected because the vast majority of requests occur between pauses, the affected requests accumulate in the 99th percentile (P99) tail, creating sharp latency spikes.",
        "positive_paraphrase_1": {
            "query": "How do stop-the-world garbage collection pauses create sharp P99 latency spikes while P50 remains healthy?",
            "reason": "Direct paraphrase explaining the disproportionate impact of STW pauses on P99 tail latency.",
        },
        "positive_paraphrase_2": {
            "query": "Why does a 50ms GC pause cause severe 99th percentile tail latency degradation in high-throughput services?",
            "reason": "Syntactic variation focusing on how pause durations amplify into P99 percentile spikes.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you tune JVM ZGC or Shenandoah to achieve sub-millisecond maximum GC pause times?",
            "response": "Enable generational ZGC with `-XX:+UseZGC -XX:+ZGenerational`, which performs concurrent marking and relocation without pausing application threads for more than 1 ms.",
            "reason": "Asks how to tune low-latency collectors (ZGC/Shenandoah) rather than explaining the mechanics of P99 tail amplification.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script that calculates P50, P90, P99, and P99.9 latency percentiles from a list of response times.",
            "response": "```python\nimport numpy as np\ndef calc_percentiles(latencies):\n    return {p: np.percentile(latencies, p) for p in [50, 90, 99, 99.9]}\n```",
            "reason": "Requests Python code calculating percentiles rather than explaining GC pause impact on tail latency.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Linux kernel CPU throttling in Kubernetes cgroups cause P99 tail latency spikes?",
            "response": "When a container consumes its CFS quota within a 100 ms period, the kernel CFS scheduler completely throttles the container's CPU threads until the period resets, stalling in-flight requests.",
            "reason": "Asks about CFS cgroup throttling causing tail latency spikes rather than garbage collection STW pauses.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does tail latency amplification worsen exponentially in microservice architectures with fan-out queries?",
            "response": "If a frontend service fans out to 100 backend services where each has 1% chance of a slow response, the probability that the user request experiences a delay is $1 - (0.99)^{100} \\approx 63.4\\%$.",
            "reason": "Explains microservice fan-out probability math rather than GC pause mechanics.",
        },
    },

    # 142. GDB Core Dump Inspection
    {
        "intent_group_id": "ig_dbg_gdb_core_dump_inspection",
        "domain": "software_debugging",
        "subtopic": "Crash Dumps & Syscall Tracing",
        "task_kind": "debugging",
        "dimensions": {"tool": {"status": "known", "value": "gdb"}},
        "canonical_query": "How do you inspect a Linux core dump using GDB to find the crashing instruction and call stack?",
        "canonical_response": "Launch GDB with the crashing binary and core dump file: `gdb <executable> <core_file>`. GDB immediately outputs the fatal signal (e.g. SIGSEGV) and the offending instruction. Run `bt` (or `bt full`) to inspect the call stack and local variable values. Use `thread apply all bt` to inspect all threads, and switch to the crashing frame with `frame <N>` to inspect register contents (`info registers`) and evaluate variable states (`print <var>`).",
        "positive_paraphrase_1": {
            "query": "What are the essential GDB commands to analyze a core dump and print backtraces of the crashing thread?",
            "reason": "Direct paraphrase explaining GDB commands for inspecting core dumps and backtraces.",
        },
        "positive_paraphrase_2": {
            "query": "How do you load a core file into GDB and navigate stack frames to determine why a process terminated?",
            "reason": "Syntactic variation focusing on loading core files and navigating stack frames in GDB.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure Linux coredumpctl and sysctl kernel.core_pattern to automatically capture core dumps?",
            "response": "Set `sysctl -w kernel.core_pattern=|/lib/systemd/systemd-coredump %P %u %g %s %t %c %h` and use `coredumpctl list` and `coredumpctl gdb` to view dumps.",
            "reason": "Asks how to configure kernel core dump generation rather than how to inspect a dump inside GDB.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide a GDB batch command one-liner to extract a backtrace from a core dump without entering interactive mode.",
            "response": "```bash\ngdb -batch -ex \"thread apply all bt\" -ex \"quit\" /path/to/binary /path/to/core\n```",
            "reason": "Requests a specific shell one-liner instead of explaining the diagnostic procedure in GDB.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do you inspect Windows minidump (.dmp) files using WinDbg and the !analyze -v command?",
            "response": "Open the dump in WinDbg, configure symbol paths with `.symfix`, and run `!analyze -v` to perform automated crash analysis.",
            "reason": "Asks about Windows minidumps and WinDbg rather than Linux GDB core dump inspection.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why do stripped binaries show '??' in GDB backtraces, and how do separate debug symbols (.debug) resolve this?",
            "response": "Stripping removes the ELF `.symtab` and DWARF debug sections; pointing GDB to unstripped binaries or debug symbol files using `set debug-file-directory` restores function names and line numbers.",
            "reason": "Focuses on DWARF debug symbols and stripped binaries rather than core dump inspection workflow.",
        },
    },

    # 143. SIGSEGV Null Pointer Dereference
    {
        "intent_group_id": "ig_dbg_sigsegv_null_pointer_dereference",
        "domain": "software_debugging",
        "subtopic": "Crash Dumps & Syscall Tracing",
        "task_kind": "debugging",
        "dimensions": {"language": {"status": "known", "value": "c"}},
        "canonical_query": "What happens in the CPU and operating system when a C program dereferences a NULL pointer, causing SIGSEGV?",
        "canonical_response": "In modern operating systems, virtual address `0x0` (and typically the entire first memory page, e.g. `0x0000` to `0x0FFF`) is deliberately left unmapped by the kernel. When the CPU executes an instruction dereferencing a NULL pointer (or a struct field at a small offset like `0x8`), the Memory Management Unit (MMU) fails to find a valid Translation Lookaside Buffer (TLB) or page table entry. The CPU raises a page fault exception. The OS kernel traps this fault, identifies that address 0 is invalid, and sends a `SIGSEGV` (signal 11) to terminate the process.",
        "positive_paraphrase_1": {
            "query": "Explain how the CPU MMU and OS page tables turn a NULL pointer dereference into a SIGSEGV signal.",
            "reason": "Direct paraphrase explaining unmapped virtual page 0, MMU page faults, and SIGSEGV signals.",
        },
        "positive_paraphrase_2": {
            "query": "Why does accessing memory address 0 trigger a hardware page fault and operating system segmentation violation?",
            "reason": "Syntactic variation focusing on address 0 triggering hardware page faults and SIGSEGV.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you register a custom POSIX signal handler using sigaction() to catch SIGSEGV and print diagnostics?",
            "response": "Call `sigaction(SIGSEGV, &sa, NULL)` with `sa_flags = SA_SIGINFO` and an alternate signal stack configured with `sigaltstack()` to handle stack overflow crashes safely.",
            "reason": "Asks how to write a signal handler with sigaction rather than explaining low-level CPU/MMU fault semantics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a minimal C snippet that demonstrates dereferencing a NULL pointer struct field.",
            "response": "```c\nstruct User { int id; };\nint main() {\n    struct User* u = NULL;\n    return u->id; // dereference NULL + offset 0\n}\n```",
            "reason": "Requests C code reproducing a null pointer dereference rather than explaining the architectural mechanisms.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Java convert native hardware SIGSEGV faults into java.lang.NullPointerException without crashing the JVM?",
            "response": "The JVM installs a signal handler for SIGSEGV; when a null dereference occurs, the JVM signal handler catches the fault, inspects the PC, and jumps into the JVM exception handling routine throwing a NullPointerException.",
            "reason": "Asks about JVM internal signal handling and NullPointerException generation rather than native C SIGSEGV.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between SIGSEGV (invalid memory access) and SIGBUS (bus error / unaligned access)?",
            "response": "SIGSEGV indicates an access to an unmapped virtual address or permission violation (e.g. writing to read-only code). SIGBUS indicates a hardware alignment violation or accessing a valid mapping backed by an unavailable physical file (e.g. truncated mmap).",
            "reason": "Contrasts SIGSEGV with SIGBUS rather than explaining NULL pointer MMU faults.",
        },
    },

    # 144. Node.js Unhandled Promise Rejections
    {
        "intent_group_id": "ig_dbg_nodejs_unhandled_promise_rejections",
        "domain": "software_debugging",
        "subtopic": "Crash Dumps & Syscall Tracing",
        "task_kind": "debugging",
        "dimensions": {"runtime": {"status": "known", "value": "nodejs"}},
        "canonical_query": "Why do unhandled promise rejections terminate Node.js processes in modern versions, and how should they be handled?",
        "canonical_response": "Starting in Node.js v15, unhandled promise rejections trigger the default `--unhandled-rejections=throw` mode, emitting an uncaught exception that terminates the process with exit code 1. This prevents services from continuing execution in corrupt or undefined state after asynchronous failures. They should be handled by always attaching `.catch()` handlers or wrapping `await` calls in `try/catch` blocks, and registering a top-level `process.on('unhandledRejection', ...)` handler for logging before clean shutdown.",
        "positive_paraphrase_1": {
            "query": "Explain why unhandled rejected promises cause Node.js servers to crash and how to handle them properly.",
            "reason": "Direct paraphrase explaining the throw behavior of unhandled rejections and proper handling.",
        },
        "positive_paraphrase_2": {
            "query": "In modern Node.js, what causes exit code 1 on unhandled promise rejections and how do you catch them?",
            "reason": "Syntactic variation focusing on exit code 1 and error handling strategies for unhandled rejections.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure the --unhandled-rejections flag in Node.js to warn or strict mode?",
            "response": "Pass `--unhandled-rejections=warn` on the command line or via `NODE_OPTIONS` to log a warning instead of throwing an error, or `strict` to crash immediately.",
            "reason": "Asks about Node.js CLI flags rather than explaining error handling patterns and crash rationale.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Express error-handling middleware that catches rejected promises from async route handlers.",
            "response": "```javascript\nconst asyncHandler = fn => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);\napp.get('/api', asyncHandler(async (req, res) => { throw new Error('fail'); }));\n```",
            "reason": "Requests specific Express async wrapper implementation rather than explaining runtime unhandled rejection semantics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Python asyncio handle unretrieved exceptions in asyncio.Task objects?",
            "response": "In Python asyncio, if a task fails and its exception is never retrieved via `task.result()` or `await`, Python logs an 'Task exception was never retrieved' error upon garbage collection without crashing.",
            "reason": "Asks about Python asyncio task exceptions rather than Node.js promise rejections.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between process.on('uncaughtException') and process.on('unhandledRejection') in Node.js?",
            "response": "`uncaughtException` handles synchronous errors that bubbled up through the call stack without a try/catch, while `unhandledRejection` handles rejected Promises that had no rejection handler attached.",
            "reason": "Contrasts synchronous uncaught exceptions with asynchronous promise rejections.",
        },
    },

    # 145. Tracing System Calls with strace
    {
        "intent_group_id": "ig_dbg_strace_syscall_tracing",
        "domain": "software_debugging",
        "subtopic": "Crash Dumps & Syscall Tracing",
        "task_kind": "debugging",
        "dimensions": {"tool": {"status": "known", "value": "strace"}},
        "canonical_query": "How do you use strace on Linux to troubleshoot a hanging process or missing configuration file?",
        "canonical_response": "Attach strace to a running process using `strace -p <PID>` or start a command under strace: `strace -f -tt -T -e trace=file,network <command>`. Use `-e trace=open,openat,access` to see every configuration file path the process attempts to read and the returned `ENOENT` errors. To troubleshoot a hanging process, inspect the last system call printed (often `futex`, `select`, `epoll_wait`, or `read(socket)`), where `-T` displays the time spent inside that blocking syscall.",
        "positive_paraphrase_1": {
            "query": "What strace options do you use on Linux to find missing files and identify where a process is stuck hanging?",
            "reason": "Direct paraphrase explaining strace usage for file lookup failures and hanging syscalls.",
        },
        "positive_paraphrase_2": {
            "query": "How can you trace file opening attempts and blocking system calls using strace -p on a running Linux PID?",
            "reason": "Syntactic variation focusing on tracing file operations and blocking calls with strace.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you run strace with -c to generate a summary histogram of syscall counts and elapsed execution time?",
            "response": "Run `strace -c <command>`: strace collects statistics on every system call and prints a tabular summary showing % time, seconds, usecs/call, total calls, and errors per syscall.",
            "reason": "Asks how to generate summary statistics with strace -c rather than troubleshooting file access or hangs.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide a shell command to filter strace output with grep for failed openat calls returning ENOENT.",
            "response": "```bash\nstrace -f -e trace=openat ./app 2>&1 | grep ENOENT\n```",
            "reason": "Requests a specific shell pipeline rather than explaining strace troubleshooting flags and interpretation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do you trace system calls on macOS using DTrace or dtruss instead of strace?",
            "response": "On macOS, SIP restricts ptrace; use `sudo dtruss -p <PID>` or `sudo dtrace -s script.d` to trace kernel syscall entries and returns.",
            "reason": "Asks about macOS DTrace/dtruss rather than Linux strace.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does attaching strace via ptrace introduce significant slowdown to high-frequency system calls?",
            "response": "ptrace forces two context switches per syscall (one at syscall entry and one at exit) where the kernel halts the tracee and signals the tracer process, adding microsecond latency to every operation.",
            "reason": "Focuses on ptrace context switch performance overhead rather than troubleshooting workflow.",
        },
    },

    # 146. Capturing Network Packets with tcpdump
    {
        "intent_group_id": "ig_dbg_tcpdump_network_capture",
        "domain": "software_debugging",
        "subtopic": "Crash Dumps & Syscall Tracing",
        "task_kind": "debugging",
        "dimensions": {"tool": {"status": "known", "value": "tcpdump"}},
        "canonical_query": "How do you capture and filter network traffic with tcpdump to diagnose API connection drops?",
        "canonical_response": "Run `tcpdump -i any -nn -s0 -v \"tcp port 8080 and host 10.0.0.5\" -w capture.pcap`. Key flags include `-i any` (listen on all interfaces), `-nn` (prevent DNS and port name lookups for performance), `-s0` (capture full packet payload), and `-w` to write raw pcap files for Wireshark inspection. Filter for connection drops by targeting TCP control flags, such as `tcp[tcpflags] & (tcp-rst|tcp-fin) != 0` to capture unexpected RST (reset) packets.",
        "positive_paraphrase_1": {
            "query": "What tcpdump command flags and Berkeley Packet Filters (BPF) capture TCP reset and dropped connections?",
            "reason": "Direct paraphrase explaining tcpdump flags and BPF filters for diagnosing connection drops.",
        },
        "positive_paraphrase_2": {
            "query": "How do you record packets with tcpdump -w to inspect connection resets and drops in Wireshark?",
            "reason": "Syntactic variation focusing on saving tcpdump pcap files to diagnose resets.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you print packet ASCII payloads in real time on the terminal using tcpdump -A or -X?",
            "response": "Use `tcpdump -i eth0 -A -s 1500 'tcp port 80'` to print HTTP request/response payloads in plain ASCII text directly to stdout.",
            "reason": "Asks how to print ASCII payloads in terminal rather than diagnosing connection drops and resets.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a bash script using tshark to parse a pcap file and count the number of TCP RST packets per source IP.",
            "response": "```bash\ntshark -r capture.pcap -Y \"tcp.flags.reset == 1\" -T fields -e ip.src | sort | uniq -c | sort -nr\n```",
            "reason": "Requests tshark bash script to parse pcap files rather than explaining tcpdump capture methodology.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do you capture network packets programmatically in Python using Scapy's sniff() function?",
            "response": "Use `from scapy.all import sniff; packets = sniff(filter=\"tcp port 80\", count=10)` to capture and parse packet objects in Python.",
            "reason": "Asks about Python Scapy packet sniffing rather than tcpdump CLI usage.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between packet filtering with BPF in kernel space versus userspace filtering?",
            "response": "BPF filters run directly inside the Linux kernel packet tap, discarding non-matching packets before copying bytes across the kernel-to-userspace memory boundary, avoiding massive buffer drops.",
            "reason": "Focuses on kernel BPF architecture vs userspace copy overhead rather than connection drop debugging.",
        },
    },

    # 147. TCP Timeout vs Connection Refused
    {
        "intent_group_id": "ig_dbg_tcp_timeout_vs_refused",
        "domain": "software_debugging",
        "subtopic": "Crash Dumps & Syscall Tracing",
        "task_kind": "debugging",
        "dimensions": {"protocol": {"status": "known", "value": "tcp"}},
        "canonical_query": "What is the difference at the network level between a TCP 'Connection Refused' (ECONNREFUSED) and 'Connection Timed Out' (ETIMEDOUT)?",
        "canonical_response": "'Connection Refused' (ECONNREFUSED) occurs when the client's TCP SYN reaches the destination host, but no listening process is bound to the target port; the destination OS kernel actively sends back an immediate TCP RST (reset) packet. 'Connection Timed Out' (ETIMEDOUT) occurs when the client's SYN packets receive no response at all (dropped by a stateful firewall, security group, or unrouted IP), forcing the client OS to repeatedly retransmit SYN packets until the connection timeout expires.",
        "positive_paraphrase_1": {
            "query": "Explain why Connection Refused returns an immediate RST packet while Connection Timed Out waits for retransmissions.",
            "reason": "Direct paraphrase explaining the network differences between TCP RST and silent SYN drops.",
        },
        "positive_paraphrase_2": {
            "query": "What network events distinguish ECONNREFUSED from ETIMEDOUT when trying to connect to a server?",
            "reason": "Syntactic variation focusing on network events distinguishing ECONNREFUSED from ETIMEDOUT.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you tune TCP SYN retransmission counts on Linux using net.ipv4.tcp_syn_retries sysctl?",
            "response": "Reduce SYN timeout duration by setting `sysctl -w net.ipv4.tcp_syn_retries=3`, which limits SYN retransmissions from the default 6 attempts (approx 127s) down to roughly 7s.",
            "reason": "Asks how to tune the Linux sysctl tcp_syn_retries setting rather than explaining ECONNREFUSED vs ETIMEDOUT.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script using the socket module that attempts a TCP connection and catches ECONNREFUSED and ETIMEDOUT separately.",
            "response": "```python\nimport socket, errno\ns = socket.socket()\ntry:\n    s.connect(('1.2.3.4', 80))\nexcept OSError as e:\n    if e.errno == errno.ECONNREFUSED: print('Refused')\n    elif e.errno == errno.ETIMEDOUT: print('Timeout')\n```",
            "reason": "Requests Python socket error-handling code rather than explaining network layer behavior.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does HTTP 504 Gateway Timeout differ from HTTP 502 Bad Gateway in reverse proxy error responses?",
            "response": "HTTP 502 indicates the reverse proxy received an invalid response or ECONNREFUSED from upstream; HTTP 504 indicates the proxy's connection or read from upstream timed out.",
            "reason": "Asks about HTTP status codes 502 vs 504 rather than TCP layer ECONNREFUSED vs ETIMEDOUT.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does a firewall configured to REJECT produce ECONNREFUSED while DROP produces ETIMEDOUT?",
            "response": "A firewall rule with target `REJECT` actively generates and returns an ICMP Port Unreachable or TCP RST packet, causing immediate ECONNREFUSED, whereas `DROP` silently discards the packet, producing ETIMEDOUT.",
            "reason": "Focuses on firewall REJECT vs DROP rule configuration rather than host TCP stack behavior.",
        },
    },
]
