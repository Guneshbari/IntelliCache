"""
Domain 8: System Configuration (21 intent groups).
Covers Linux Kernel & OS Tuning, Docker & Containerization,
Systemd & Service Management, Nginx & Web Server Tuning.
"""

from typing import Any, Dict, List

SYS_CONFIG_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 148. Linux Kernel Listen Backlog (somaxconn & tcp_max_syn_backlog)
    {
        "intent_group_id": "ig_sys_linux_kernel_listen_backlog",
        "domain": "system_configuration",
        "subtopic": "Linux Kernel & OS Tuning",
        "task_kind": "configuration",
        "dimensions": {"operating_system": {"status": "known", "value": "linux"}},
        "canonical_query": "How do net.core.somaxconn and tcp_max_syn_backlog configure TCP connection queues in the Linux kernel?",
        "canonical_response": "Linux uses two distinct queues during the TCP three-way handshake: the SYN queue (half-open connections that received SYN but haven't received final ACK) and the Accept queue (fully established connections waiting for the application to call `accept()`). `net.ipv4.tcp_max_syn_backlog` limits the size of the SYN queue. `net.core.somaxconn` specifies the upper ceiling for the Accept queue; the application's `listen(fd, backlog)` backlog argument is capped by `somaxconn` (`min(backlog, somaxconn)`).",
        "positive_paraphrase_1": {
            "query": "Explain how somaxconn and tcp_max_syn_backlog control the SYN and accept queues in Linux networking.",
            "reason": "Direct paraphrase explaining somaxconn, tcp_max_syn_backlog, and the two connection queues.",
        },
        "positive_paraphrase_2": {
            "query": "What is the relationship between the listen() backlog argument, somaxconn, and tcp_max_syn_backlog in Linux?",
            "reason": "Syntactic variation focusing on how listen() backlog relates to kernel queue parameters.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you detect listen queue overflows on a running Linux server using netstat -s or ss -lt?",
            "response": "Run `netstat -s | grep -i listen` to check 'times the listen queue of a socket overflowed', or inspect the `Send-Q` column in `ss -lt` to see the current queued connections waiting for accept().",
            "reason": "Asks how to monitor and detect queue drops using ss/netstat rather than explaining configuration mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the sysctl.conf entries to increase somaxconn to 65535 and tcp_max_syn_backlog to 32768.",
            "response": "```ini\nnet.core.somaxconn = 65535\nnet.ipv4.tcp_max_syn_backlog = 32768\n```\nApply with `sysctl -p`.",
            "reason": "Requests specific sysctl configuration lines rather than explaining queue architecture.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does TCP syncookies (net.ipv4.tcp_syncookies) mitigate SYN flood attacks when the SYN queue overflows?",
            "response": "When the SYN queue fills up, syncookies encodes connection state into the initial SYN-ACK sequence number, avoiding memory allocation in the SYN queue entirely.",
            "reason": "Asks specifically about SYN cookies attack mitigation rather than backlog queue sizing.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens when the Linux accept queue overflows: does the kernel drop the ACK or return a RST?",
            "response": "By default (`tcp_abort_on_overflow=0`), the kernel silently drops the incoming final ACK packet, prompting the client to retransmit the ACK; setting it to 1 causes the kernel to reset the connection with RST.",
            "reason": "Focuses on tcp_abort_on_overflow behavior rather than backlog queue sizing parameters.",
        },
    },

    # 149. Linux Memory Overcommit (vm.overcommit_memory)
    {
        "intent_group_id": "ig_sys_linux_memory_overcommit",
        "domain": "system_configuration",
        "subtopic": "Linux Kernel & OS Tuning",
        "task_kind": "configuration",
        "dimensions": {"operating_system": {"status": "known", "value": "linux"}},
        "canonical_query": "What are the three modes of vm.overcommit_memory in Linux, and why does Redis require vm.overcommit_memory=1?",
        "canonical_response": "Linux `vm.overcommit_memory` controls kernel memory allocation heuristics: `0` (heuristic overcommit: allows sensible overcommit, rejects obviously excessive requests), `1` (always overcommit: malloc always succeeds, used when processes fork large address spaces), and `2` (strict: total committed memory cannot exceed `swap + (RAM * overcommit_ratio)`). Redis requires `vm.overcommit_memory=1` because when creating background snapshots (`BGSAVE`), it forks a child process: without overcommit, the fork fails with out-of-memory errors because the kernel assumes the child requires a duplicate of the entire parent memory, even though copy-on-write (COW) ensures only modified pages are copied.",
        "positive_paraphrase_1": {
            "query": "Explain the settings for vm.overcommit_memory (0, 1, 2) and why Redis BGSAVE requires setting it to 1.",
            "reason": "Direct paraphrase explaining overcommit_memory modes and Redis BGSAVE fork COW behavior.",
        },
        "positive_paraphrase_2": {
            "query": "Why does Redis background snapshotting fail under copy-on-write unless Linux memory overcommit is set to 1?",
            "reason": "Syntactic variation focusing on copy-on-write, BGSAVE failure, and setting overcommit to 1.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you calculate CommitLimit and Committed_AS from /proc/meminfo when vm.overcommit_memory=2?",
            "response": "Inspect `/proc/meminfo`: `CommitLimit = SwapTotal + (MemTotal * overcommit_ratio / 100)`; allocation fails if `Committed_AS` exceeds `CommitLimit`.",
            "reason": "Asks how to calculate strict mode limits from /proc/meminfo rather than explaining the modes and Redis usage.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Show how to set vm.overcommit_memory=1 both temporarily in runtime and permanently across reboots.",
            "response": "```bash\nsysctl -w vm.overcommit_memory=1\necho 'vm.overcommit_memory = 1' >> /etc/sysctl.d/99-redis.conf\nsysctl -p /etc/sysctl.d/99-redis.conf\n```",
            "reason": "Requests shell commands to apply the setting rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Linux Out-Of-Memory (OOM) Killer select which process to kill using oom_score_adj?",
            "response": "The kernel calculates `oom_score` based on resident memory percentage; writing to `/proc/<PID>/oom_score_adj` (-1000 to +1000) adjusts priority, where -1000 prevents OOM killing.",
            "reason": "Asks about OOM killer scoring rather than memory overcommit policies.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does high copy-on-write page dirtiness during Redis BGSAVE cause unexpected RSS spikes under high write throughput?",
            "response": "If the parent process executes heavy writes while the child saves, the kernel must duplicate modified 4KB pages in physical RAM, causing the combined RSS of parent and child to swell towards 2x.",
            "reason": "Focuses on write amplification and RSS swelling during COW rather than overcommit configuration modes.",
        },
    },

    # 150. Open File Descriptor Limits (ulimit & limits.conf)
    {
        "intent_group_id": "ig_sys_file_descriptor_limits",
        "domain": "system_configuration",
        "subtopic": "Linux Kernel & OS Tuning",
        "task_kind": "configuration",
        "dimensions": {"operating_system": {"status": "known", "value": "linux"}},
        "canonical_query": "How do soft and hard limits for nofile work in /etc/security/limits.conf, and why does ulimit -n differ in systemd?",
        "canonical_response": "In Linux PAM sessions, `nofile` controls max open file descriptors: the 'soft' limit is the active limit enforced by the kernel, which an unprivileged process can raise up to the 'hard' limit; only root can raise the 'hard' limit. However, `/etc/security/limits.conf` applies strictly to PAM login sessions (SSH, terminal). Services managed by `systemd` bypass PAM limits entirely and inherit systemd defaults (often 1024 or 524288) unless explicitly overridden using `LimitNOFILE=65536` in the service unit file.",
        "positive_paraphrase_1": {
            "query": "Explain soft vs hard nofile limits in Linux and why systemd services ignore /etc/security/limits.conf.",
            "reason": "Direct paraphrase explaining soft/hard nofile limits and systemd bypassing limits.conf.",
        },
        "positive_paraphrase_2": {
            "query": "Why does setting ulimit -n in limits.conf have no effect on background systemd services and how do you configure LimitNOFILE?",
            "reason": "Syntactic variation focusing on systemd ignoring PAM limits and using LimitNOFILE.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you inspect the current soft and hard limits of an already running process using /proc/<PID>/limits?",
            "response": "Run `cat /proc/<PID>/limits | grep \"Max open files\"` to see the current soft (Current Limit) and hard (Max Limit) file descriptor ceilings for that PID.",
            "reason": "Asks how to inspect running process limits via /proc rather than explaining configuration mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a systemd drop-in override file to set LimitNOFILE=1048576 for an nginx.service unit.",
            "response": "```ini\n# /etc/systemd/system/nginx.service.d/override.conf\n[Service]\nLimitNOFILE=1048576\n```\nReload with `systemctl daemon-reload && systemctl restart nginx`.",
            "reason": "Requests specific systemd drop-in config file snippet rather than explaining the concepts.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Docker container nofile limit configuration (--ulimit nofile=...) interact with dockerd daemon defaults?",
            "response": "Docker containers inherit `default-ulimits` from `/etc/docker/daemon.json`, which can be overridden per container with `docker run --ulimit nofile=65536:65536`.",
            "reason": "Asks about Docker ulimit configuration rather than Linux PAM and systemd file limits.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between per-process nofile limits and the system-wide fs.file-max sysctl setting?",
            "response": "`nofile` restricts the maximum file descriptors a single process can allocate, while `fs.file-max` restricts the global sum of open file descriptors across all processes in the entire operating system.",
            "reason": "Contrasts per-process nofile with system-wide fs.file-max rather than explaining soft/hard PAM vs systemd limits.",
        },
    },

    # 151. Virtual Memory Swappiness (vm.swappiness)
    {
        "intent_group_id": "ig_sys_vm_swappiness_tuning",
        "domain": "system_configuration",
        "subtopic": "Linux Kernel & OS Tuning",
        "task_kind": "configuration",
        "dimensions": {"operating_system": {"status": "known", "value": "linux"}},
        "canonical_query": "How does vm.swappiness influence page cache reclamation versus anonymous memory swapping in Linux?",
        "canonical_response": "`vm.swappiness` (range 0 to 200, default 60) represents the kernel's preference balance between reclaiming file-backed page cache memory and swapping anonymous process memory to disk. High values (e.g. 100) cause the kernel to aggressively page out anonymous memory to preserve page cache for I/O; low values (e.g. 1 or 10) prioritize reclaiming unmapped file cache while avoiding swapping anonymous application memory. For database workloads (Postgres, Redis), low swappiness (1 to 10) prevents unpredictable swap I/O latency while retaining swap as an emergency safety net.",
        "positive_paraphrase_1": {
            "query": "Explain how vm.swappiness balances file page cache eviction against swapping anonymous process memory.",
            "reason": "Direct paraphrase explaining page cache eviction vs anonymous swapping ratio.",
        },
        "positive_paraphrase_2": {
            "query": "Why is vm.swappiness set to a low value (1-10) on database servers to avoid latency spikes?",
            "reason": "Syntactic variation focusing on low swappiness preventing latency spikes on database systems.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you create and activate a Linux swap file using fallocate, mkswap, and swapon?",
            "response": "Create the file with `fallocate -l 4G /swapfile`, secure it with `chmod 600 /swapfile`, format with `mkswap /swapfile`, and activate with `swapon /swapfile`.",
            "reason": "Asks how to create a swapfile on disk rather than explaining swappiness tuning mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Show how to configure vm.swappiness=1 in sysctl.d to persist across reboots.",
            "response": "```bash\necho 'vm.swappiness = 1' | sudo tee /etc/sysctl.d/99-swappiness.conf\nsudo sysctl --system\n```",
            "reason": "Requests specific shell commands to apply the setting rather than explaining the virtual memory algorithm.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does zram compressed swap in RAM differ from traditional disk-based swap partitions on Linux?",
            "response": "zram creates a block device in RAM that compresses swapped pages with LZ4/ZSTD, trading small CPU overhead for avoiding slow disk I/O.",
            "reason": "Asks about zram compressed RAM swap rather than swappiness ratio tuning.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens when vm.swappiness is set to 0 in modern Linux kernels (v3.5+)?",
            "response": "In modern kernels, `vm.swappiness=0` avoids swapping anonymous pages entirely until memory exhaustion reaches the point where OOM killer invocation is imminent, but it does not disable swap completely.",
            "reason": "Focuses on the exact edge-case semantics of swappiness=0 rather than general ratio tuning.",
        },
    },

    # 152. Inotify and Epoll Limits
    {
        "intent_group_id": "ig_sys_inotify_epoll_limits",
        "domain": "system_configuration",
        "subtopic": "Linux Kernel & OS Tuning",
        "task_kind": "configuration",
        "dimensions": {"operating_system": {"status": "known", "value": "linux"}},
        "canonical_query": "What causes 'ENOSPC: System limit for number of file watchers reached' and how does fs.inotify.max_user_watches fix it?",
        "canonical_response": "The Linux `inotify` subsystem allows applications (IDE file watchers, Vite/Webpack dev servers, Prometheus file discovery) to register watches on directories and files. The kernel caps the total number of watches allowed per real user ID via `fs.inotify.max_user_watches` (often defaulting to 8192). When watching large source trees or `node_modules`, this limit is easily exhausted, returning `ENOSPC`. Increasing `fs.inotify.max_user_watches` (e.g. to 524288) allocates kernel memory (approx 1 KB per watch) and resolves the exhaustion.",
        "positive_paraphrase_1": {
            "query": "Explain how increasing fs.inotify.max_user_watches resolves file watcher ENOSPC errors on Linux.",
            "reason": "Direct paraphrase explaining inotify watch exhaustion and max_user_watches tuning.",
        },
        "positive_paraphrase_2": {
            "query": "Why do web development dev servers run out of inotify file watchers and how is the limit raised?",
            "reason": "Syntactic variation focusing on dev server watch exhaustion and raising the limit.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you count which processes are consuming the most inotify watches using /proc/*/fd/*?",
            "response": "Run `find /proc/*/fd -lname anon_inode:inotify 2>/dev/null | cut -d/ -f3 | xargs -I {} grep -s '^watches:' /proc/{}/fdinfo/*` to aggregate consumed watches per process.",
            "reason": "Asks how to inspect consumption per process rather than explaining the limit and configuration fix.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the command to set fs.inotify.max_user_watches=524288 persistently in sysctl.conf.",
            "response": "```bash\necho \"fs.inotify.max_user_watches=524288\" | sudo tee -a /etc/sysctl.conf && sudo sysctl -p\n```",
            "reason": "Requests shell commands rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does epoll's max_user_watches limit in /proc/sys/fs/epoll/max_user_watches differ from inotify?",
            "response": "epoll's `max_user_watches` limits the total number of file descriptors registered across all epoll instances per user, preventing epoll table exhaustion in network servers.",
            "reason": "Asks about epoll max_user_watches rather than inotify file system watching.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between fs.inotify.max_user_instances and fs.inotify.max_user_watches?",
            "response": "`max_user_instances` limits the number of distinct inotify file descriptor queues (`inotify_init()`) a user can open, while `max_user_watches` limits the total number of files and directories watched across all instances.",
            "reason": "Contrasts inotify instances vs watches rather than explaining watch limit exhaustion.",
        },
    },

    # 153. Transparent Huge Pages (THP)
    {
        "intent_group_id": "ig_sys_transparent_huge_pages_db",
        "domain": "system_configuration",
        "subtopic": "Linux Kernel & OS Tuning",
        "task_kind": "configuration",
        "dimensions": {"database": {"status": "known", "value": "postgres"}},
        "canonical_query": "Why do databases like PostgreSQL and Redis recommend disabling Transparent Huge Pages (THP) on Linux?",
        "canonical_response": "Standard memory pages are 4 KB, while Transparent Huge Pages (THP) are 2 MB. While THP reduces TLB cache misses for contiguous memory workloads, database engines perform sparse, random memory accesses. With THP enabled, modifying a single 8-byte record forces the Linux kernel to allocate or copy an entire 2 MB page, causing severe memory bloat and write amplification. Furthermore, the background `khugepaged` daemon locks memory regions during page compaction, introducing unpredictable multi-millisecond latency spikes and jitter.",
        "positive_paraphrase_1": {
            "query": "Explain why Redis and PostgreSQL suffer latency spikes and memory bloat from Transparent Huge Pages (THP).",
            "reason": "Direct paraphrase explaining THP memory bloat, compaction latency, and database recommendations.",
        },
        "positive_paraphrase_2": {
            "query": "What mechanisms cause Transparent Huge Pages to degrade performance in transactional and caching databases?",
            "reason": "Syntactic variation focusing on THP degradation mechanisms in transactional workloads.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you disable Transparent Huge Pages using GRUB kernel boot parameters in /etc/default/grub?",
            "response": "Append `transparent_hugepage=never` to `GRUB_CMDLINE_LINUX_DEFAULT`, run `update-grub`, and reboot the machine.",
            "reason": "Asks for GRUB boot parameter configuration commands rather than explaining architectural drawbacks.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a systemd service unit that disables THP at boot time by writing never to sysfs.",
            "response": "```ini\n[Service]\nType=oneshot\nExecStart=/bin/sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled'\n```",
            "reason": "Requests systemd service implementation rather than explaining why databases disable THP.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Explicit Huge Pages (hugetlbfs) in PostgreSQL differ from Transparent Huge Pages (THP)?",
            "response": "Explicit Huge Pages are pre-allocated statically at boot time via `vm.nr_hugepages` and pinned in RAM, avoiding `khugepaged` runtime compaction overhead entirely.",
            "reason": "Contrasts explicit HugeTLB with THP rather than explaining why THP degrades performance.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does setting THP to madvise instead of never allow specific applications to opt-in via madvise() syscalls?",
            "response": "Setting `enabled=madvise` disables automatic system-wide THP promotion while permitting applications that explicitly call `madvise(MADV_HUGEPAGE)` to use 2MB pages where beneficial.",
            "reason": "Focuses on madvise opt-in semantics rather than why databases disable THP.",
        },
    },

    # 154. Docker Multi-Stage Builds
    {
        "intent_group_id": "ig_sys_docker_multistage_builds",
        "domain": "system_configuration",
        "subtopic": "Docker & Containerization",
        "task_kind": "configuration",
        "dimensions": {"container": {"status": "known", "value": "docker"}},
        "canonical_query": "How do Docker multi-stage builds reduce production container image size and eliminate build tool vulnerabilities?",
        "canonical_response": "Multi-stage builds use multiple `FROM` instructions in a single Dockerfile. The first stage (builder) includes heavy compilers, SDKs, package managers, and header files to build the application binary or bundle. Subsequent stages (such as a minimal alpine or distroless base) copy only the compiled artifacts from the builder using `COPY --from=builder /app/binary /binary`. This strips build-time dependencies, compiler caches, and development tooling, shrinking image sizes from hundreds of megabytes down to megabytes and drastically reducing the CVE attack surface.",
        "positive_paraphrase_1": {
            "query": "Explain how Docker multi-stage builds isolate build dependencies to create minimal, secure runtime images.",
            "reason": "Direct paraphrase explaining multi-stage builds stripping build tools and shrinking runtime images.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does COPY --from in Dockerfiles prevent compilers and build tools from shipping into production images?",
            "reason": "Syntactic variation focusing on COPY --from and excluding compilers from production.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you target a specific stage in a multi-stage Docker build using docker build --target?",
            "response": "Run `docker build --target builder -t my-app:dev .` to build and stop execution at the named stage for testing or development debugging.",
            "reason": "Asks about CLI flag --target rather than explaining size and security benefits of multi-stage builds.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a multi-stage Dockerfile for a Go web application building a static binary copied into a scratch base.",
            "response": "```dockerfile\nFROM golang:1.22-alpine AS builder\nWORKDIR /app\nCOPY . .\nRUN CGO_ENABLED=0 go build -o main .\nFROM scratch\nCOPY --from=builder /app/main /main\nENTRYPOINT [\"/main\"]\n```",
            "reason": "Requests specific Go multi-stage Dockerfile code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Google's distroless container images differ from Alpine Linux images for production runtimes?",
            "response": "Distroless contains only your application and runtime dependencies without package managers or shells, whereas Alpine includes BusyBox shell and apk package manager.",
            "reason": "Compares Distroless vs Alpine base images rather than explaining multi-stage build mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does ordering Dockerfile RUN instructions from least-frequently changed to most-frequently changed optimize layer caching?",
            "response": "Docker invalidates cache for subsequent layers once a layer changes; placing unchanging OS dependencies before application code ensures cached layers are reused across builds.",
            "reason": "Focuses on layer cache ordering rules rather than multi-stage build image stripping.",
        },
    },

    # 155. Docker Non-Root User Security
    {
        "intent_group_id": "ig_sys_docker_non_root_user",
        "domain": "system_configuration",
        "subtopic": "Docker & Containerization",
        "task_kind": "configuration",
        "dimensions": {"container": {"status": "known", "value": "docker"}},
        "canonical_query": "Why is running Docker containers as a non-root user critical for security and how is it configured in Dockerfiles?",
        "canonical_response": "By default, containers execute as `root` (UID 0), which matches root on the host kernel. If a container breakout or privilege escalation vulnerability occurs, a root process inside the container possesses full root capabilities on the host filesystem and kernel. It is configured in the Dockerfile by creating an unprivileged user and group (`RUN groupadd -r app && useradd -r -g app app`) and switching execution with `USER app:app` before the `ENTRYPOINT`/`CMD`. Runtime enforcement is also applied with `docker run --user 1000:1000` or Kubernetes `runAsNonRoot: true`.",
        "positive_paraphrase_1": {
            "query": "Explain the security risks of running containers as root and how to specify USER directives in Dockerfiles.",
            "reason": "Direct paraphrase explaining root security hazards and specifying non-root USER directives.",
        },
        "positive_paraphrase_2": {
            "query": "How does switching to an unprivileged UID in Docker prevent container breakouts from gaining host root access?",
            "reason": "Syntactic variation focusing on unprivileged UIDs mitigating container breakout attacks.",
        },
        "hard_negative_diff_operation": {
            "query": "How do Linux user namespaces (userns-remap) in Docker daemon remap container root to an unprivileged host UID?",
            "response": "Configuring `\"userns-remap\": \"default\"` in `/etc/docker/daemon.json` maps container UID 0 to an unprivileged host UID range (e.g. 100000+), neutralizing container root escapes.",
            "reason": "Asks about userns-remap daemon configuration rather than Dockerfile USER directives and container security.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Dockerfile commands to create a user 'appuser' with UID 1001 and set appropriate directory permissions.",
            "response": "```dockerfile\nRUN addgroup -g 1001 appgroup && adduser -u 1001 -G appgroup -s /bin/sh -D appuser \\\n    && chown -R appuser:appgroup /app\nUSER appuser\n```",
            "reason": "Requests specific Dockerfile shell commands rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Kubernetes securityContext enforce runAsNonRoot and readOnlyRootFilesystem in pod manifests?",
            "response": "In the pod spec, set `securityContext.runAsNonRoot: true` to prevent containers from starting as UID 0, and `readOnlyRootFilesystem: true` to prevent disk modifications.",
            "reason": "Asks about Kubernetes securityContext pod manifest fields rather than Dockerfile non-root configuration.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can an unprivileged non-root container user not bind to ports below 1024 without net_bind_service capabilities?",
            "response": "Linux classifies ports below 1024 as privileged; unprivileged processes cannot bind to them unless granted the `CAP_NET_BIND_SERVICE` Linux capability or lowering `ip_unprivileged_port_start`.",
            "reason": "Explains privileged port binding limitations rather than root container breakout risks.",
        },
    },

    # 156. Docker Build Layer Caching
    {
        "intent_group_id": "ig_sys_docker_layer_caching",
        "domain": "system_configuration",
        "subtopic": "Docker & Containerization",
        "task_kind": "configuration",
        "dimensions": {"container": {"status": "known", "value": "docker"}},
        "canonical_query": "How does Docker layer caching work and why should dependency files (package.json / requirements.txt) be copied before application code?",
        "canonical_response": "Docker builds images as a series of read-only layers corresponding to Dockerfile instructions (`RUN`, `COPY`, `ADD`). During a build, Docker checks if it can reuse a cached layer: for `COPY`, it hashes file contents; for `RUN`, it matches the exact command string. If any layer changes, all subsequent layers are invalidated. Copying dependency files first (`COPY package.json .`) and installing packages (`RUN npm install`) before copying the application source (`COPY . .`) ensures expensive dependency downloads remain cached whenever developers modify application code.",
        "positive_paraphrase_1": {
            "query": "Explain how Docker build cache invalidation works and why copying package files before source code speeds up builds.",
            "reason": "Direct paraphrase explaining Docker layer cache invalidation and separating dependency copies.",
        },
        "positive_paraphrase_2": {
            "query": "In Dockerfiles, why does copying requirements.txt before project source files prevent re-running pip install?",
            "reason": "Syntactic variation focusing on caching dependency installation steps before copying project code.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure Docker BuildKit cache mounts (RUN --mount=type=cache) for npm or pip?",
            "response": "Use `RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt` to persist the package manager's cache directory across builds without creating extra image layers.",
            "reason": "Asks how to use BuildKit cache mounts rather than explaining standard layer cache invalidation rules.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide a Node.js Dockerfile snippet demonstrating optimal layer caching for npm install.",
            "response": "```dockerfile\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci --only=production\nCOPY . .\nCMD [\"node\", \"server.js\"]\n```",
            "reason": "Requests specific Dockerfile snippet rather than explaining how the cache invalidation algorithm works.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does GitHub Actions cache Docker layers across CI workflow runs using cache-from and cache-to?",
            "response": "Use `docker/build-push-action` with `cache-from: type=gha` and `cache-to: type=gha,mode=max` to store layer caches in GitHub Actions cache storage.",
            "reason": "Asks about GitHub Actions CI remote cache configuration rather than Dockerfile layer mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does chaining commands with '&&' in a single RUN instruction reduce image layer count?",
            "response": "Each instruction produces a new filesystem layer; chaining cleanup (`apt-get install ... && rm -rf /var/lib/apt/lists/*`) in one RUN ensures temporary files are not preserved in intermediate layers.",
            "reason": "Focuses on RUN command chaining and cleanup rather than cache invalidation ordering.",
        },
    },

    # 157. Cgroup v2 Resource Limits
    {
        "intent_group_id": "ig_sys_cgroup_v2_resource_limits",
        "domain": "system_configuration",
        "subtopic": "Docker & Containerization",
        "task_kind": "configuration",
        "dimensions": {"container": {"status": "known", "value": "cgroups"}},
        "canonical_query": "How do Linux cgroups v2 control CPU CFS quotas and memory limits for container runtimes?",
        "canonical_response": "Cgroups v2 provides a unified hierarchy for resource distribution. For CPU, `cpu.max` specifies two numbers: `$MAX $PERIOD` (e.g. `200000 100000` allows 2 CPU cores per 100 ms period); if a container exceeds this CFS quota, its threads are throttled until the next period. For memory, `memory.max` sets the hard memory limit (triggering OOM kill if exceeded and un-reclaimable), `memory.high` sets a soft throttle boundary where processes are slowed down to reclaim page cache, and `memory.current` reflects live usage.",
        "positive_paraphrase_1": {
            "query": "Explain how cgroups v2 implements CPU quota throttling (cpu.max) and hard memory limits (memory.max).",
            "reason": "Direct paraphrase explaining cgroup v2 cpu.max throttling and memory.max enforcement.",
        },
        "positive_paraphrase_2": {
            "query": "What kernel mechanisms in cgroups v2 enforce CPU periods and trigger OOM killer events for containers?",
            "reason": "Syntactic variation focusing on CFS periods, throttling, and OOM killer events in cgroups v2.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you inspect live cgroup v2 resource usage directly from /sys/fs/cgroup/ on a Linux host?",
            "response": "Inspect files in `/sys/fs/cgroup/system.slice/...`: `cat cpu.stat` to see throttled time, and `cat memory.current` to see resident memory consumption in bytes.",
            "reason": "Asks how to inspect sysfs files manually rather than explaining how the cgroup controller limits work.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a docker run command that limits a container to 1.5 CPU cores and 512MB RAM with no swap.",
            "response": "```bash\ndocker run --cpus=\"1.5\" --memory=\"512m\" --memory-swap=\"512m\" -d my-image\n```",
            "reason": "Requests specific docker CLI flags rather than explaining cgroup v2 kernel controllers.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do cgroups v1 differs from cgroups v2 regarding multi-hierarchy versus unified hierarchy?",
            "response": "In cgroups v1, each controller (cpu, memory, blkio) lived in its own independent directory hierarchy, causing coordination conflicts; cgroups v2 unifies all controllers into a single nested process tree.",
            "reason": "Compares cgroup v1 vs v2 hierarchy design rather than explaining CPU and memory control mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between CPU throttling (exceeding CFS quota) and CPU starvation (insufficient CPU shares)?",
            "response": "Throttling occurs when a process uses up its allocated time in a CFS period regardless of whether other cores are idle. Starvation occurs when competing high-priority processes monopolize CPU capacity under high load.",
            "reason": "Contrasts CFS quota throttling with share-based starvation rather than explaining cgroup v2 limits.",
        },
    },

    # 158. Docker Container Healthchecks & Restart Policies
    {
        "intent_group_id": "ig_sys_docker_healthchecks_restarts",
        "domain": "system_configuration",
        "subtopic": "Docker & Containerization",
        "task_kind": "configuration",
        "dimensions": {"container": {"status": "known", "value": "docker"}},
        "canonical_query": "How do Docker HEALTHCHECK instructions interact with container restart policies and service orchestrators?",
        "canonical_response": "A Dockerfile `HEALTHCHECK` periodically runs a command inside the container (e.g. `curl -f http://localhost:8080/health || exit 1`) with parameters `--interval`, `--timeout`, `--start-period`, and `--retries`. If the command exits non-zero consecutive times matching retries, the container status transitions from `healthy` to `unhealthy`. Crucially, standard Docker engine restart policies (`restart: always` or `unless-stopped`) only restart containers upon process exit, NOT when becoming unhealthy; orchestrators like Docker Swarm or Kubernetes are required to kill and replace unhealthy containers.",
        "positive_paraphrase_1": {
            "query": "Explain how HEALTHCHECK commands work and why Docker does not automatically restart unhealthy containers by default.",
            "reason": "Direct paraphrase explaining HEALTHCHECK transitions and Docker's lack of automatic restart on unhealthy status.",
        },
        "positive_paraphrase_2": {
            "query": "What is the difference between container process termination and healthcheck status transitions in Docker?",
            "reason": "Syntactic variation focusing on process exit vs healthcheck state transitions.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure an autoheal third-party container to automatically restart unhealthy Docker containers?",
            "response": "Deploy the `willfarrell/autoheal` container with access to `/var/run/docker.sock` and label target containers with `autoheal=true`.",
            "reason": "Asks how to use a specific third-party autoheal container rather than explaining native Docker healthcheck mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Dockerfile HEALTHCHECK instruction that polls an HTTP endpoint every 30 seconds with a 3 second timeout.",
            "response": "```dockerfile\nHEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \\\n  CMD curl -f http://localhost:8080/health || exit 1\n```",
            "reason": "Requests specific Dockerfile syntax rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Kubernetes liveness probes differ from readiness probes regarding pod restarts and traffic routing?",
            "response": "Liveness probes kill and restart the container when they fail; readiness probes temporarily remove the pod from Service endpoints without restarting it.",
            "reason": "Asks about Kubernetes liveness/readiness probes rather than Docker HEALTHCHECK behavior.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between Docker restart: on-failure and restart: always?",
            "response": "`on-failure` only restarts the container if the main process exits with a non-zero exit code, whereas `always` restarts the container regardless of exit code or system reboot.",
            "reason": "Contrasts on-failure vs always restart policies rather than explaining HEALTHCHECK interaction.",
        },
    },

    # 159. Systemd Restart Policies
    {
        "intent_group_id": "ig_sys_systemd_restart_policies",
        "domain": "system_configuration",
        "subtopic": "Systemd & Service Management",
        "task_kind": "configuration",
        "dimensions": {"service_manager": {"status": "known", "value": "systemd"}},
        "canonical_query": "How do Restart=always, RestartSec, and StartLimitBurst prevent flapping crash loops in systemd services?",
        "canonical_response": "In a systemd unit file, `Restart=always` instructs systemd to restart the service whenever it terminates, whether cleanly (exit code 0) or due to failure/crashes. `RestartSec=5s` introduces a delay between consecutive restart attempts to avoid hammering system resources. To prevent infinite crash loops ('flapping'), systemd enforces rate limiting using `StartLimitIntervalSec=60s` and `StartLimitBurst=5` (configured in `[Unit]`): if the service restarts more than 5 times within a 60-second window, systemd halts restarts and places the unit in a `failed` state.",
        "positive_paraphrase_1": {
            "query": "Explain how systemd rate-limits service restarts using RestartSec, StartLimitIntervalSec, and StartLimitBurst.",
            "reason": "Direct paraphrase explaining systemd restart rate limiting and crash loop prevention.",
        },
        "positive_paraphrase_2": {
            "query": "Why does systemd mark a service as failed when it restarts too frequently, and how do burst limits configure this?",
            "reason": "Syntactic variation focusing on systemd failing flapping services and configuring burst thresholds.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you reset a failed systemd service state using systemctl reset-failed?",
            "response": "Run `systemctl reset-failed <service_name>` to clear the failed state and reset the restart burst rate counter, allowing `systemctl start` to execute again.",
            "reason": "Asks for the command to reset a failed unit rather than explaining restart configuration parameters.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete systemd service file [Service] section configured with Restart=on-failure and RestartSec=3s.",
            "response": "```ini\n[Service]\nExecStart=/usr/local/bin/app\nRestart=on-failure\nRestartSec=3s\nStartLimitIntervalSec=30s\nStartLimitBurst=3\n```",
            "reason": "Requests specific systemd unit configuration code rather than explaining how the parameters work.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does PM2 restart crashed Node.js processes using --max-restarts and --restart-delay?",
            "response": "PM2 monitors the child process PID and triggers automatic restarts, pausing for `--restart-delay` ms and stopping if `--max-restarts` is exceeded within a time window.",
            "reason": "Asks about PM2 process manager restart policies rather than systemd.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between Restart=always and Restart=on-failure in systemd?",
            "response": "`Restart=always` restarts the service on any exit (including clean exit code 0 or SIGTERM), whereas `Restart=on-failure` only restarts if the process exits with non-zero status or dies from an unhandled signal.",
            "reason": "Contrasts Restart=always with Restart=on-failure rather than explaining crash loop rate-limiting.",
        },
    },

    # 160. Systemd Service Types (simple vs notify vs forking)
    {
        "intent_group_id": "ig_sys_systemd_service_types",
        "domain": "system_configuration",
        "subtopic": "Systemd & Service Management",
        "task_kind": "configuration",
        "dimensions": {"service_manager": {"status": "known", "value": "systemd"}},
        "canonical_query": "What is the difference between Type=simple, Type=notify, and Type=forking in systemd service definitions?",
        "canonical_response": "`Type=simple` (default) assumes the service is immediately up and ready as soon as `ExecStart` is forked; dependent units start concurrently without waiting for initialization. `Type=forking` expects the binary to call `fork()` and background itself; systemd considers the service started once the parent process exits and tracks the child PID (via `PIDFile=`). `Type=notify` is the most robust: the process remains in the foreground and explicitly sends `READY=1` via a Unix domain socket (`sd_notify()`) once initialization is complete; systemd waits for this signal before starting dependent units.",
        "positive_paraphrase_1": {
            "query": "Explain how Type=simple, Type=notify, and Type=forking notify systemd of service readiness.",
            "reason": "Direct paraphrase explaining readiness notification across simple, notify, and forking systemd types.",
        },
        "positive_paraphrase_2": {
            "query": "Why is Type=notify preferred over Type=simple when dependent systemd services require initialized sockets?",
            "reason": "Syntactic variation focusing on Type=notify synchronizing dependent service startups.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you send a READY=1 notification from a Python script to systemd using systemd-python or sd_notify?",
            "response": "Import `systemd.daemon` and call `systemd.daemon.notify('READY=1')`, or write to the socket specified in the `$NOTIFY_SOCKET` environment variable.",
            "reason": "Asks how to write Python code calling sd_notify rather than comparing systemd unit types.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a systemd unit file using Type=notify and WatchdogSec=30s for a daemon.",
            "response": "```ini\n[Service]\nType=notify\nExecStart=/usr/bin/daemon\nWatchdogSec=30s\nRestart=always\n```",
            "reason": "Requests specific systemd unit configuration file rather than explaining the three service types.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do launchd daemons on macOS handle startup notification compared to systemd Type=notify?",
            "response": "macOS launchd manages sockets directly through `launchd.plist` socket activation, passing pre-created file descriptors to daemons on launch.",
            "reason": "Asks about macOS launchd socket activation rather than systemd service types.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is Type=oneshot in systemd and how does RemainAfterExit=yes affect it?",
            "response": "`Type=oneshot` is designed for short-lived tasks that execute and exit; setting `RemainAfterExit=yes` keeps the unit in active (exited) state so dependent services can still run.",
            "reason": "Focuses on Type=oneshot scripts rather than simple vs notify vs forking daemons.",
        },
    },

    # 161. Systemd Journald Log Retention
    {
        "intent_group_id": "ig_sys_journald_retention_tuning",
        "domain": "system_configuration",
        "subtopic": "Systemd & Service Management",
        "task_kind": "configuration",
        "dimensions": {"service_manager": {"status": "known", "value": "systemd"}},
        "canonical_query": "How do you configure systemd-journald disk space limits and log retention in /etc/systemd/journald.conf?",
        "canonical_response": "In `/etc/systemd/journald.conf`, disk limits are configured under the `[Journal]` section: `SystemMaxUse=4G` caps the total disk space journal files can consume on persistent storage; `SystemKeepFree=2G` guarantees minimum free disk space remaining on the filesystem; `SystemMaxFileSize=500M` specifies the maximum size of individual journal files before rotation; and `MaxRetentionSec=1month` automatically purges log files older than the specified duration. After modifying the configuration, apply changes by restarting the daemon: `systemctl restart systemd-journald`.",
        "positive_paraphrase_1": {
            "query": "Explain how to set SystemMaxUse, SystemKeepFree, and MaxRetentionSec in journald.conf to control log disk consumption.",
            "reason": "Direct paraphrase explaining journald.conf disk space and retention settings.",
        },
        "positive_paraphrase_2": {
            "query": "How can you prevent systemd journal logs from filling up the root filesystem using journald configuration?",
            "reason": "Syntactic variation focusing on preventing disk exhaustion via journald configuration.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you immediately clean up journal logs on a running server using journalctl --vacuum-size or --vacuum-time?",
            "response": "Run `journalctl --vacuum-size=1G` to immediately trim logs down to 1 GB, or `journalctl --vacuum-time=7d` to delete entries older than 7 days.",
            "reason": "Asks how to manually vacuum logs via CLI rather than configuring persistent limits in journald.conf.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the /etc/systemd/journald.conf configuration block to limit log storage to 2GB with 14-day retention.",
            "response": "```ini\n[Journal]\nStorage=persistent\nSystemMaxUse=2G\nSystemMaxFileSize=200M\nMaxRetentionSec=14d\n```",
            "reason": "Requests specific config file lines rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does logrotate manage log compression and rotation for classic syslog /var/log files?",
            "response": "`logrotate` runs via cron, renaming log files (e.g. `app.log.1`), compressing with gzip, and signaling the daemon via SIGHUP to reopen its log descriptor.",
            "reason": "Asks about classic logrotate daemon rather than systemd-journald.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between Storage=persistent and Storage=volatile in journald.conf?",
            "response": "`Storage=persistent` stores binary journals in `/var/log/journal/` across reboots, whereas `Storage=volatile` stores logs exclusively in memory in `/run/log/journal/`, losing logs on reboot.",
            "reason": "Contrasts persistent vs volatile storage rather than explaining disk size limits and retention.",
        },
    },

    # 162. Systemd Resource Control (CPUWeight & MemoryMax)
    {
        "intent_group_id": "ig_sys_systemd_resource_slices",
        "domain": "system_configuration",
        "subtopic": "Systemd & Service Management",
        "task_kind": "configuration",
        "dimensions": {"service_manager": {"status": "known", "value": "systemd"}},
        "canonical_query": "How do CPUWeight, CPUQuota, and MemoryMax in systemd unit files translate to kernel cgroup limits?",
        "canonical_response": "Systemd acts as the primary user-space manager for Linux cgroups v2. In a unit's `[Service]` section: `CPUWeight=100` translates to `cpu.weight` (proportional CPU share from 1 to 10000 under contention); `CPUQuota=150%` translates to `cpu.max` (enforcing CFS bandwidth throttling, where 150% caps the service to 1.5 CPU cores per period); and `MemoryMax=2G` translates directly to `memory.max` (the hard cgroup memory ceiling, killing the service via OOM if exceeded).",
        "positive_paraphrase_1": {
            "query": "Explain how systemd maps CPUQuota, CPUWeight, and MemoryMax directives into underlying cgroup v2 controllers.",
            "reason": "Direct paraphrase explaining how systemd directives map into cgroup v2 controllers.",
        },
        "positive_paraphrase_2": {
            "query": "What is the effect of setting CPUQuota and MemoryMax in a systemd service file on kernel resource throttling?",
            "reason": "Syntactic variation focusing on CPUQuota and MemoryMax effects on kernel throttling and OOM.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you dynamically set CPUQuota or MemoryMax on a running systemd service using systemctl set-property?",
            "response": "Run `systemctl set-property <service_name> CPUQuota=50% MemoryMax=1G` to apply resource limits immediately and write transient drop-in files.",
            "reason": "Asks how to use the systemctl set-property CLI command rather than explaining unit file translation to cgroups.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a systemd slice file (/etc/systemd/system/customer.slice) allocating 40% CPU and 4GB memory.",
            "response": "```ini\n[Unit]\nDescription=Customer Slice\n[Slice]\nCPUQuota=40%\nMemoryMax=4G\n```",
            "reason": "Requests specific systemd slice configuration file rather than explaining directive mappings.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Kubernetes Pod resource requests and limits correspond to container runtime cgroup settings?",
            "response": "Kubernetes `cpu.requests` maps to cgroup `cpu.weight`/shares; `cpu.limits` maps to `cpu.max`/CFS quota; `memory.limits` maps to `memory.max`.",
            "reason": "Asks about Kubernetes Pod requests and limits rather than native systemd unit configuration.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between MemoryHigh and MemoryMax in systemd resource control?",
            "response": "`MemoryMax` is the absolute hard limit that triggers the kernel OOM killer when exceeded; `MemoryHigh` is a soft throttle boundary where processes are slowed down and page cache is aggressively reclaimed.",
            "reason": "Contrasts MemoryHigh with MemoryMax rather than explaining CPU/Memory mappings to cgroups.",
        },
    },

    # 163. Supervisord vs Systemd in Containers
    {
        "intent_group_id": "ig_sys_supervisord_vs_systemd_containers",
        "domain": "system_configuration",
        "subtopic": "Systemd & Service Management",
        "task_kind": "explanation",
        "dimensions": {"service_manager": {"status": "known", "value": "supervisord"}},
        "canonical_query": "What is the architectural difference between using Supervisord versus Systemd for process supervision in Linux containers?",
        "canonical_response": "Systemd is designed as a complete OS init system (PID 1) requiring access to cgroups, dbus, and mounted sysfs filesystems, making it heavy and problematic inside standard Docker containers unless running in privileged mode. Supervisord is a lightweight, userspace Python-based process supervisor designed specifically to run multiple foreground/background processes under a single PID 1 in unprivileged containers. It handles process lifecycle, logging to stdout, and restarts without needing kernel cgroup integration or systemd IPC.",
        "positive_paraphrase_1": {
            "query": "Compare Supervisord and Systemd as PID 1 process supervisors inside Docker containers.",
            "reason": "Direct paraphrase comparing Supervisord and Systemd inside container environments.",
        },
        "positive_paraphrase_2": {
            "query": "Why is Supervisord preferred over Systemd when running multiple processes in an unprivileged Docker container?",
            "reason": "Syntactic variation focusing on Supervisord running unprivileged in Docker compared to Systemd.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure a supervisord.conf file with [program:web] to supervise a Gunicorn application?",
            "response": "Add `[program:web]` with `command=gunicorn app:main`, `autostart=true`, `autorestart=true`, and `stdout_logfile=/dev/stdout` in `supervisord.conf`.",
            "reason": "Asks how to write a supervisord.conf file rather than comparing Supervisord and Systemd architecture.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Dockerfile that installs Supervisord and sets it as the ENTRYPOINT to launch two services.",
            "response": "```dockerfile\nRUN apt-get update && apt-get install -y supervisor\nCOPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf\nCMD [\"/usr/bin/supervisord\", \"-n\"]\n```",
            "reason": "Requests specific Dockerfile code rather than explaining architectural differences.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does tini or dumb-init serve as a lightweight PID 1 init system in containers compared to Supervisord?",
            "response": "Tini is a minimal C binary that acts as PID 1 solely to reap zombie child processes and forward OS signals (SIGTERM), without managing multiple sub-processes.",
            "reason": "Compares tini/dumb-init with Supervisord rather than comparing Supervisord with Systemd.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does running one process per container follow container best practices better than multi-process supervisors?",
            "response": "Single-process containers decouple application lifecycles, simplify horizontal autoscaling, isolate failure domains, and leverage native orchestrator healthchecks and logging.",
            "reason": "Discusses single-process container philosophy rather than comparing supervisor implementations.",
        },
    },

    # 164. Nginx Reverse Proxy Buffering
    {
        "intent_group_id": "ig_sys_nginx_proxy_buffering",
        "domain": "system_configuration",
        "subtopic": "Nginx & Web Server Tuning",
        "task_kind": "configuration",
        "dimensions": {"web_server": {"status": "known", "value": "nginx"}},
        "canonical_query": "How does proxy_buffering on versus off in Nginx affect memory, disk temp files, and streaming responses?",
        "canonical_response": "When `proxy_buffering on` (default), Nginx reads the entire response from the upstream application as quickly as possible into memory buffers (`proxy_buffers`). If the response exceeds buffer size, Nginx spills the excess to temporary disk files (`proxy_temp_path`). This frees upstream workers immediately so they can process new requests while Nginx slowly streams the payload to slow clients. When `proxy_buffering off`, Nginx reads directly from upstream to client synchronously: this is essential for Server-Sent Events (SSE) and streaming LLM tokens to prevent buffering delays.",
        "positive_paraphrase_1": {
            "query": "Explain how Nginx proxy_buffering works and why it must be disabled for real-time streaming responses like SSE.",
            "reason": "Direct paraphrase explaining proxy_buffering mechanics and disabling it for SSE/streaming.",
        },
        "positive_paraphrase_2": {
            "query": "What are the trade-offs of Nginx proxy_buffering regarding upstream worker release versus disk temporary file I/O?",
            "reason": "Syntactic variation focusing on worker release vs disk temp file trade-offs in proxy buffering.",
        },
        "hard_negative_diff_operation": {
            "query": "How can an upstream application selectively disable Nginx proxy buffering using the X-Accel-Buffering header?",
            "response": "The upstream app emits `X-Accel-Buffering: no` in its HTTP response headers; Nginx intercepts this header and disables buffering for that specific response.",
            "reason": "Asks how upstream headers (X-Accel-Buffering) override buffering rather than explaining buffering configuration directives.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the Nginx location block configuration to disable buffering for an AI streaming /v1/chat/completions route.",
            "response": "```nginx\nlocation /v1/chat/completions {\n    proxy_pass http://backend;\n    proxy_buffering off;\n    proxy_cache off;\n    proxy_set_header Connection '';\n    proxy_http_version 1.1;\n}\n```",
            "reason": "Requests specific Nginx configuration block rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Envoy proxy handle response buffering and streaming with the buffer filter?",
            "response": "Envoy streams responses by default unless the `envoy.filters.http.buffer` filter is explicitly configured with `max_request_bytes` to buffer payloads.",
            "reason": "Asks about Envoy proxy buffer filter instead of Nginx proxy buffering.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the purpose of proxy_busy_buffers_size in Nginx when proxy buffering is enabled?",
            "response": "`proxy_busy_buffers_size` limits the total size of memory buffers that can be actively sending data to the client while the rest of the response is still being read from upstream.",
            "reason": "Focuses on the specific tuning parameter proxy_busy_buffers_size rather than general buffering on vs off concepts.",
        },
    },

    # 165. Nginx Upstream Keepalives
    {
        "intent_group_id": "ig_sys_nginx_upstream_keepalive",
        "domain": "system_configuration",
        "subtopic": "Nginx & Web Server Tuning",
        "task_kind": "configuration",
        "dimensions": {"web_server": {"status": "known", "value": "nginx"}},
        "canonical_query": "Why does adding keepalive to an Nginx upstream block require proxy_http_version 1.1 and proxy_set_header Connection ''?",
        "canonical_response": "In an Nginx `upstream` block, `keepalive <N>` configures a cache of up to N idle keepalive TCP connections to upstream backend servers. However, Nginx defaults to HTTP/1.0 for upstream proxying, which automatically sends `Connection: close`. To actually reuse idle keepalive connections, you must explicitly set `proxy_http_version 1.1;` (since HTTP/1.1 supports persistent connections) and `proxy_set_header Connection \"\";` (to clear the default `Connection: close` header). Without these two directives, Nginx closes the connection after every single request despite the `keepalive` directive.",
        "positive_paraphrase_1": {
            "query": "Explain why Nginx upstream keepalive requires proxy_http_version 1.1 and clearing the Connection header.",
            "reason": "Direct paraphrase explaining HTTP/1.1 and Connection header requirements for Nginx upstream keepalive.",
        },
        "positive_paraphrase_2": {
            "query": "Why does Nginx fail to reuse backend connections unless HTTP/1.1 and empty Connection headers are configured?",
            "reason": "Syntactic variation focusing on HTTP/1.0 defaults preventing upstream connection reuse.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you calculate the optimal keepalive connection count in Nginx upstream blocks based on concurrency?",
            "response": "Set `keepalive` to roughly 2x the expected peak concurrent requests handled per upstream worker to ensure low latency without exhausting socket tables.",
            "reason": "Asks how to size the keepalive integer rather than explaining HTTP/1.1 header requirements.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Nginx configuration file with an upstream block and location block enabling persistent backend keepalives.",
            "response": "```nginx\nupstream backend {\n    server 127.0.0.1:8000;\n    keepalive 32;\n}\nserver {\n    location / {\n        proxy_pass http://backend;\n        proxy_http_version 1.1;\n        proxy_set_header Connection \"\";\n    }\n}\n```",
            "reason": "Requests full Nginx configuration file code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does HAProxy configure backend connection pooling with http-reuse safe versus aggressive?",
            "response": "HAProxy's `http-reuse` directive controls connection reuse safety: `safe` reuses connections only for the first request or idempotent verbs, while `aggressive` reuses idle connections immediately.",
            "reason": "Asks about HAProxy http-reuse settings rather than Nginx upstream keepalive.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between client-facing keepalive_timeout and upstream keepalive in Nginx?",
            "response": "`keepalive_timeout` controls how long Nginx keeps idle TCP connections open to web browsers/clients, while `keepalive` inside an `upstream` block controls persistent connections between Nginx and backend application servers.",
            "reason": "Contrasts client keepalive_timeout with upstream keepalive rather than explaining HTTP/1.1 header prerequisites.",
        },
    },

    # 166. Nginx Compression (Gzip & Brotli)
    {
        "intent_group_id": "ig_sys_nginx_gzip_brotli_compression",
        "domain": "system_configuration",
        "subtopic": "Nginx & Web Server Tuning",
        "task_kind": "configuration",
        "dimensions": {"web_server": {"status": "known", "value": "nginx"}},
        "canonical_query": "How does Nginx gzip and brotli static compression differ from on-the-fly dynamic compression?",
        "canonical_response": "Dynamic compression compresses responses on-the-fly in real-time as requests arrive; this consumes significant CPU cycles, especially at higher compression levels (e.g. gzip 9 or brotli 11). Static compression (`gzip_static on;` or `brotli_static on;`) instructs Nginx to look for pre-compressed `.gz` or `.br` files on disk (generated during frontend build pipelines like Vite/Webpack). If a pre-compressed file exists and the client sends matching `Accept-Encoding`, Nginx streams the pre-compressed bytes directly via zero-copy `sendfile`, achieving maximum compression ratios with zero runtime CPU overhead.",
        "positive_paraphrase_1": {
            "query": "Explain the difference between Nginx dynamic gzip/brotli compression and pre-compressed static files (gzip_static).",
            "reason": "Direct paraphrase explaining dynamic on-the-fly compression vs pre-compressed static assets.",
        },
        "positive_paraphrase_2": {
            "query": "Why does gzip_static eliminate CPU overhead while serving maximum compression ratio files in Nginx?",
            "reason": "Syntactic variation focusing on gzip_static eliminating CPU overhead via pre-compressed files.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure gzip_types in Nginx to compress JSON and SVG while excluding JPEG and PNG images?",
            "response": "Add `gzip_types text/plain text/css application/json application/javascript image/svg+xml;` to avoid wasting CPU trying to compress already-compressed binary images.",
            "reason": "Asks how to configure the gzip_types MIME list rather than contrasting dynamic vs static compression.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the Nginx server block directives to enable both dynamic Brotli and brotli_static.",
            "response": "```nginx\nbrotli on;\nbrotli_static on;\nbrotli_comp_level 6;\nbrotli_types text/plain text/css application/json application/javascript;\n```",
            "reason": "Requests specific Nginx configuration directives rather than explaining architectural differences.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Cloudflare automatic Brotli compression handle dynamic responses at the edge?",
            "response": "Cloudflare compresses origin responses using Brotli at edge nodes before streaming them to clients, utilizing dedicated edge caching and hardware acceleration.",
            "reason": "Asks about Cloudflare edge compression rather than Nginx web server configuration.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why should gzip compression levels be tuned to 5 or 6 instead of level 9 for dynamic responses?",
            "response": "Levels 5 and 6 achieve roughly 95% of the compression savings of level 9 while consuming less than half the CPU cycles, avoiding CPU exhaustion under high traffic.",
            "reason": "Focuses on CPU-to-compression ratio diminishing returns rather than dynamic vs static compression.",
        },
    },

    # 167. Nginx Rate Limiting (limit_req_zone)
    {
        "intent_group_id": "ig_sys_nginx_rate_limiting",
        "domain": "system_configuration",
        "subtopic": "Nginx & Web Server Tuning",
        "task_kind": "configuration",
        "dimensions": {"web_server": {"status": "known", "value": "nginx"}},
        "canonical_query": "How does Nginx implement leaky bucket rate limiting using limit_req_zone, burst, and nodelay?",
        "canonical_response": "Nginx uses the leaky bucket algorithm via `limit_req_zone`: `limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s` creates a 10MB shared memory zone tracking client IPs and enforcing a steady rate of 10 requests per second (1 request every 100 ms). In the location block, `limit_req zone=api burst=20 nodelay;` configures a burst buffer of 20 requests. With `nodelay`, arriving burst requests within the 20-slot allowance are processed immediately without artificial sleep delays; requests exceeding the 20-slot burst capacity are rejected immediately with `HTTP 503` (or configured status).",
        "positive_paraphrase_1": {
            "query": "Explain how the leaky bucket algorithm works in Nginx using limit_req_zone, burst, and nodelay.",
            "reason": "Direct paraphrase explaining the leaky bucket model, burst buffer, and nodelay in Nginx.",
        },
        "positive_paraphrase_2": {
            "query": "What is the behavior of Nginx limit_req with nodelay versus delayed burst processing?",
            "reason": "Syntactic variation focusing on nodelay processing bursts immediately vs introducing artificial delays.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you customize Nginx to return HTTP 429 Too Many Requests instead of HTTP 503 using limit_req_status?",
            "response": "Add `limit_req_status 429;` inside the http or server block to return standard RFC 6585 HTTP 429 status codes.",
            "reason": "Asks how to change the returned HTTP status code rather than explaining leaky bucket burst and nodelay mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Nginx configuration snippet that limits login attempts to 1 request per second with a burst of 5.",
            "response": "```nginx\nlimit_req_zone $binary_remote_addr zone=login:10m rate=1r/s;\nlocation /login {\n    limit_req zone=login burst=5 nodelay;\n    proxy_pass http://auth_svc;\n}\n```",
            "reason": "Requests specific Nginx configuration lines rather than explaining algorithm mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Redis-based token bucket rate limiting with Lua scripts compare to Nginx in-memory rate limiting?",
            "response": "Redis rate limiting maintains token buckets across a distributed multi-node fleet using atomic Lua scripts, whereas Nginx `limit_req_zone` is confined to a single host instance.",
            "reason": "Compares distributed Redis rate limiting with single-host Nginx rather than explaining Nginx parameters.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between limit_req (request rate) and limit_conn (concurrent connections) in Nginx?",
            "response": "`limit_req` limits the arrival frequency of requests over time (e.g. 10 req/s), while `limit_conn` limits the number of simultaneously active open connections allowed per IP.",
            "reason": "Contrasts limit_req with limit_conn rather than explaining burst and nodelay mechanics.",
        },
    },

    # 168. Nginx TLS Session Resumption & Ticket Caching
    {
        "intent_group_id": "ig_sys_nginx_tls_session_resumption",
        "domain": "system_configuration",
        "subtopic": "Nginx & Web Server Tuning",
        "task_kind": "configuration",
        "dimensions": {"security": {"status": "known", "value": "tls"}},
        "canonical_query": "How do TLS session IDs (ssl_session_cache) and session tickets (ssl_session_tickets) optimize HTTPS handshakes in Nginx?",
        "canonical_response": "Full TLS handshakes require 2 round-trip times (RTT) and asymmetric cryptography calculations. Session resumption allows returning clients to resume sessions in 1 RTT (or 0 RTT in TLS 1.3). Session IDs (`ssl_session_cache shared:SSL:10m;`) store session states in a shared memory pool on the server; the client presents the ID to resume. Session tickets (`ssl_session_tickets on;`) store encrypted session state on the client side encrypted with a server secret key, removing server-side memory requirements and allowing seamless resumption across load-balanced Nginx servers sharing ticket keys.",
        "positive_paraphrase_1": {
            "query": "Explain how ssl_session_cache and TLS session tickets reduce handshake latency and cryptographic overhead in Nginx.",
            "reason": "Direct paraphrase explaining session cache and ticket resumption reducing 2-RTT handshakes.",
        },
        "positive_paraphrase_2": {
            "query": "What is the difference between server-side ssl_session_cache and client-side session tickets for TLS resumption?",
            "reason": "Syntactic variation focusing on server-side memory cache vs encrypted client-side tickets.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you securely rotate TLS session ticket encryption keys (ssl_session_ticket_key) across a cluster of Nginx servers?",
            "response": "Generate random 48-byte keys, distribute them to all servers via secure configuration management, and configure primary and fallback keys in Nginx to allow smooth rotation.",
            "reason": "Asks how to rotate session ticket keys across a cluster rather than explaining how resumption works.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the Nginx SSL configuration lines to configure a 10MB shared session cache and a 1-day timeout.",
            "response": "```nginx\nssl_session_cache shared:SSL:10m;\nssl_session_timeout 1d;\nssl_session_tickets on;\n```",
            "reason": "Requests specific Nginx configuration directives rather than explaining handshake acceleration.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does TLS 1.3 0-RTT early data resumption differ from TLS 1.2 session resumption?",
            "response": "In TLS 1.3, clients can send application data in the very first flight (0-RTT) alongside the resumption handshake, though it introduces replay attack risks.",
            "reason": "Asks about TLS 1.3 0-RTT early data rather than Nginx session cache vs tickets.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does using session tickets without forward secrecy key rotation compromise past TLS session confidentiality?",
            "response": "If an attacker captures encrypted traffic and subsequently steals a static ticket key, they can decrypt all historical session tickets and recover session master secrets.",
            "reason": "Focuses on forward secrecy security risks of ticket keys rather than handshake performance optimization.",
        },
    },
]
