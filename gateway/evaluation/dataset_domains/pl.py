"""
Domain 1: Programming Languages (21 intent groups).
Covers Rust, Python, Go, TypeScript, C++, and Java.
"""

from typing import Any, Dict, List

PL_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 1. Rust Ownership & Borrowing
    {
        "intent_group_id": "ig_pl_rust_ownership_borrowing",
        "domain": "programming_languages",
        "subtopic": "Rust",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "rust"}},
        "canonical_query": "How does the Rust borrow checker prevent data races and use-after-free errors at compile time?",
        "canonical_response": "The Rust borrow checker enforces ownership invariants at compile time: every value has a unique owner, and references must either be a single mutable reference (&mut T) or any number of immutable references (&T), but never both simultaneously. This prevents data races and use-after-free without a garbage collector.",
        "positive_paraphrase_1": {
            "query": "In what way does Rust eliminate data races and dangling pointers through its borrow checker?",
            "reason": "Direct paraphrase with identical technical intent: explains compile-time borrow checker guarantees.",
        },
        "positive_paraphrase_2": {
            "query": "Can you explain how Rust's ownership and reference aliasing rules guarantee memory safety without a GC?",
            "reason": "Syntactic variation asking for the same explanation of compile-time memory safety via ownership.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you bypass or disable the Rust borrow checker using raw pointers and unsafe blocks?",
            "response": "To bypass the borrow checker in Rust, you use raw pointers (*const T or *mut T) inside unsafe blocks, which removes compile-time aliasing and lifetime checks.",
            "reason": "Asks how to bypass/disable the borrow checker rather than how it enforces safety.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide a complete Rust code implementation demonstrating interior mutability using RefCell and custom Drop.",
            "response": "```rust\nuse std::cell::RefCell;\nstruct Container {\n    inner: RefCell<Vec<i32>>,\n}\nimpl Drop for Container {\n    fn drop(&mut self) {\n        println!(\"Cleaned up container\");\n    }\n}\n```",
            "reason": "Requests concrete Rust code implementing RefCell and Drop rather than an explanation of borrow checker invariants.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Go runtime detect and prevent data races in concurrent goroutines?",
            "response": "Go provides a runtime race detector enabled via the -race flag and relies on sync primitives like Mutex and channels, rather than compile-time borrow checking.",
            "reason": "Inquires about Go runtime race detection rather than Rust compile-time borrow checking.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What categories of memory bugs can the Rust borrow checker NOT prevent at compile time?",
            "response": "The borrow checker cannot prevent logical memory leaks (e.g., circular references with Arc), stack overflow from unbounded recursion, or deadlocks.",
            "reason": "Asks for memory bugs Rust CANNOT prevent (negation) rather than what it prevents.",
        },
    },

    # 2. Rust Lifetimes in Structs
    {
        "intent_group_id": "ig_pl_rust_lifetimes_structs",
        "domain": "programming_languages",
        "subtopic": "Rust",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "rust"}},
        "canonical_query": "Why are explicit lifetime annotations required when a Rust struct holds references?",
        "canonical_response": "Explicit lifetime annotations on structs (such as struct View<'a> { data: &'a str }) inform the compiler how long the referenced data must remain valid relative to the struct instance, ensuring the struct cannot outlive the underlying borrowed data.",
        "positive_paraphrase_1": {
            "query": "What is the purpose of lifetime parameters in Rust struct definitions containing references?",
            "reason": "Direct rephrasing asking why structs with references require lifetime parameters.",
        },
        "positive_paraphrase_2": {
            "query": "Why does the Rust compiler demand explicit 'a lifetime specifiers for reference fields in structs?",
            "reason": "Syntactic variation addressing compiler lifetime requirements on struct reference fields.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you refactor a Rust struct holding references to own its data using String or Cow?",
            "response": "To eliminate lifetime parameters on structs, replace borrowed reference fields like &'a str with owned types like String or Cow<'static, str>.",
            "reason": "Asks how to refactor to owned types rather than explaining why lifetime annotations are needed.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Rust struct definition with two distinct lifetime parameters and implement a getter method.",
            "response": "```rust\nstruct Context<'a, 'b> {\n    name: &'a str,\n    config: &'b [u8],\n}\nimpl<'a, 'b> Context<'a, 'b> {\n    fn get_name(&self) -> &'a str { self.name }\n}\n```",
            "reason": "Requests specific Rust code implementation with multiple lifetimes rather than theoretical explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do C++ templates handle reference lifetime bounds compared to Rust lifetimes?",
            "response": "C++ templates do not enforce lifetime bounds at compile time; dangling references result in undefined behavior unless managed via smart pointers.",
            "reason": "Asks about C++ template reference lifetime semantics rather than Rust struct lifetime rules.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "When does the Rust compiler elide lifetime annotations so they are NOT required?",
            "response": "The Rust compiler applies lifetime elision rules strictly to function signatures (single input reference, or &self method parameter), never to struct definitions.",
            "reason": "Asks when lifetime annotations are omitted (elision) rather than why they are required on structs.",
        },
    },

    # 3. Rust Send and Sync Concurrency Traits
    {
        "intent_group_id": "ig_pl_rust_concurrency_send_sync",
        "domain": "programming_languages",
        "subtopic": "Rust",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "rust"}},
        "canonical_query": "What is the difference between the Send and Sync marker traits in Rust concurrency?",
        "canonical_response": "In Rust, Send indicates that ownership of a type can be transferred across thread boundaries safely, whereas Sync indicates that it is safe to share references to the type (&T) across multiple threads concurrently (i.e., T is Sync if and only if &T is Send).",
        "positive_paraphrase_1": {
            "query": "How do Send and Sync traits differ in Rust's thread safety model?",
            "reason": "Direct paraphrase asking to differentiate Send from Sync in Rust.",
        },
        "positive_paraphrase_2": {
            "query": "Can you explain the distinction between transferring ownership (Send) and sharing references (Sync) across threads in Rust?",
            "reason": "Elaborated rephrasing asking for the exact same semantic distinction.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement unsafe Send and Sync for a custom raw pointer wrapper in Rust?",
            "response": "To implement Send and Sync manually, use `unsafe impl Send for MyWrapper {}` and `unsafe impl Sync for MyWrapper {}` after ensuring interior invariants are thread-safe.",
            "reason": "Asks how to manually implement the traits with unsafe code rather than explaining their conceptual difference.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Rust multithreaded worker pool spawning threads with crossbeam and channel communication.",
            "response": "```rust\nuse crossbeam_channel::unbounded;\nuse std::thread;\nfn run_pool() {\n    let (tx, rx) = unbounded::<i32>();\n    let h = thread::spawn(move || for msg in rx { println!(\"{}\", msg); });\n    tx.send(42).unwrap();\n}\n```",
            "reason": "Requests concrete multi-threaded worker pool code rather than an explanation of Send and Sync.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Java guarantee thread safety with the volatile keyword and synchronized blocks?",
            "response": "Java uses volatile for memory visibility and synchronized blocks for mutual exclusion, relying on runtime monitor locks rather than compile-time traits.",
            "reason": "Asks about Java concurrency keywords rather than Rust Send/Sync traits.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Which common Rust standard library types are explicitly NOT Send and NOT Sync?",
            "response": "Rc<T> is neither Send nor Sync because it uses non-atomic reference counting. RefCell<T> is Send if T is Send, but NOT Sync because borrow checks are not thread-safe.",
            "reason": "Asks for types that lack Send/Sync (negation / counter-examples) rather than the difference between the traits.",
        },
    },

    # 4. Rust Pattern Matching on Option and Result
    {
        "intent_group_id": "ig_pl_rust_pattern_matching_result",
        "domain": "programming_languages",
        "subtopic": "Rust",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "rust"}},
        "canonical_query": "How does Rust exhaustive pattern matching ensure safe error handling with Result and Option?",
        "canonical_response": "Rust's match expressions are verified by the compiler to be exhaustive, requiring code to handle both Ok/Err variants for Result and Some/None variants for Option. This makes unhandled null or error states impossible at compile time without explicit unwrap calls.",
        "positive_paraphrase_1": {
            "query": "In what way does exhaustive match in Rust prevent unhandled null and error states for Option and Result?",
            "reason": "Direct paraphrase asking how pattern matching guarantees complete error and null handling.",
        },
        "positive_paraphrase_2": {
            "query": "Why does Rust's compiler require all variants to be covered in match statements on Result and Option types?",
            "reason": "Syntactic variation asking why match exhaustiveness is enforced on Option/Result.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you use the question mark operator (?) to propagate errors in Rust functions instead of match?",
            "response": "The ? operator unwraps Ok(T) or returns Err(From::from(e)) immediately from the enclosing function, providing concise error propagation without verbose match statements.",
            "reason": "Asks how to use the ? operator for propagation rather than explaining match exhaustiveness.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete Rust error handling module defining a custom enum error with thiserror.",
            "response": "```rust\nuse thiserror::Error;\n#[derive(Error, Debug)]\npub enum AppError {\n    #[error(\"IO failure: {0}\")]\n    Io(#[from] std::io::Error),\n    #[error(\"Entity not found: {0}\")]\n    NotFound(String),\n}\n```",
            "reason": "Requests concrete custom error enum code with thiserror rather than pattern matching explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Swift handle error propagation with throw, try, and catch compared to Rust?",
            "response": "Swift uses typed throws and try/catch syntax resembling exceptions, whereas Rust treats errors as regular values in the Result<T, E> algebraic type.",
            "reason": "Asks about Swift error handling rather than Rust pattern matching on Result/Option.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "When can pattern matching in Rust cause runtime panics instead of compile-time safety?",
            "response": "Pattern matching causes runtime panics if unwrap(), expect(), or slice indexing is used without checking boundaries, or when refutable let bindings fail.",
            "reason": "Asks when pattern matching causes panics (unsafe failure modes) rather than compile-time safety.",
        },
    },

    # 5. Rust Arc<Mutex<T>> Shared State
    {
        "intent_group_id": "ig_pl_rust_smart_pointers_arc_mutex",
        "domain": "programming_languages",
        "subtopic": "Rust",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "rust"}},
        "canonical_query": "Why is the Arc<Mutex<T>> pattern commonly needed to share mutable state across threads in Rust?",
        "canonical_response": "Arc (Atomically Reference Counted) provides shared ownership across multiple threads by managing reference count atomically, while Mutex provides interior mutability and exclusive synchronization. Together, Arc<Mutex<T>> allows multiple threads to safely hold references and mutate the underlying data one at a time.",
        "positive_paraphrase_1": {
            "query": "What roles do Arc and Mutex play when sharing and mutating data between threads in Rust?",
            "reason": "Direct paraphrase asking for the respective roles of Arc and Mutex in concurrent shared state.",
        },
        "positive_paraphrase_2": {
            "query": "Explain why Rust requires combining Arc with Mutex for multi-threaded shared mutable state.",
            "reason": "Syntactic variation asking why Arc and Mutex are combined for concurrent mutation.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you replace Mutex with RwLock in an Arc<RwLock<T>> to optimize for read-heavy workloads?",
            "response": "Replace Mutex<T> with RwLock<T> and call .read().unwrap() for concurrent readers, reserving .write().unwrap() only for exclusive update operations.",
            "reason": "Asks how to switch to RwLock for read performance rather than explaining the Arc<Mutex<T>> pattern.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a working Rust program that increments a shared counter across 10 spawned threads using Arc<Mutex<i32>>.",
            "response": "```rust\nuse std::sync::{Arc, Mutex};\nuse std::thread;\nfn main() {\n    let counter = Arc::new(Mutex::new(0));\n    let mut handles = vec![];\n    for _ in 0..10 {\n        let c = Arc::clone(&counter);\n        handles.push(thread::spawn(move || { *c.lock().unwrap() += 1; }));\n    }\n    for h in handles { h.join().unwrap(); }\n}\n```",
            "reason": "Requests complete runnable Rust code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does C++ std::shared_ptr combined with std::mutex manage concurrent object mutation?",
            "response": "C++ std::shared_ptr maintains atomic reference counts, but access to the managed object requires manual synchronization with std::mutex or std::lock_guard.",
            "reason": "Asks about C++ shared_ptr and mutex rather than Rust Arc<Mutex<T>>.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can you NOT use Rc<RefCell<T>> across threads instead of Arc<Mutex<T>> in Rust?",
            "response": "Rc<RefCell<T>> cannot be used across threads because Rc reference count mutations are non-atomic and RefCell borrow flag tracking is not synchronized, violating Send/Sync.",
            "reason": "Asks why single-threaded Rc<RefCell<T>> cannot be used across threads (negation/invalidation).",
        },
    },

    # 6. Python Asyncio Event Loop
    {
        "intent_group_id": "ig_pl_python_asyncio_event_loop",
        "domain": "programming_languages",
        "subtopic": "Python",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "python"}},
        "canonical_query": "How does the Python asyncio event loop manage non-blocking cooperative multitasking?",
        "canonical_response": "The asyncio event loop runs on a single thread and maintains a queue of ready tasks. When a coroutine yields control via 'await' on an I/O operation or future, the event loop registers the OS socket with epoll/kqueue and executes other ready tasks until the I/O event fires.",
        "positive_paraphrase_1": {
            "query": "Explain how asyncio's single-threaded event loop schedules coroutines and handles asynchronous I/O in Python.",
            "reason": "Direct paraphrase explaining asyncio cooperative single-threaded event loop scheduling.",
        },
        "positive_paraphrase_2": {
            "query": "In what manner does the Python event loop achieve concurrency through cooperative multitasking and await?",
            "reason": "Syntactic variation asking how the event loop coordinates cooperative multitasking.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you run CPU-bound blocking code inside an asyncio event loop using run_in_executor?",
            "response": "To execute CPU-heavy blocking code without stalling the event loop, use `await loop.run_in_executor(None, blocking_func, arg)` to offload work to a thread pool.",
            "reason": "Asks how to offload blocking CPU work to an executor rather than explaining how the event loop works.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script using asyncio.gather and a Semaphore to scrape 50 URLs with a concurrency limit of 5.",
            "response": "```python\nimport asyncio, aiohttp\nasync def fetch(url, sem, session):\n    async with sem, session.get(url) as r:\n        return await r.text()\nasync def main(urls):\n    sem = asyncio.Semaphore(5)\n    async with aiohttp.ClientSession() as s:\n        return await asyncio.gather(*(fetch(u, sem, s) for u in urls))\n```",
            "reason": "Requests concrete web scraping code with Semaphore and gather rather than event loop explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Node.js libuv event loop phase architecture differ from Python's asyncio loop?",
            "response": "Node.js libuv divides event processing into explicit phases (timers, I/O polling, check/setImmediate, close callbacks), whereas asyncio uses a unified task-queue and selector model.",
            "reason": "Asks about Node.js libuv event loop phases rather than Python asyncio.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes the entire asyncio event loop to freeze and stall all concurrent tasks?",
            "response": "The event loop freezes whenever a coroutine performs synchronous blocking I/O (like time.sleep() or requests.get()) or long CPU computation without yielding control.",
            "reason": "Asks what freezes/stalls the event loop (failure mode) rather than how it manages cooperative multitasking.",
        },
    },

    # 7. Python GIL and Concurrency
    {
        "intent_group_id": "ig_pl_python_gil_concurrency",
        "domain": "programming_languages",
        "subtopic": "Python",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "python"}},
        "canonical_query": "Why does Python's Global Interpreter Lock (GIL) prevent multi-threaded CPU parallelism?",
        "canonical_response": "The GIL is a mutex that protects CPython internal data structures and reference counting from concurrent access. Because only one native thread can hold the GIL and execute Python bytecode at any given moment, multi-threaded CPU-bound programs cannot utilize multiple CPU cores in parallel.",
        "positive_paraphrase_1": {
            "query": "What is the reason CPython's GIL restricts CPU-bound tasks from running in parallel on multiple cores?",
            "reason": "Direct paraphrase asking why the GIL blocks CPU parallelism in CPython.",
        },
        "positive_paraphrase_2": {
            "query": "How does the Global Interpreter Lock in Python limit multi-threading performance for compute-heavy workloads?",
            "reason": "Syntactic variation asking how the GIL limits compute-heavy thread performance.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you bypass GIL limitations for CPU-intensive tasks using Python's multiprocessing module?",
            "response": "Use `multiprocessing.Pool` or `ProcessPoolExecutor`, which spawn independent Python OS processes each with their own memory space and GIL, allowing true multi-core scaling.",
            "reason": "Asks how to bypass the GIL with multiprocessing rather than explaining why it prevents thread parallelism.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script comparing execution times of threading.Thread vs multiprocessing.Process for matrix multiplication.",
            "response": "```python\nimport time, threading, multiprocessing\ndef calc(): sum(i*i for i in range(10_000_000))\n# Benchmark threading vs multiprocessing\n```",
            "reason": "Requests benchmarking code for threads vs processes rather than explaining GIL mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Ruby YARV Global VM Lock (GVL) compare to Python's GIL in multi-core execution?",
            "response": "Ruby's GVL functions similarly to CPython's GIL, permitting only one thread to execute Ruby VM bytecode at a time, though Ruby 3 offers Ractors for shared-nothing parallelism.",
            "reason": "Asks about Ruby GVL rather than Python CPython GIL.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "When does multi-threading in Python actually improve performance despite the GIL?",
            "response": "Multi-threading improves performance for I/O-bound tasks (network calls, disk reads, database queries) because CPython releases the GIL while waiting for OS I/O operations.",
            "reason": "Asks when threading works well (I/O workloads) rather than why the GIL stops CPU parallelism.",
        },
    },

    # 8. Python Generators and Yield
    {
        "intent_group_id": "ig_pl_python_generators_yield",
        "domain": "programming_languages",
        "subtopic": "Python",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "python"}},
        "canonical_query": "How do Python generators using the yield keyword optimize memory usage during large dataset processing?",
        "canonical_response": "Generators produce items lazily on demand one at a time using 'yield', pausing function state and local variables between iterations. This avoids loading the entire dataset into memory simultaneously, resulting in O(1) auxiliary memory consumption instead of O(N).",
        "positive_paraphrase_1": {
            "query": "Why do generator functions with yield consume significantly less memory when iterating over massive data in Python?",
            "reason": "Direct paraphrase asking why yield minimizes memory consumption on large data.",
        },
        "positive_paraphrase_2": {
            "query": "Explain lazy evaluation in Python generators and how it achieves constant memory overhead.",
            "reason": "Syntactic variation focusing on lazy evaluation and constant memory properties.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you send values into a running Python generator using the generator.send() method?",
            "response": "Calling `gen.send(val)` resumes the generator and causes the current `yield` expression to evaluate to `val`, enabling two-way coroutine communication.",
            "reason": "Asks how to use generator.send() for input rather than explaining memory optimization.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python generator pipeline that reads a 10GB CSV file line by line, filters invalid rows, and yields parsed dicts.",
            "response": "```python\ndef stream_csv(path):\n    with open(path) as f:\n        for line in f:\n            row = line.strip().split(',')\n            if len(row) > 3:\n                yield {'id': row[0], 'val': row[1]}\n```",
            "reason": "Requests concrete CSV streaming pipeline code rather than conceptual memory explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do JavaScript generator functions (function*) and yield compare to async iterators (for-await-of)?",
            "response": "JavaScript synchronous generators yield values immediately, whereas async generators yield promises and require `for await...of` loops to consume asynchronous stream data.",
            "reason": "Asks about JavaScript generators and async iterators rather than Python memory optimization.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if you convert a Python generator to a list using list(gen) on a large dataset?",
            "response": "Calling list(gen) immediately forces evaluation of all remaining items, allocating full memory for all elements and completely eliminating the memory savings of the generator.",
            "reason": "Asks what happens when generator memory savings are destroyed via eager list conversion.",
        },
    },

    # 9. Python Descriptors and Property
    {
        "intent_group_id": "ig_pl_python_descriptors_properties",
        "domain": "programming_languages",
        "subtopic": "Python",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "python"}},
        "canonical_query": "How does the Python descriptor protocol (__get__, __set__, __delete__) power @property decorators and methods?",
        "canonical_response": "The descriptor protocol intercepts attribute access on class instances. The @property decorator implements __get__ and optional __set__/__delete__, turning attribute lookups (obj.x) into method calls. When an attribute is accessed, Python checks the class dict for a descriptor before checking instance __dict__.",
        "positive_paraphrase_1": {
            "query": "In what way does the descriptor protocol with __get__ and __set__ underlie the @property decorator in Python?",
            "reason": "Direct paraphrase asking how descriptors enable property decorators.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how Python resolves attribute lookups through descriptors and how @property implements this protocol.",
            "reason": "Syntactic variation asking for attribute resolution mechanics via descriptors.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement a reusable data validation descriptor in Python to enforce positive integer values?",
            "response": "```python\nclass PositiveInteger:\n    def __set_name__(self, owner, name):\n        self.name = name\n    def __set__(self, obj, value):\n        if value <= 0: raise ValueError()\n        obj.__dict__[self.name] = value\n```",
            "reason": "Asks for code implementing a custom validator descriptor rather than explaining how the protocol works.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python class using @property with both getter, setter, and deleter methods for a temperature converter.",
            "response": "```python\nclass Temp:\n    def __init__(self, c): self._c = c\n    @property\n    def f(self): return self._c * 9/5 + 32\n    @f.setter\n    def f(self, val): self._c = (val - 32) * 5/9\n```",
            "reason": "Requests specific property decorator code implementation rather than explaining the descriptor protocol.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do JavaScript Object.defineProperty getters and setters compare to Python descriptors?",
            "response": "JavaScript getters/setters are defined directly on property descriptors via Object.defineProperty, executing functions on property access without Python's class-level protocol methods.",
            "reason": "Asks about JavaScript Object.defineProperty rather than Python descriptors.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between a data descriptor and a non-data descriptor in Python attribute precedence?",
            "response": "A data descriptor defines both __get__ and __set__ and takes precedence over instance __dict__. A non-data descriptor defines only __get__ and can be overridden by instance attributes.",
            "reason": "Asks specifically about data vs non-data descriptor precedence differences rather than general @property mechanism.",
        },
    },

    # 10. Python Context Managers
    {
        "intent_group_id": "ig_pl_python_context_managers",
        "domain": "programming_languages",
        "subtopic": "Python",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "python"}},
        "canonical_query": "How do Python context managers ensure deterministic resource cleanup using __enter__ and __exit__?",
        "canonical_response": "The 'with' statement invokes __enter__ at block entry and guarantees __exit__ is called upon exit, even if exceptions are raised. The __exit__ method receives exception type, value, and traceback arguments, allowing cleanup code (like closing files or releasing locks) to execute reliably.",
        "positive_paraphrase_1": {
            "query": "Why does the 'with' statement in Python guarantee reliable cleanup through __enter__ and __exit__ methods?",
            "reason": "Direct paraphrase asking why with statements guarantee resource cleanup.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how Python context managers manage resource acquisition and release during normal and exceptional exits.",
            "reason": "Syntactic variation asking how context managers handle normal and exceptional exits.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you suppress specific exceptions inside a Python context manager's __exit__ method?",
            "response": "To suppress an exception in __exit__, return True from the method. If __exit__ returns True, Python swallows the active exception and continues execution normally.",
            "reason": "Asks specifically how to suppress exceptions by returning True rather than general cleanup mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python context manager using the @contextmanager generator decorator to time code block execution.",
            "response": "```python\nfrom contextlib import contextmanager\nimport time\n@contextmanager\ndef timer():\n    t0 = time.perf_counter()\n    try: yield\n    finally: print(f\"Elapsed: {time.perf_counter()-t0:.4f}s\")\n```",
            "reason": "Requests specific timing decorator code rather than explaining __enter__/__exit__ guarantees.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Java try-with-resources and the AutoCloseable interface compare to Python context managers?",
            "response": "Java try-with-resources calls close() on objects implementing AutoCloseable at block exit, providing compile-time type safety but lacking Python's flexible exception suppression mechanism.",
            "reason": "Asks about Java AutoCloseable rather than Python context managers.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Under what extreme circumstances can a Python context manager fail to execute its __exit__ method?",
            "response": "__exit__ will not execute if the process is terminated abruptly via SIGKILL, os._exit(), or if the interpreter crashes due to an unhandled C-extension segmentation fault.",
            "reason": "Asks when __exit__ fails to execute (process termination) rather than how it guarantees cleanup.",
        },
    },

    # 11. Python Dataclasses vs Pydantic
    {
        "intent_group_id": "ig_pl_python_dataclasses_pydantic",
        "domain": "programming_languages",
        "subtopic": "Python",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "python"}},
        "canonical_query": "What are the primary architectural differences between standard Python @dataclass and Pydantic BaseModel?",
        "canonical_response": "Standard dataclasses generate boilerplate methods (__init__, __repr__, __eq__) and type annotations serve only as documentation without runtime enforcement. Pydantic BaseModel performs strict runtime data validation, automatic type coercion, and JSON schema parsing at runtime.",
        "positive_paraphrase_1": {
            "query": "How do standard Python dataclasses differ from Pydantic models regarding runtime validation and type coercion?",
            "reason": "Direct paraphrase comparing dataclasses with Pydantic on validation and coercion.",
        },
        "positive_paraphrase_2": {
            "query": "Explain the differences between @dataclass and Pydantic BaseModel for data modeling and parsing in Python.",
            "reason": "Syntactic variation asking to compare dataclass and BaseModel for data modeling.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you define custom field validators using @field_validator in Pydantic v2?",
            "response": "In Pydantic v2, use `@field_validator('field_name')` with a classmethod that accepts the value and returns the validated or coerced output, raising ValueError on failure.",
            "reason": "Asks how to write custom validators in Pydantic rather than comparing dataclasses to Pydantic.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Pydantic v2 model with nested models, field aliases, and serialization aliases.",
            "response": "```python\nfrom pydantic import BaseModel, Field\nclass User(BaseModel):\n    user_id: int = Field(validation_alias='id', serialization_alias='userId')\n```",
            "reason": "Requests specific Pydantic v2 model code implementation rather than architectural comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does TypeScript interface and Zod schema validation compare to Python dataclasses and Pydantic?",
            "response": "TypeScript interfaces are erased at compile time like Python dataclass types, while Zod performs runtime validation and type inference like Pydantic.",
            "reason": "Asks about TypeScript Zod rather than Python dataclasses vs Pydantic.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why might you deliberately choose Python standard dataclasses OVER Pydantic for high-throughput internal objects?",
            "response": "Standard dataclasses have significantly lower instantiation overhead and zero third-party dependencies, making them preferable when input data is already validated and throughput is critical.",
            "reason": "Asks why one would prefer dataclasses over Pydantic (trade-off justification).",
        },
    },

    # 12. Go Goroutines and Channels
    {
        "intent_group_id": "ig_pl_go_goroutines_channels",
        "domain": "programming_languages",
        "subtopic": "Go",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "go"}},
        "canonical_query": "How does the Go runtime M-to-N work-stealing scheduler execute goroutines across OS threads?",
        "canonical_response": "Go maps M goroutines onto N OS threads (G-M-P model). Each logical processor (P) has a local run queue of goroutines. When a P runs out of runnable work, it steals half the goroutines from another processor's queue, maintaining high CPU core utilization with minimal global lock contention.",
        "positive_paraphrase_1": {
            "query": "Explain the G-M-P work-stealing model used by the Go runtime scheduler to manage goroutines.",
            "reason": "Direct paraphrase asking how the G-M-P work-stealing scheduler operates in Go.",
        },
        "positive_paraphrase_2": {
            "query": "How does Go achieve high-throughput concurrency through its M:N work-stealing scheduler?",
            "reason": "Syntactic variation focusing on M:N scheduling and work stealing.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you close a Go channel safely from multiple sender goroutines without causing a panic?",
            "response": "To prevent sending on a closed channel, use a sync.Once to close the channel exactly once, or signal senders to stop via a separate done cancellation channel.",
            "reason": "Asks how to close channels safely rather than explaining scheduler mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Go worker pool with 5 goroutines reading from a jobs channel and writing to a results channel.",
            "response": "```go\nfunc worker(id int, jobs <-chan int, results chan<- int) {\n    for j := range jobs { results <- j * 2 }\n}\n```",
            "reason": "Requests concrete Go worker pool implementation code rather than scheduler explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Erlang BEAM scheduler compare to Go's runtime scheduler for lightweight processes?",
            "response": "BEAM uses preemptive scheduling based on reduction counts with complete per-process heap isolation, whereas Go uses cooperative scheduling at function calls and safe points with shared memory.",
            "reason": "Asks about Erlang BEAM process scheduling rather than Go goroutine scheduling.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes a goroutine to block and transition its OS thread to sysmon monitoring in Go?",
            "response": "A goroutine blocks during synchronous system calls (cgo, file I/O). The sysmon background thread detects threads blocked for >10ms and detaches the P so other goroutines can run.",
            "reason": "Asks about sysmon detection of blocked OS threads rather than normal work-stealing execution.",
        },
    },

    # 13. Go Context Propagation and Cancellation
    {
        "intent_group_id": "ig_pl_go_context_cancellation",
        "domain": "programming_languages",
        "subtopic": "Go",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "go"}},
        "canonical_query": "How does context.Context propagation coordinate request timeouts and cancellation trees in Go?",
        "canonical_response": "Go's context.Context forms an immutable tree of cancellation scopes. When a parent context is cancelled or times out, its Done() channel closes, which cascades down to all derived child contexts, allowing long-running downstream operations and HTTP clients to abort gracefully.",
        "positive_paraphrase_1": {
            "query": "In what way does Go's context package propagate cancellation signals and deadlines down a call hierarchy?",
            "reason": "Direct paraphrase asking how context cancellation cascades down call hierarchies.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how context.WithTimeout and context.WithCancel manage resource termination across goroutines in Go.",
            "reason": "Syntactic variation addressing cancellation management across goroutines.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you attach and retrieve request-scoped values using context.WithValue in Go HTTP middleware?",
            "response": "Use `ctx = context.WithValue(req.Context(), key, val)` to store values, and `ctx.Value(key)` with a type assertion to retrieve them in downstream handlers.",
            "reason": "Asks how to use context.WithValue for request data rather than explaining cancellation trees.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Go HTTP client request function with a 3-second context deadline that closes idle connections on failure.",
            "response": "```go\nctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)\ndefer cancel()\nreq, _ := http.NewRequestWithContext(ctx, \"GET\", url, nil)\nresp, err := client.Do(req)\n```",
            "reason": "Requests specific HTTP client timeout code rather than conceptual context explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does C# CancellationTokenSource manage task cancellation compared to Go's context?",
            "response": "C# uses CancellationTokenSource and CancellationToken structs with registered callbacks, while Go relies on channel closure (<-ctx.Done()) within select blocks.",
            "reason": "Asks about C# cancellation tokens rather than Go context.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What resource leak occurs if you fail to call the cancel() function returned by context.WithTimeout in Go?",
            "response": "Failing to invoke cancel() causes the internal timer and context tree references to persist until the full timeout duration expires, leaking memory and goroutine resources.",
            "reason": "Asks about memory leaks from omitting cancel() rather than how cancellation propagates.",
        },
    },

    # 14. Go sync.Pool
    {
        "intent_group_id": "ig_pl_go_sync_pool_reuse",
        "domain": "programming_languages",
        "subtopic": "Go",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "go"}},
        "canonical_query": "How does sync.Pool reduce garbage collection pressure in high-throughput Go services?",
        "canonical_response": "sync.Pool maintains a thread-safe cache of reusable temporary objects. Instead of allocating new byte buffers or structs on the heap for every request, goroutines retrieve existing objects via Get() and return them with Put(). The GC can drain the pool during collection cycles, preventing unbounded memory growth.",
        "positive_paraphrase_1": {
            "query": "Why does using sync.Pool to recycle temporary buffers decrease GC overhead in Go applications?",
            "reason": "Direct paraphrase asking how sync.Pool object reuse lowers GC overhead.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how Go's sync.Pool amortizes heap allocations and interacts with the garbage collector.",
            "reason": "Syntactic variation asking how sync.Pool interacts with heap allocation and GC.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement a bounded worker pool in Go using buffered channels instead of sync.Pool?",
            "response": "Create a channel with capacity N, populate it with tokens, and have workers acquire a token before processing and release it after, bounding active concurrency.",
            "reason": "Asks how to build a concurrency rate-limiter pool rather than explaining sync.Pool memory caching.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Go implementation using sync.Pool for bytes.Buffer with proper reset before reuse.",
            "response": "```go\nvar bufPool = sync.Pool{\n    New: func() any { return new(bytes.Buffer) },\n}\nfunc process() {\n    b := bufPool.Get().(*bytes.Buffer)\n    b.Reset()\n    defer bufPool.Put(b)\n}\n```",
            "reason": "Requests concrete sync.Pool buffer recycling code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Java Commons Pool generic object pooling differ from Go's sync.Pool?",
            "response": "Java Commons Pool manages stateful bounded pools with validation and eviction policies, whereas Go sync.Pool is lightweight, lock-free per processor, and cleared by the GC.",
            "reason": "Asks about Java Apache Commons Pool rather than Go sync.Pool.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is sync.Pool completely unsuitable for managing database connections or network sockets in Go?",
            "response": "sync.Pool items may be deallocated by the garbage collector at any collection cycle without warning, which would silently drop and close active database or socket connections.",
            "reason": "Asks why sync.Pool cannot be used for connection pools (unsuitability / negation).",
        },
    },

    # 15. Go Interfaces and Structural Typing
    {
        "intent_group_id": "ig_pl_go_interfaces_dispatch",
        "domain": "programming_languages",
        "subtopic": "Go",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "go"}},
        "canonical_query": "How does Go's structural interface typing and dynamic dispatch work under the hood?",
        "canonical_response": "In Go, interfaces are satisfied implicitly without 'implements' declarations. At runtime, an interface value is represented as a two-word pair: an itable (itab) pointer containing type metadata and method pointers, and a pointer to the underlying data. Method calls dispatch through the itab.",
        "positive_paraphrase_1": {
            "query": "Explain how Go implements implicit interfaces and dynamic method dispatch using itables.",
            "reason": "Direct paraphrase explaining itable structure and implicit interface dispatch.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Go runtime represent interface values and dispatch method calls without explicit inheritance?",
            "reason": "Syntactic variation asking how interface values are represented and dispatched.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you perform type assertions and type switches on empty interface (any) in Go?",
            "response": "Use `val, ok := i.(string)` for safe assertions, or `switch v := i.(type) { case int: ... }` to branch based on concrete underlying types.",
            "reason": "Asks how to write type assertions and switches rather than explaining runtime itables.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Go logging decorator that wraps an io.Writer interface and counts written bytes.",
            "response": "```go\ntype CountingWriter struct {\n    w io.Writer\n    count int64\n}\nfunc (c *CountingWriter) Write(p []byte) (int, error) {\n    n, err := c.w.Write(p)\n    c.count += int64(n)\n    return n, err\n}\n```",
            "reason": "Requests specific io.Writer decorator code rather than itable dispatch explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Rust trait object dynamic dispatch (dyn Trait) vtable compare to Go's itable?",
            "response": "Rust dyn Trait uses fat pointers (data ptr + vtable ptr) constructed at compile time, whereas Go builds and caches itables dynamically at runtime when types are assigned to interfaces.",
            "reason": "Asks about Rust dyn Trait vtables rather than Go interfaces.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is an interface holding a nil concrete pointer NOT equal to nil in Go (if i == nil pitfall)?",
            "response": "An interface value is nil only if both its itab type pointer and its data pointer are nil. An interface holding a typed nil pointer has a valid itab, making `i == nil` evaluate to false.",
            "reason": "Asks about the typed nil pointer comparison pitfall rather than general dispatch mechanics.",
        },
    },

    # 16. Go Defer, Panic, and Recover
    {
        "intent_group_id": "ig_pl_go_defer_panic_recover",
        "domain": "programming_languages",
        "subtopic": "Go",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "go"}},
        "canonical_query": "How does Go's defer, panic, and recover mechanism unwind the call stack during runtime panics?",
        "canonical_response": "When a panic occurs, Go unwinds the stack frame by frame, executing deferred functions in LIFO order. If a deferred function invokes recover(), the panic is intercepted, stack unwinding halts, and normal execution resumes in the recovering function's caller.",
        "positive_paraphrase_1": {
            "query": "Explain how stack unwinding and deferred function execution operate during a Go panic and recovery.",
            "reason": "Direct paraphrase asking how panic unwinding and deferred execution work in Go.",
        },
        "positive_paraphrase_2": {
            "query": "In what order are deferred calls executed when a panic is triggered and caught via recover() in Go?",
            "reason": "Syntactic variation addressing LIFO defer execution and recover mechanics.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you return error values from deferred functions using named return values in Go?",
            "response": "Declare named return parameters like `(err error)`, then modify `err` inside `defer func() { if r := recover(); r != nil { err = fmt.Errorf(\"panic: %v\", r) } }()`.",
            "reason": "Asks how to mutate named returns inside defer rather than explaining unwinding mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Go HTTP middleware that catches handler panics, logs the stack trace, and returns HTTP 500.",
            "response": "```go\nfunc RecoverMiddleware(next http.Handler) http.Handler {\n    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {\n        defer func() {\n            if rec := recover(); rec != nil {\n                http.Error(w, \"Internal Server Error\", 500)\n            }\n        }()\n        next.ServeHTTP(w, r)\n    })\n}\n```",
            "reason": "Requests specific HTTP recovery middleware code rather than stack unwinding explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does C++ stack unwinding during exception handling compare to Go defer and panic?",
            "response": "C++ automatically invokes class destructors (RAII) during exception stack unwinding, whereas Go relies on explicitly registered defer functions and a separate recover mechanism.",
            "reason": "Asks about C++ exception unwinding rather than Go panic/recover.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Can recover() catch panics originating from a different spawned goroutine in Go?",
            "response": "No. A panic unwinds only the stack of the goroutine where it was raised. If not recovered inside that same goroutine, it terminates the entire Go process regardless of other goroutines.",
            "reason": "Asks about the boundary limitation of recover across goroutines (negation / cross-goroutine rule).",
        },
    },

    # 17. TypeScript Discriminated Unions
    {
        "intent_group_id": "ig_pl_ts_discriminated_unions",
        "domain": "programming_languages",
        "subtopic": "TypeScript",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "typescript"}},
        "canonical_query": "How do TypeScript discriminated unions enable compile-time exhaustive type narrowing?",
        "canonical_response": "Discriminated unions combine multiple object types sharing a common literal property (the discriminant, e.g. type: 'success' | 'error'). Within conditional or switch statements, the TypeScript compiler narrows the union to the specific variant, and an unhandled case can be caught using the 'never' type.",
        "positive_paraphrase_1": {
            "query": "In what way do tagged unions with a discriminant field allow type-safe narrowing in TypeScript?",
            "reason": "Direct paraphrase asking how tagged unions enable type narrowing.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how TypeScript verifies exhaustive switch handling over union types using the never type.",
            "reason": "Syntactic variation focusing on exhaustive switch checking with never.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you write custom user-defined type guards (arg is Type) in TypeScript?",
            "response": "A user-defined type guard is a function whose return type is a type predicate: `function isFish(pet: Pet): pet is Fish { return (pet as Fish).swim !== undefined; }`.",
            "reason": "Asks how to write custom type guard functions rather than explaining discriminated unions.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write TypeScript types for an API response discriminated union (Loading, Success, Error) with a render function.",
            "response": "```typescript\ntype State = \n  | { status: 'loading' }\n  | { status: 'success'; data: string }\n  | { status: 'error'; error: Error };\n```",
            "reason": "Requests concrete UI state union types and render code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Rust enum algebraic data types compare to TypeScript discriminated unions?",
            "response": "Rust enums are nominal, true algebraic sum types with direct payload storage, while TypeScript discriminated unions are structural overlays on JavaScript plain objects.",
            "reason": "Asks about Rust nominal enums rather than TypeScript structural unions.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What compiler flag must be enabled in tsconfig.json for exhaustive type checks to catch unhandled union cases?",
            "response": "Strict mode or `noImplicitReturns` combined with assigning unhandled switch defaults to a variable of type `never` will cause the compiler to fail if any variant is omitted.",
            "reason": "Asks about tsconfig compiler flags rather than how the narrowing mechanism works.",
        },
    },

    # 18. TypeScript Utility Types
    {
        "intent_group_id": "ig_pl_ts_utility_types",
        "domain": "programming_languages",
        "subtopic": "TypeScript",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "typescript"}},
        "canonical_query": "How do TypeScript built-in utility types Pick, Omit, and Partial transform interface types?",
        "canonical_response": "TypeScript utility types use mapped types and keyof lookups: Partial<T> marks all properties optional via [P in keyof T]?: T[P]; Pick<T, K> constructs a type with only keys K; Omit<T, K> uses Exclude<keyof T, K> to pick all keys except K.",
        "positive_paraphrase_1": {
            "query": "Explain how mapped types implement Partial, Pick, and Omit for type manipulation in TypeScript.",
            "reason": "Direct paraphrase asking how mapped types implement Pick, Omit, and Partial.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do Pick, Omit, and Partial modify object property modifiers and keys in TypeScript?",
            "reason": "Syntactic variation asking how property modifiers are transformed.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement a recursive DeepPartial<T> utility type using conditional types in TypeScript?",
            "response": "```typescript\ntype DeepPartial<T> = T extends object ? { [P in keyof T]?: DeepPartial<T[P]> } : T;\n```",
            "reason": "Asks for custom DeepPartial implementation rather than explaining built-in utility types.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write TypeScript interfaces for a User model and derive CreateUserDTO (Omit id) and UpdateUserDTO (Partial).",
            "response": "```typescript\ninterface User { id: string; name: string; email: string; }\ntype CreateUserDTO = Omit<User, 'id'>;\ntype UpdateUserDTO = Partial<CreateUserDTO>;\n```",
            "reason": "Requests specific DTO type definition code rather than explaining utility type mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Rust derive macro system compare to TypeScript utility types for modifying structs?",
            "response": "Rust derive macros generate concrete code at compile time via procedural macros, whereas TypeScript utility types compute structural type representations without emitting code.",
            "reason": "Asks about Rust derive macros rather than TypeScript utility types.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Omit<T, K> lose union distribution when applied to union types in TypeScript?",
            "response": "Omit is defined as Pick<T, Exclude<keyof T, K>>. When applied to an unresolved union, keyof T produces shared keys only, collapsing the distinct union variants unless wrapped in a distributive helper.",
            "reason": "Asks about the union distribution collapse pitfall with Omit rather than general utility types.",
        },
    },

    # 19. C++ RAII and Smart Pointers
    {
        "intent_group_id": "ig_pl_cpp_raii_smart_pointers",
        "domain": "programming_languages",
        "subtopic": "C++",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "cpp"}},
        "canonical_query": "How does RAII (Resource Acquisition Is Initialization) prevent resource leaks in modern C++?",
        "canonical_response": "RAII binds the lifecycle of resources (heap memory, file handles, mutex locks) to the lifetime of stack objects. When a stack object goes out of scope, its destructor is automatically invoked during normal execution or exception stack unwinding, guaranteeing resource deallocation.",
        "positive_paraphrase_1": {
            "query": "Explain how modern C++ uses RAII to guarantee automatic cleanup of heap memory and file handles.",
            "reason": "Direct paraphrase asking how RAII ensures automatic cleanup.",
        },
        "positive_paraphrase_2": {
            "query": "Why does binding resource destruction to stack object lifetime in C++ eliminate memory leaks?",
            "reason": "Syntactic variation focusing on stack lifetime binding and leak prevention.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you break circular reference cycles between std::shared_ptr instances using std::weak_ptr?",
            "response": "Use `std::weak_ptr` for back-references or observer pointers. Because weak_ptr does not increment the reference count, it prevents cycles from keeping objects alive indefinitely.",
            "reason": "Asks how to break circular cycles with weak_ptr rather than explaining RAII principles.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a custom C++ RAII class that wraps a POSIX socket file descriptor and closes it in the destructor.",
            "response": "```cpp\nclass SocketRAII {\n    int fd;\npublic:\n    explicit SocketRAII(int f) : fd(f) {}\n    ~SocketRAII() { if (fd >= 0) close(fd); }\n};\n```",
            "reason": "Requests specific C++ socket wrapper code rather than conceptual RAII explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Python __enter__ and __exit__ context managers compare to C++ RAII destructors?",
            "response": "C++ RAII executes destructors automatically on scope exit, while Python requires an explicit `with` statement block to trigger context manager cleanup.",
            "reason": "Asks about Python context managers compared to C++ RAII.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is std::unique_ptr preferred over std::shared_ptr in modern C++ when shared ownership is not needed?",
            "response": "std::unique_ptr has zero runtime overhead (same size as raw pointer) and no control block or atomic operations, whereas std::shared_ptr requires heap-allocated control blocks and atomic increments.",
            "reason": "Asks why unique_ptr is preferred over shared_ptr (performance trade-off) rather than explaining RAII.",
        },
    },

    # 20. C++ Move Semantics
    {
        "intent_group_id": "ig_pl_cpp_move_semantics",
        "domain": "programming_languages",
        "subtopic": "C++",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "cpp"}},
        "canonical_query": "How do C++11 rvalue references (&&) and move semantics eliminate deep copies of heap memory?",
        "canonical_response": "Move semantics allow an object to steal resources from an expiring rvalue temporary rather than performing an expensive deep copy. By defining a move constructor accepting T&&, pointer ownership of internal buffers is transferred directly to the new object, leaving the source in a valid but empty state.",
        "positive_paraphrase_1": {
            "query": "In what way do rvalue references and std::move prevent unnecessary copying of dynamically allocated data in C++?",
            "reason": "Direct paraphrase explaining how move semantics prevent deep copying.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how transferring ownership of heap buffers via move constructors optimizes performance in C++11.",
            "reason": "Syntactic variation addressing buffer ownership transfer and performance optimization.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement perfect forwarding in C++ template functions using std::forward and universal references?",
            "response": "Declare parameters as `T&&` (forwarding reference) and pass them as `std::forward<T>(arg)` to preserve their original value category (lvalue vs rvalue) when delegating to another function.",
            "reason": "Asks how to implement perfect forwarding rather than explaining move semantics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a C++ class with a dynamic int array implementing both move constructor and move assignment operator with noexcept.",
            "response": "```cpp\nclass Buffer {\n    int* data; size_t sz;\npublic:\n    Buffer(Buffer&& o) noexcept : data(o.data), sz(o.sz) { o.data = nullptr; o.sz = 0; }\n};\n```",
            "reason": "Requests specific move constructor code implementation rather than theoretical explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Rust destructive move semantics by default differ from C++ non-destructive move semantics?",
            "response": "Rust moves are destructive: assigning a value invalidates the source at compile time. In C++, moving leaves the source object alive in a valid but unspecified state, requiring destructor cleanup.",
            "reason": "Asks about Rust destructive move semantics vs C++ non-destructive move semantics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does std::move not actually move anything by itself in C++?",
            "response": "std::move does not perform memory transfers or code execution; it is merely an unconditional static_cast converting an lvalue expression into an rvalue reference (T&&) to enable move constructors to be selected.",
            "reason": "Asks what std::move actually does (static_cast) rather than how move semantics transfer buffers.",
        },
    },

    # 21. Java Virtual Threads
    {
        "intent_group_id": "ig_pl_java_virtual_threads",
        "domain": "programming_languages",
        "subtopic": "Java",
        "task_kind": "explanation",
        "dimensions": {"language": {"status": "known", "value": "java"}},
        "canonical_query": "How do Java 21 virtual threads (Project Loom) achieve high concurrency compared to platform threads?",
        "canonical_response": "Platform threads are 1:1 wrappers around OS threads with fixed megabyte stack allocations. Java 21 virtual threads are M:N user-mode threads managed by the JVM. They are mounted on a small pool of carrier OS threads; when a virtual thread performs blocking I/O, it unmounts from its carrier, allowing other tasks to run.",
        "positive_paraphrase_1": {
            "query": "Explain how Project Loom virtual threads allow millions of concurrent tasks with minimal memory overhead in Java 21.",
            "reason": "Direct paraphrase asking how Project Loom virtual threads achieve scalability.",
        },
        "positive_paraphrase_2": {
            "query": "What is the architectural difference between JVM carrier threads and virtual threads in Java 21?",
            "reason": "Syntactic variation asking to contrast carrier platform threads and virtual threads.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure an ExecutorService in Java 21 to use virtual threads via Executors.newVirtualThreadPerTaskExecutor()?",
            "response": "```java\ntry (var executor = Executors.newVirtualThreadPerTaskExecutor()) {\n    executor.submit(() -> doWork());\n}\n```",
            "reason": "Asks how to instantiate the virtual thread executor rather than explaining JVM scheduler architecture.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete Java 21 Spring Boot controller handling 10,000 simulated concurrent HTTP requests using virtual threads.",
            "response": "```java\n@RestController\npublic class BenchController {\n    @GetMapping(\"/test\")\n    public String test() throws InterruptedException {\n        Thread.sleep(100); return \"ok\";\n    }\n}\n```",
            "reason": "Requests specific Spring Boot benchmark code rather than JVM threading explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Kotlin coroutines with suspend functions compare to Java 21 virtual threads?",
            "response": "Kotlin coroutines require explicit 'suspend' keyword markings and compiler transformations, whereas Java virtual threads run standard synchronous code without signature changes.",
            "reason": "Asks about Kotlin coroutines vs Java virtual threads.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes virtual thread pinning where a Java virtual thread cannot unmount from its carrier thread?",
            "response": "Virtual thread pinning occurs when code executes inside a synchronized block/method or invokes native methods (JNI), preventing the thread from unmounting during blocking I/O.",
            "reason": "Asks about carrier thread pinning (performance bottleneck) rather than how virtual threads achieve scale.",
        },
    },
]
