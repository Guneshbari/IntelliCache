"""
Domain 10: Infrastructure and Deployment Concepts (21 intent groups).
Covers Deployment Strategies & Migrations, Kubernetes Lifecycle & Autoscaling,
Observability & SRE Metrics, CI/CD, Supply Chain & Secrets.
"""

from typing import Any, Dict, List

INFRA_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 190. Blue-Green Deployment Strategy
    {
        "intent_group_id": "ig_infra_blue_green_deployment",
        "domain": "infrastructure_deployment",
        "subtopic": "Deployment Strategies & Migrations",
        "task_kind": "explanation",
        "dimensions": {"deployment": {"status": "known", "value": "blue_green"}},
        "canonical_query": "How does a Blue-Green deployment strategy achieve zero-downtime releases and instant rollbacks?",
        "canonical_response": "In a Blue-Green deployment, two identical production environments exist simultaneously: 'Blue' is the active environment serving live production traffic, while 'Green' is the idle standby environment. The new version is deployed and thoroughly validated in Green without affecting users. Once verified, the load balancer or DNS router switches traffic instantly from Blue to Green. If any critical incident occurs post-cutover, rolling back is instantaneous: the load balancer switches traffic back to Blue, which was kept running in its original state.",
        "positive_paraphrase_1": {
            "query": "Explain how Blue-Green deployments ensure zero downtime and immediate rollback via router switching.",
            "reason": "Direct paraphrase explaining Blue-Green environments, traffic cutover, and instant rollback.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does running two identical production environments allow instantaneous rollbacks during releases?",
            "reason": "Syntactic variation focusing on dual environments and instantaneous traffic rollbacks.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you automate DNS record switching for Blue-Green deployments in AWS Route 53 using weighted routing?",
            "response": "Configure Route 53 weighted resource records for Blue and Green, transitioning weights from 100/0 to 0/100 via AWS CLI or Terraform.",
            "reason": "Asks for AWS Route 53 weighted record configuration rather than explaining the architectural Blue-Green strategy.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Nginx configuration that switches traffic between blue and green upstream clusters via a toggle variable.",
            "response": "```nginx\nupstream blue { server 10.0.0.1:8080; }\nupstream green { server 10.0.0.2:8080; }\nserver {\n  set $active_upstream blue;\n  location / { proxy_pass http://$active_upstream; }\n}\n```",
            "reason": "Requests specific Nginx configuration code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Rolling Deployments in Kubernetes update pods progressively compared to Blue-Green deployments?",
            "response": "Rolling deployments replace old pods with new pods incrementally inside a single environment, avoiding the 2x infrastructure cost of Blue-Green but taking longer to roll back.",
            "reason": "Compares Rolling Deployments with Blue-Green rather than focusing on Blue-Green design.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Blue-Green deployment require database schemas to remain backward compatible across both environments simultaneously?",
            "response": "Because both Blue and Green share the same database during cutover, breaking schema changes in Green would immediately crash active Blue instances before traffic is switched.",
            "reason": "Focuses on shared database backward compatibility constraints rather than the deployment strategy itself.",
        },
    },

    # 191. Canary Releases & Progressive Traffic Shifting
    {
        "intent_group_id": "ig_infra_canary_releases_traffic_shifting",
        "domain": "infrastructure_deployment",
        "subtopic": "Deployment Strategies & Migrations",
        "task_kind": "explanation",
        "dimensions": {"deployment": {"status": "known", "value": "canary"}},
        "canonical_query": "How do canary releases and progressive traffic shifting reduce deployment blast radius in production services?",
        "canonical_response": "A canary release deploys the new software version alongside the current stable version to a tiny subset of users or traffic (e.g. 1% to 5%). Real-time telemetry (error rates, P99 latency, business conversion) is monitored for anomalies. If metrics remain healthy, an automated orchestrator (like Argo Rollouts or Flagger) progressively increases the traffic share (e.g. 10%, 25%, 50%, 100%). If errors or regressions exceed pre-configured thresholds, the canary is aborted automatically, restricting the blast radius of bugs to only the small fraction of users exposed to the canary.",
        "positive_paraphrase_1": {
            "query": "Explain how canary deployments use telemetry and automated progressive traffic shifts to minimize blast radius.",
            "reason": "Direct paraphrase explaining canary deployment, telemetry gates, and progressive traffic shifts.",
        },
        "positive_paraphrase_2": {
            "query": "Why does routing a 1% traffic canary before a full rollout protect production services from widespread outages?",
            "reason": "Syntactic variation focusing on small traffic percentages limiting failure blast radius.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure Istio VirtualServices and DestinationRules to split HTTP traffic 90/10 between v1 and v2?",
            "response": "In Istio `VirtualService`, define `route: [{destination: {subset: v1}, weight: 90}, {destination: {subset: v2}, weight: 10}]`.",
            "reason": "Asks for Istio traffic routing YAML syntax rather than explaining canary deployment principles.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Argo Rollouts AnalysisTemplate that checks Prometheus HTTP error rates during a canary step.",
            "response": "```yaml\napiVersion: argoproj.io/v1alpha1\nkind: AnalysisTemplate\nspec:\n  metrics:\n  - name: error-rate\n    provider:\n      prometheus:\n        query: sum(rate(http_errors[1m])) / sum(rate(http_requests[1m]))\n```",
            "reason": "Requests specific Argo Rollouts YAML code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does LaunchDarkly feature flagging enable targeted user percentage rollouts compared to network traffic splitting?",
            "response": "Feature flags evaluate percentage rollouts in application code based on hashed user IDs, keeping the user experience consistent across sessions regardless of network routing.",
            "reason": "Compares application feature flags with network traffic splitting rather than explaining canary deployments.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can low traffic volumes invalidate automated statistical analysis during early canary phases?",
            "response": "At 1% traffic, sample sizes may be too small to reach statistical significance, causing high variance where a single transient error can trigger an erroneous automated rollback.",
            "reason": "Analyzes statistical sample size limitations of early canaries rather than general canary architecture.",
        },
    },

    # 192. Kubernetes Rolling Updates (maxSurge & maxUnavailable)
    {
        "intent_group_id": "ig_infra_k8s_rolling_updates",
        "domain": "infrastructure_deployment",
        "subtopic": "Deployment Strategies & Migrations",
        "task_kind": "configuration",
        "dimensions": {"orchestrator": {"status": "known", "value": "kubernetes"}},
        "canonical_query": "How do maxSurge and maxUnavailable control pod replacement rate and capacity during Kubernetes rolling updates?",
        "canonical_response": "In a Kubernetes Deployment `strategy.rollingUpdate`: `maxSurge` specifies the maximum number or percentage of pods that can be created above the desired replica count during an update (e.g. `maxSurge: 25%` on 4 replicas allows spinning up 1 extra new pod before deleting old ones). `maxUnavailable` specifies the maximum number or percentage of pods that can be unavailable during the update (e.g. `maxUnavailable: 0` ensures 100% capacity is strictly maintained at all times by waiting for new pods to become `Ready` before terminating old ones).",
        "positive_paraphrase_1": {
            "query": "Explain how maxSurge and maxUnavailable regulate pod creation and deletion during Kubernetes rolling updates.",
            "reason": "Direct paraphrase explaining maxSurge and maxUnavailable in Kubernetes Deployments.",
        },
        "positive_paraphrase_2": {
            "query": "Why does setting maxUnavailable to 0 guarantee that application capacity never dips below the replica count during rollouts?",
            "reason": "Syntactic variation focusing on maxUnavailable: 0 preventing capacity drops during updates.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you pause, resume, and undo a Kubernetes deployment rollout using kubectl rollout commands?",
            "response": "Use `kubectl rollout pause deployment/app`, `kubectl rollout resume deployment/app`, and `kubectl rollout undo deployment/app --to-revision=2`.",
            "reason": "Asks for kubectl CLI commands to manage rollouts rather than explaining maxSurge and maxUnavailable configuration parameters.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the YAML snippet for a Kubernetes Deployment configuring a RollingUpdate strategy with maxSurge 1 and maxUnavailable 0.",
            "response": "```yaml\nstrategy:\n  type: RollingUpdate\n  rollingUpdate:\n    maxSurge: 1\n    maxUnavailable: 0\n```",
            "reason": "Requests specific YAML snippet rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does AWS ECS rolling update minimumHealthyPercent and maximumPercent map to Kubernetes rollout parameters?",
            "response": "In ECS, `minimumHealthyPercent=100` corresponds to `maxUnavailable=0`, and `maximumPercent=200` corresponds to `maxSurge=100%`.",
            "reason": "Compares AWS ECS parameters with Kubernetes rather than explaining Kubernetes native rolling updates.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a new pod fails readiness probes during a rolling update when maxSurge=1 and maxUnavailable=0?",
            "response": "The deployment stalls because the new pod never transitions to `Ready`; zero old pods are terminated, keeping the original application running at 100% capacity.",
            "reason": "Analyzes the exact behavior of stalled readiness probes during zero-unavailable rollouts.",
        },
    },

    # 193. Feature Flags vs Code Deployments
    {
        "intent_group_id": "ig_infra_feature_flags_decoupled_release",
        "domain": "infrastructure_deployment",
        "subtopic": "Deployment Strategies & Migrations",
        "task_kind": "explanation",
        "dimensions": {"pattern": {"status": "known", "value": "feature_flags"}},
        "canonical_query": "How do feature flags decouple code deployment from feature release and what are the operational trade-offs?",
        "canonical_response": "Deploying code (pushing bits to production servers) is an engineering activity, while releasing a feature (making functionality visible to end users) is a business decision. Feature flags wrap new code paths in conditional checks evaluated dynamically at runtime. This allows code to be deployed to production continuously while remaining dormant. Features can be enabled instantly for internal testers, specific customer cohorts, or rolled out gradually without re-deploying code. The trade-offs include technical debt from dead flag cleanup, increased code branching complexity, and difficulty in testing combinatorial permutations of flag states.",
        "positive_paraphrase_1": {
            "query": "Explain how feature flags separate the act of deploying code from releasing functionality to users.",
            "reason": "Direct paraphrase explaining the decoupling of deployment from feature release via feature toggles.",
        },
        "positive_paraphrase_2": {
            "query": "What are the benefits and maintenance trade-offs of using runtime feature toggles in continuous delivery?",
            "reason": "Syntactic variation focusing on benefits and technical debt trade-offs of feature toggles.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you evaluate feature flags in Python using the Unleash or LaunchDarkly SDK client?",
            "response": "Initialize the client (`ldclient.set_sdk_key('key')`) and evaluate variations: `show_feature = client.variation('new-checkout', user_context, False)`.",
            "reason": "Asks how to use a specific vendor SDK API rather than explaining the architectural separation of deployment vs release.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python context manager that temporarily mocks a feature flag as True during unit test execution.",
            "response": "```python\nfrom unittest.mock import patch\ndef mock_flag(flag_name, value):\n    return patch('flags.is_enabled', side_effect=lambda name, *a: value if name == flag_name else False)\n```",
            "reason": "Requests Python unit test mocking code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do trunk-based development and short-lived feature branches rely on feature flags to avoid merge hell?",
            "response": "Developers merge incomplete features into main daily behind dark feature flags, avoiding long-lived divergent branches while preventing unfinished features from reaching users.",
            "reason": "Asks about trunk-based development branching workflows rather than feature flag decoupling trade-offs.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between release flags (temporary) and ops/kill-switch flags (permanent)?",
            "response": "Release flags are short-lived toggles deleted after rollout completes; ops flags (circuit breakers, kill switches) remain permanently in code to disable non-critical features during outages.",
            "reason": "Contrasts release flags with operational kill-switches rather than explaining deployment vs release decoupling.",
        },
    },

    # 194. Expand and Contract Schema Migrations
    {
        "intent_group_id": "ig_infra_expand_contract_migrations",
        "domain": "infrastructure_deployment",
        "subtopic": "Deployment Strategies & Migrations",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "migrations"}},
        "canonical_query": "How does the Expand and Contract (Parallel Run) pattern perform zero-downtime database schema migrations?",
        "canonical_response": "The Expand and Contract pattern breaks breaking schema changes (e.g. renaming a column or splitting a table) into non-breaking phases across deployments: 1. Expand: Add the new column/table alongside the old one without deleting anything; the database now supports both structures. 2. Dual-Write: Deploy application code that reads from the old structure but writes to both old and new. Backfill historical records asynchronously. 3. Read Cutover: Deploy application code that now reads and writes exclusively to the new structure. 4. Contract: Once verified and all older application instances are decommissioned, safely drop the old column/table in the database.",
        "positive_paraphrase_1": {
            "query": "Explain the phases of the Expand and Contract pattern for non-breaking zero-downtime database updates.",
            "reason": "Direct paraphrase explaining the 4 phases (expand, dual-write/backfill, cutover, contract) of zero-downtime migrations.",
        },
        "positive_paraphrase_2": {
            "query": "How do you rename a production database column without locking tables or causing application downtime?",
            "reason": "Syntactic variation focusing on column renaming without downtime via expand/contract.",
        },
        "hard_negative_diff_operation": {
            "query": "How do tools like gh-ost or pt-online-schema-change execute online table migrations in MySQL using binlog triggers?",
            "response": "They create a shadow ghost table, copy rows in chunks, apply binary log delta changes asynchronously, and execute an atomic table swap (`RENAME TABLE`).",
            "reason": "Asks how MySQL online schema tools (gh-ost) work internally rather than the general Expand/Contract application pattern.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Alembic migration script in Python that adds a new column 'full_name' with server default.",
            "response": "```python\ndef upgrade():\n    op.add_column('users', sa.Column('full_name', sa.String(255), nullable=True))\n```",
            "reason": "Requests specific Alembic migration script code rather than explaining the architectural pattern.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Prisma migrate handle drift detection and schema migrations compared to Alembic or Flyway?",
            "response": "Prisma compares the declarative `schema.prisma` file with the database shadow database to calculate drift and generate deterministic SQL migration scripts.",
            "reason": "Compares Prisma Migrate with other tools rather than explaining the Expand/Contract architectural strategy.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does adding a NOT NULL constraint without a default value in PostgreSQL lock the entire table in older versions?",
            "response": "In older Postgres versions, adding `NOT NULL` forced an exclusive table lock (`ACCESS EXCLUSIVE`) while the engine scanned every existing row to verify nullness.",
            "reason": "Focuses on Postgres table locking semantics rather than the Expand and Contract pattern phases.",
        },
    },

    # 195. Shadow Traffic (Traffic Mirroring)
    {
        "intent_group_id": "ig_infra_shadow_traffic_mirroring",
        "domain": "infrastructure_deployment",
        "subtopic": "Deployment Strategies & Migrations",
        "task_kind": "explanation",
        "dimensions": {"networking": {"status": "known", "value": "traffic_mirroring"}},
        "canonical_query": "How does shadow traffic (traffic mirroring/dark launching) validate candidate backend versions with zero user risk?",
        "canonical_response": "Shadow traffic (supported by Envoy, Istio, Nginx) duplicates real incoming production HTTP requests at the network proxy layer. The primary request is sent to the current live service, and its response is returned to the user as normal. Simultaneously, an asynchronous duplicate copy of the request is 'shadowed' to the candidate experimental service. The shadow service executes all logic (profiling CPU, latency, correctness, memory leaks), but its responses are silently discarded. This provides realistic production load and edge-case testing with zero impact on real users or production response latencies.",
        "positive_paraphrase_1": {
            "query": "Explain how traffic mirroring replicates live production requests to test candidate services with zero impact on users.",
            "reason": "Direct paraphrase explaining traffic shadowing/mirroring and silent response discarding.",
        },
        "positive_paraphrase_2": {
            "query": "Why is dark launching via Envoy request shadowing safer than canary deployments for high-risk backend rewrites?",
            "reason": "Syntactic variation focusing on zero risk of dark launching compared to user-facing canaries.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure request mirroring in Istio VirtualServices using the mirror directive?",
            "response": "In Istio `VirtualService`, define `mirror: {host: candidate-service, subset: v2}` and `mirrorPercentage: {value: 100.0}` under the HTTP route.",
            "reason": "Asks for Istio YAML syntax to configure mirroring rather than explaining the operational concepts and safety requirements.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Nginx location configuration using mirror /mirror_endpoint to duplicate incoming requests.",
            "response": "```nginx\nlocation /api {\n    mirror /mirror;\n    proxy_pass http://live_backend;\n}\nlocation = /mirror {\n    internal;\n    proxy_pass http://shadow_backend;\n}\n```",
            "reason": "Requests specific Nginx configuration lines rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Diffy test service regressions by comparing output responses of candidate versions against live baselines?",
            "response": "Diffy acts as a proxy that sends requests to candidate and baseline instances, compares JSON response structures, and filters out noise fields (timestamps) to report functional diffs.",
            "reason": "Asks specifically about Diffy regression testing tool rather than general traffic mirroring architecture.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why must shadow traffic be strictly disabled or mocked for non-idempotent side effects like payments and emails?",
            "response": "Because duplicated requests execute real code, a shadowed state-changing request could charge customer credit cards or send duplicate notification emails twice unless external sinks are mocked or stubbed.",
            "reason": "Focuses on preventing duplicated side-effects during mirroring rather than the general mirroring mechanism.",
        },
    },

    # 196. Kubernetes Readiness vs Liveness Probes
    {
        "intent_group_id": "ig_infra_k8s_readiness_vs_liveness",
        "domain": "infrastructure_deployment",
        "subtopic": "Kubernetes Lifecycle & Autoscaling",
        "task_kind": "explanation",
        "dimensions": {"orchestrator": {"status": "known", "value": "kubernetes"}},
        "canonical_query": "What is the critical difference between Kubernetes readiness probes and liveness probes, and what happens when they fail?",
        "canonical_response": "A `livenessProbe` determines whether the container is healthy and running; if it fails consecutively, kubelet aggressively kills the container and restarts it according to its restart policy (used to recover from deadlocks or unrecoverable fatal states). A `readinessProbe` determines whether the container is ready to accept incoming traffic; if it fails, kubelet does NOT restart the container, but instead temporarily removes the pod's IP from all Kubernetes Service endpoints, ensuring no user traffic is routed to it while it warms up caches or recovers from temporary overload.",
        "positive_paraphrase_1": {
            "query": "Compare Kubernetes readiness and liveness probes: how do failure actions (pod restart vs endpoint removal) differ?",
            "reason": "Direct paraphrase comparing readiness and liveness probes and their failure actions.",
        },
        "positive_paraphrase_2": {
            "query": "Why should database connection drops trigger readiness probe failure rather than liveness probe failure in Kubernetes?",
            "reason": "Syntactic variation focusing on overload/db drops failing readiness to prevent restart storms.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure initialDelaySeconds, periodSeconds, and failureThreshold for probes in a Pod spec?",
            "response": "Under `livenessProbe`/`readinessProbe`, configure `httpGet: {path: /health, port: 8080}`, `initialDelaySeconds: 15`, `periodSeconds: 10`, and `failureThreshold: 3`.",
            "reason": "Asks how to configure YAML fields rather than explaining the architectural difference in failure behavior.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FastAPI route providing distinct /healthz (liveness) and /ready (readiness) endpoints.",
            "response": "```python\n@app.get('/healthz')\ndef liveness(): return {'status': 'ok'}\n@app.get('/ready')\ndef readiness():\n    if not db.is_connected(): raise HTTPException(503)\n    return {'status': 'ready'}\n```",
            "reason": "Requests FastAPI endpoint implementation code rather than explaining Kubernetes probe mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do AWS ALB Target Group health checks compare to Kubernetes readiness probes?",
            "response": "AWS ALB target health checks ping instances directly and deregister unhealthy EC2 targets from the load balancer, analogous to Kubernetes removing endpoints from Services.",
            "reason": "Compares AWS ALB target checks with Kubernetes rather than contrasting readiness with liveness.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does making a liveness probe depend on a shared external database cause catastrophic cascading cluster restarts?",
            "response": "If the database experiences a brief slowdown, every pod's liveness probe fails simultaneously, causing Kubernetes to kill and restart all pods at once, causing a cluster-wide blackout.",
            "reason": "Focuses on the specific anti-pattern of external dependency checking in liveness probes.",
        },
    },

    # 197. Kubernetes Startup Probes
    {
        "intent_group_id": "ig_infra_k8s_startup_probes",
        "domain": "infrastructure_deployment",
        "subtopic": "Kubernetes Lifecycle & Autoscaling",
        "task_kind": "configuration",
        "dimensions": {"orchestrator": {"status": "known", "value": "kubernetes"}},
        "canonical_query": "Why were startup probes introduced in Kubernetes and how do they prevent slow-starting applications from entering crash loops?",
        "canonical_response": "Legacy applications (large JVM services, heavy ML model loading) often take several minutes to initialize. Before startup probes, engineers had to increase `livenessProbe.initialDelaySeconds` to huge values (e.g. 5 minutes). This had a fatal drawback: if the container entered a real deadlock later during normal runtime, kubelet would take 5 minutes to detect and restart it. A `startupProbe` disables both liveness and readiness checks until the startup probe succeeds for the first time. It allows generous startup polling (`periodSeconds: 10`, `failureThreshold: 30` = 5 minutes): once passed, fast liveness checks (`periodSeconds: 5`) take over immediately.",
        "positive_paraphrase_1": {
            "query": "Explain how Kubernetes startup probes isolate long container boot times without compromising fast liveness recovery.",
            "reason": "Direct paraphrase explaining startup probes decoupling boot time delays from fast runtime liveness.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do startup probes prevent premature kills on slow-starting legacy Java or machine learning containers?",
            "reason": "Syntactic variation focusing on slow initialization without premature liveness kills.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you calculate the maximum allowed initialization time from periodSeconds and failureThreshold in a startup probe?",
            "response": "Maximum startup duration is `periodSeconds * failureThreshold` (e.g. `10s * 30 = 300s` or 5 minutes); exceeding this window triggers container termination.",
            "reason": "Asks how to calculate the timeout formula rather than explaining the architectural purpose of startup probes.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the YAML container specification containing both a startupProbe with a 3-minute ceiling and a fast livenessProbe.",
            "response": "```yaml\nstartupProbe:\n  httpGet: { path: /health, port: 8080 }\n  periodSeconds: 10\n  failureThreshold: 18\nlivenessProbe:\n  httpGet: { path: /health, port: 8080 }\n  periodSeconds: 5\n```",
            "reason": "Requests specific YAML specification code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Kubernetes init containers differ from startup probes in preparing container state before startup?",
            "response": "Init containers run to completion sequentially before the application container starts, whereas startup probes execute inside the live application container as it initializes.",
            "reason": "Compares init containers with startup probes rather than explaining startup probe mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a startup probe fails all retries during application initialization?",
            "response": "If the startup probe fails `failureThreshold` times, kubelet kills the container and subjects it to the Pod's `restartPolicy`, entering a CrashLoopBackOff state.",
            "reason": "Focuses on the failure and CrashLoopBackOff outcome rather than the architectural benefits over initialDelaySeconds.",
        },
    },

    # 198. Pod Disruption Budgets (PDB)
    {
        "intent_group_id": "ig_infra_k8s_pod_disruption_budgets",
        "domain": "infrastructure_deployment",
        "subtopic": "Kubernetes Lifecycle & Autoscaling",
        "task_kind": "configuration",
        "dimensions": {"orchestrator": {"status": "known", "value": "kubernetes"}},
        "canonical_query": "How do Pod Disruption Budgets (PDB) protect high availability during Kubernetes node upgrades and voluntary disruptions?",
        "canonical_response": "Disruptions in Kubernetes are either involuntary (hardware failure, kernel panic, VM preemption) or voluntary (cluster autoscaler scale-down, node OS patching, `kubectl drain`). While involuntary disruptions cannot be prevented, voluntary disruptions must obey Pod Disruption Budgets. A PDB specifies the minimum number (`minAvailable`) or percentage of pods that must remain healthy, or the maximum number of pods (`maxUnavailable`) that can be evicted at one time. When an administrator or cluster autoscaler drains a node, the Eviction API checks the PDB: if evicting a pod would breach the budget, the eviction is blocked until replacements are healthy.",
        "positive_paraphrase_1": {
            "query": "Explain how Pod Disruption Budgets (PDB) prevent outages during kubectl drain and node pool maintenance.",
            "reason": "Direct paraphrase explaining PDB protection during voluntary evictions like kubectl drain.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Kubernetes Eviction API enforce minAvailable constraints during voluntary node draining?",
            "reason": "Syntactic variation focusing on the Eviction API enforcing minAvailable during draining.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you safely drain a Kubernetes node ignoring daemonsets and allowing pod deletion using kubectl drain?",
            "response": "Run `kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --force` to cordon the node and evict eligible workloads.",
            "reason": "Asks for kubectl drain CLI flags rather than explaining PDB architecture and budget rules.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Kubernetes PodDisruptionBudget YAML manifest that ensures at least 2 replicas are available for app: redis.",
            "response": "```yaml\napiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata: { name: redis-pdb }\nspec:\n  minAvailable: 2\n  selector:\n    matchLabels: { app: redis }\n```",
            "reason": "Requests specific PDB YAML manifest code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Karpenter node consolidation respect Pod Disruption Budgets when replacing underutilized EC2 instances?",
            "response": "Karpenter queries the Eviction API before terminating nodes: if any pod on the node is protected by an active PDB that would be violated, Karpenter skips consolidation.",
            "reason": "Asks about Karpenter node autoscaling implementation rather than fundamental Kubernetes PDB concepts.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can an aggressive PDB with minAvailable: 100% permanently block node upgrades if a cluster has no spare capacity?",
            "response": "If `minAvailable` is 100% and there are no spare nodes to schedule a replacement pod before evicting the old one, the Eviction API permanently denies eviction, stalling upgrades.",
            "reason": "Focuses on the deadlock failure mode of over-constrained PDBs rather than general PDB protection.",
        },
    },

    # 199. CPU Requests vs Limits and Throttling
    {
        "intent_group_id": "ig_infra_k8s_cpu_requests_vs_limits",
        "domain": "infrastructure_deployment",
        "subtopic": "Kubernetes Lifecycle & Autoscaling",
        "task_kind": "configuration",
        "dimensions": {"orchestrator": {"status": "known", "value": "kubernetes"}},
        "canonical_query": "What is the difference between CPU requests and CPU limits in Kubernetes, and why can CPU limits cause unexpected throttling?",
        "canonical_response": "`cpu.requests` is used by the `kube-scheduler` to place pods on nodes; the node guarantees these CPU cycles using Linux CFS shares under contention. `cpu.limits` enforces a hard ceiling via Linux CFS quota (`cpu.cfs_quota_us` per 100 ms period). If a multi-threaded application (e.g. Node.js or Go runtime spawning background threads) consumes its quota in the first 20 ms of a 100 ms period, the Linux kernel completely throttles the container's threads for the remaining 80 ms, regardless of whether other CPU cores on the host node are 100% idle, causing severe P99 latency spikes.",
        "positive_paraphrase_1": {
            "query": "Explain how Kubernetes CPU requests guide scheduling while CPU limits enforce CFS bandwidth throttling.",
            "reason": "Direct paraphrase explaining CPU requests for scheduling and CFS quota limits causing throttling.",
        },
        "positive_paraphrase_2": {
            "query": "Why does exceeding Kubernetes CPU limits cause latency spikes even when host machine CPU is mostly idle?",
            "reason": "Syntactic variation focusing on CFS quota period exhaustion causing throttling on idle hosts.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you detect CPU throttling in a running container using container_cpu_cfs_throttled_periods_total in Prometheus?",
            "response": "Query `rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])` in Prometheus to calculate throttled period percentage.",
            "reason": "Asks how to monitor throttling in Prometheus rather than explaining requests vs limits and CFS mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the resources YAML block for a pod setting CPU request to 500m and memory limit to 1Gi.",
            "response": "```yaml\nresources:\n  requests:\n    cpu: \"500m\"\n    memory: \"512Mi\"\n  limits:\n    memory: \"1Gi\"\n```",
            "reason": "Requests YAML code block rather than conceptual explanation of CPU throttling.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does memory.limits differ from cpu.limits regarding OOM kill vs throttling in Linux cgroups?",
            "response": "Exceeding CPU limits results in CPU CFS throttling (slowing execution), whereas exceeding memory limits triggers immediate kernel OOM killing of the process.",
            "reason": "Compares CPU limits with Memory limits rather than focusing on CPU requests vs CPU limits.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why do some production engineering teams recommend omitting CPU limits while strictly setting CPU requests?",
            "response": "Omitting CPU limits allows containers to burst freely into unallocated idle CPU capacity on the node without triggering CFS throttling, while requests prevent node overcommitment.",
            "reason": "Analyzes the operational philosophy of running without CPU limits rather than explaining the difference between requests and limits.",
        },
    },

    # 200. Horizontal Pod Autoscaler (HPA)
    {
        "intent_group_id": "ig_infra_k8s_hpa_autoscaling",
        "domain": "infrastructure_deployment",
        "subtopic": "Kubernetes Lifecycle & Autoscaling",
        "task_kind": "configuration",
        "dimensions": {"orchestrator": {"status": "known", "value": "kubernetes"}},
        "canonical_query": "How does the Kubernetes Horizontal Pod Autoscaler (HPA) calculate desired replicas from metric values?",
        "canonical_response": "The Horizontal Pod Autoscaler periodically (default 15s) queries metrics (via Metrics Server, Prometheus Adapter, or Custom Metrics API). It calculates the desired replica count using the formula: $\\text{desiredReplicas} = \\lceil \\text{currentReplicas} \\times (\\text{currentMetricValue} / \\text{targetMetricValue}) \\rceil$. To avoid thrashing ('flapping') from minor oscillations, HPA applies a tolerance threshold (default 10%): if the ratio is within `0.9` to `1.1`, no scale event occurs. Scale-down stabilization windows (`behavior.scaleDown.stabilizationWindowSeconds: 300`) smooth scaling actions to prevent premature scale-down during temporary dips.",
        "positive_paraphrase_1": {
            "query": "Explain the formula and stabilization windows the Kubernetes Horizontal Pod Autoscaler uses to determine replica counts.",
            "reason": "Direct paraphrase explaining the HPA replica calculation formula, tolerance, and stabilization windows.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the HPA mathematical formula and tolerance band prevent replica flapping during traffic fluctuations?",
            "reason": "Syntactic variation focusing on the replica formula and tolerance band preventing flapping.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure KEDA (Kubernetes Event-driven Autoscaling) to scale pods based on RabbitMQ queue depth?",
            "response": "Create a `ScaledObject` resource with `triggers: [{type: rabbitmq, metadata: {queueName: orders, queueLength: '20'}}]`.",
            "reason": "Asks how to configure KEDA for RabbitMQ rather than explaining native Kubernetes HPA mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Kubernetes HPA v2 manifest that targets 75% average CPU utilization with min 2 and max 10 replicas.",
            "response": "```yaml\napiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nspec:\n  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: web }\n  minReplicas: 2; maxReplicas: 10\n  metrics:\n  - type: Resource\n    resource: { name: cpu, target: { type: Utilization, averageUtilization: 75 } }\n```",
            "reason": "Requests specific HPA YAML manifest code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Vertical Pod Autoscaler (VPA) differ from the Horizontal Pod Autoscaler (HPA)?",
            "response": "HPA scales the number of pod replicas horizontally, while VPA scales the CPU and memory resource requests/limits of individual pods vertically.",
            "reason": "Compares VPA with HPA rather than explaining HPA calculation algorithms.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does HPA fail to scale deployments based on CPU utilization if pods do not have cpu.requests defined?",
            "response": "CPU utilization percentage is defined as `currentUsage / request`; without a defined `request`, the denominator is undefined and HPA cannot calculate the utilization ratio.",
            "reason": "Focuses on the prerequisite of defining CPU requests for percentage utilization rather than the scaling formula.",
        },
    },

    # 201. OpenTelemetry Distributed Tracing (W3C Trace Context)
    {
        "intent_group_id": "ig_infra_opentelemetry_w3c_tracing",
        "domain": "infrastructure_deployment",
        "subtopic": "Observability & SRE Metrics",
        "task_kind": "explanation",
        "dimensions": {"observability": {"status": "known", "value": "opentelemetry"}},
        "canonical_query": "How does OpenTelemetry propagate distributed tracing context across microservices using W3C Trace Context headers?",
        "canonical_response": "In a distributed microservice call chain, OpenTelemetry tracks requests as a tree of 'spans' forming a single 'trace'. Context propagation across network boundaries uses standard W3C HTTP headers: `traceparent` and `tracestate`. The `traceparent` header is formatted as `version-trace_id-parent_span_id-trace_flags` (e.g. `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01`). The downstream service extracts these headers from incoming requests, sets the incoming `trace_id` as its own, and creates child spans referencing the caller's `span_id` as their parent, weaving disparate service logs and timings into a unified trace graph.",
        "positive_paraphrase_1": {
            "query": "Explain how W3C traceparent headers propagate trace IDs and parent span IDs across distributed microservices.",
            "reason": "Direct paraphrase explaining W3C traceparent formatting and context propagation across microservices.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does OpenTelemetry context injection and extraction connect independent service spans into a single trace?",
            "reason": "Syntactic variation focusing on span context injection and extraction forming unified traces.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure the OpenTelemetry Collector to receive OTLP traces on port 4317 and export to Jaeger?",
            "response": "In `otel-collector-config.yaml`, configure `receivers: {otlp: {protocols: {grpc: {endpoint: 0.0.0.0:4317}}}}` and `exporters: {jaeger: {endpoint: jaeger:14250}}`.",
            "reason": "Asks for OTel Collector pipeline configuration rather than explaining W3C context propagation.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write Python code using opentelemetry-sdk to manually create a tracer and start an active child span.",
            "response": "```python\nfrom opentelemetry import trace\ntracer = trace.get_tracer(__name__)\nwith tracer.start_as_current_span('process_order') as span:\n    span.set_attribute('order.id', 123)\n```",
            "reason": "Requests specific Python SDK code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How did legacy B3 propagation headers (X-B3-TraceId) compare to the modern W3C Trace Context standard?",
            "response": "Zipkin's B3 specification used separate headers (`X-B3-TraceId`, `X-B3-SpanId`, `X-B3-Sampled`) whereas W3C consolidates them into a single `traceparent` header.",
            "reason": "Compares Zipkin B3 with W3C rather than explaining OpenTelemetry W3C distributed tracing mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the role of head-based sampling versus tail-based sampling in OpenTelemetry tracing pipelines?",
            "response": "Head-based sampling decides whether to record a trace at the root service before execution begins; tail-based sampling buffers all spans in an OTel collector and samples based on whether an error or high latency occurred.",
            "reason": "Contrasts head-based and tail-based sampling strategies rather than explaining W3C context propagation.",
        },
    },

    # 202. Prometheus Metric Types (Counter vs Gauge vs Histogram)
    {
        "intent_group_id": "ig_infra_prometheus_metric_types",
        "domain": "infrastructure_deployment",
        "subtopic": "Observability & SRE Metrics",
        "task_kind": "explanation",
        "dimensions": {"observability": {"status": "known", "value": "prometheus"}},
        "canonical_query": "What is the difference between Prometheus Counter, Gauge, Histogram, and Summary metric types?",
        "canonical_response": "1. `Counter`: A cumulative metric that only increases monotonically or resets to zero on restart (e.g. `http_requests_total`), queried using `rate()` or `increase()`. 2. `Gauge`: A metric representing a single numerical value that can arbitrarily go up or down (e.g. `memory_usage_bytes`, `cpu_temperature`). 3. `Histogram`: Samples observations (e.g. request durations) into pre-configured configurable buckets (`le` labels), plus `_count` and `_sum`, allowing cluster-wide aggregation of percentiles via `histogram_quantile()`. 4. `Summary`: Calculates configurable phi-quantiles directly on the client instance; this provides accurate local percentiles but cannot be aggregated across multiple server instances.",
        "positive_paraphrase_1": {
            "query": "Compare Prometheus Counters, Gauges, Histograms, and Summaries: how do their values and aggregations differ?",
            "reason": "Direct paraphrase comparing the 4 Prometheus metric types and aggregation rules.",
        },
        "positive_paraphrase_2": {
            "query": "Why can Histogram metrics be aggregated across multiple instances while Summary quantiles cannot in Prometheus?",
            "reason": "Syntactic variation focusing on Histogram vs Summary aggregation across multi-instance clusters.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you calculate the P99 request latency in PromQL using the histogram_quantile() function?",
            "response": "Run `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))` to calculate cluster-wide 99th percentile.",
            "reason": "Asks for PromQL query syntax rather than explaining the architectural definitions and differences of metric types.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python snippet using prometheus_client to define a Counter for errors and a Gauge for active connections.",
            "response": "```python\nfrom prometheus_client import Counter, Gauge\nERRORS = Counter('app_errors_total', 'Total errors', ['type'])\nCONNS = Gauge('active_connections', 'Current active connections')\n```",
            "reason": "Requests specific Python code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do StatsD metrics (timers, meters, sets) differ from Prometheus pull-based scraped metrics?",
            "response": "StatsD pushes UDP packets to a local daemon that flushes aggregates to Graphite, whereas Prometheus scrapes HTTP `/metrics` endpoints pull-style on configured scrape intervals.",
            "reason": "Compares StatsD push architecture with Prometheus pull architecture rather than explaining Prometheus metric types.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does high label cardinality in Prometheus cause excessive memory consumption and TSDB compaction crashes?",
            "response": "Each unique combination of key-value label pairs creates a new time series in memory; high-cardinality labels (like user IDs or UUIDs) generate millions of series, exhausting RAM.",
            "reason": "Focuses on high label cardinality exhaustion rather than the four core metric types.",
        },
    },

    # 203. Structured JSON Logging vs Plain Text
    {
        "intent_group_id": "ig_infra_structured_json_logging",
        "domain": "infrastructure_deployment",
        "subtopic": "Observability & SRE Metrics",
        "task_kind": "explanation",
        "dimensions": {"observability": {"status": "known", "value": "logging"}},
        "canonical_query": "Why is structured JSON logging preferred over unstructured plain-text strings in modern microservice architectures?",
        "canonical_response": "Unstructured text logs (e.g. `2026-03-01 ERROR User 123 failed login`) require brittle, CPU-intensive regular expression parsing (Grok filters in Logstash or Fluentd) to extract fields, which break whenever developers alter spacing or wording. Structured JSON logging emits logs as serialized key-value JSON objects containing standardized schema attributes (`{\"timestamp\":\"...\", \"level\":\"ERROR\", \"user_id\":123, \"trace_id\":\"...\", \"event\":\"login_failed\"}`). Log aggregation platforms (Elasticsearch, Loki, Datadog) ingest and index these fields directly without regex parsing, enabling fast, indexed filtering, aggregations, and correlation with distributed traces.",
        "positive_paraphrase_1": {
            "query": "Explain how structured JSON logs eliminate regex parsing and enable fast filtering in log aggregation systems.",
            "reason": "Direct paraphrase explaining structured logging eliminating regex overhead and enabling fast search.",
        },
        "positive_paraphrase_2": {
            "query": "What are the advantages of emitting machine-readable JSON log events instead of formatted string messages?",
            "reason": "Syntactic variation focusing on machine-readable key-value fields vs plain formatted strings.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure Vector or Fluent Bit to parse JSON container logs from /var/log/pods/* in Kubernetes?",
            "response": "In Fluent Bit, configure an `[INPUT]` tailing pod logs and attach a `[FILTER]` with `Name parser` and `Parser json` to parse raw JSON stdout.",
            "reason": "Asks how to configure Fluent Bit/Vector agents rather than explaining the architectural benefits of structured logging.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python logger configuration using structlog or python-json-logger that outputs JSON lines to stdout.",
            "response": "```python\nimport logging\nfrom pythonjsonlogger import jsonlogger\nlogHandler = logging.StreamHandler()\nlogHandler.setFormatter(jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(message)s'))\nlogging.getLogger().addHandler(logHandler)\n```",
            "reason": "Requests specific Python logger implementation code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Grafana Loki's index-free log aggregation model differ from Elasticsearch's full-text inverted index?",
            "response": "Loki only indexes metadata stream labels (analogous to Prometheus) and greps chunk payloads dynamically, whereas Elasticsearch indexes every text word and JSON field.",
            "reason": "Compares Loki vs Elasticsearch indexing architecture rather than structured vs unstructured logging.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the trade-off of structured JSON logging regarding disk bandwidth and log file storage size?",
            "response": "JSON logs are more verbose than compact text lines because key names (`timestamp`, `message`, `service`) are repeated on every line, increasing raw storage volume by 20-40% before compression.",
            "reason": "Focuses on payload verbosity and storage overhead rather than querying and parsing advantages.",
        },
    },

    # 204. Synthetic Monitoring vs Real User Monitoring (RUM)
    {
        "intent_group_id": "ig_infra_synthetic_vs_rum_monitoring",
        "domain": "infrastructure_deployment",
        "subtopic": "Observability & SRE Metrics",
        "task_kind": "explanation",
        "dimensions": {"observability": {"status": "known", "value": "rum_vs_synthetic"}},
        "canonical_query": "What is the difference between Synthetic Monitoring and Real User Monitoring (RUM) in web performance observability?",
        "canonical_response": "Synthetic Monitoring uses automated headless browsers or API probe workers running from controlled external vantage points (e.g. data centers around the world) to simulate user transactions on fixed intervals (e.g. every 5 minutes). It provides consistent, baseline performance benchmarks and alerts on downtime even when real user traffic is zero. Real User Monitoring (RUM) injects JavaScript telemetry agents into actual end-user browsers to measure real experiences (Core Web Vitals, real network latencies across diverse ISPs, mobile devices, and locations). It reflects true user impact but suffers from high data noise and cannot detect outages when user traffic is absent.",
        "positive_paraphrase_1": {
            "query": "Compare Synthetic Monitoring and Real User Monitoring (RUM): how do controlled probes differ from browser telemetry?",
            "reason": "Direct paraphrase comparing synthetic probes and real user browser telemetry.",
        },
        "positive_paraphrase_2": {
            "query": "Why is synthetic monitoring required to detect outages during zero-traffic hours while RUM captures real client diversity?",
            "reason": "Syntactic variation focusing on zero-traffic outage detection vs real device diversity.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you capture Core Web Vitals (LCP, FID, CLS) in the browser using the web-vitals JavaScript library?",
            "response": "Import `onCLS`, `onFID`, `onLCP` from `web-vitals` and send metrics to an analytics endpoint via `navigator.sendBeacon()`.",
            "reason": "Asks how to use the web-vitals JavaScript library rather than comparing synthetic vs RUM monitoring architectures.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Playwright synthetic probe script in Node.js that logs in to a portal and checks for HTTP 200.",
            "response": "```javascript\nconst { chromium } = require('playwright');\n(async () => {\n  const browser = await chromium.launch();\n  const page = await browser.newPage();\n  await page.goto('https://example.com/login');\n  await browser.close();\n})();\n```",
            "reason": "Requests Playwright synthetic script implementation code rather than conceptual comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Blackbox Exporter in Prometheus perform external HTTP and TCP synthetic probes?",
            "response": "Blackbox Exporter accepts probe targets over HTTP (`/probe?target=...&module=http_2xx`) and returns success/failure flags and latency metrics to Prometheus.",
            "reason": "Asks about Prometheus Blackbox Exporter rather than comparing Synthetic and RUM paradigms.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can aggressive ad-blockers and privacy extensions bias Real User Monitoring (RUM) datasets?",
            "response": "Privacy extensions frequently block RUM tracking scripts, selectively filtering out tech-savvy users or modern browser cohorts from the telemetry dataset.",
            "reason": "Focuses on ad-blocker sampling bias in RUM rather than contrasting synthetic with RUM monitoring.",
        },
    },

    # 205. SLO, SLI, and Error Budgets
    {
        "intent_group_id": "ig_infra_slo_sli_error_budgets",
        "domain": "infrastructure_deployment",
        "subtopic": "Observability & SRE Metrics",
        "task_kind": "explanation",
        "dimensions": {"sre": {"status": "known", "value": "slo"}},
        "canonical_query": "What are SLIs, SLOs, and Error Budgets in Site Reliability Engineering (SRE), and how do they balance feature velocity with reliability?",
        "canonical_response": "1. `SLI` (Service Level Indicator): A quantifiable metric measuring service performance (e.g. percentage of HTTP requests returning `< 500` in `< 200ms`). 2. `SLO` (Service Level Objective): A target reliability percentage agreed upon with stakeholders (e.g. 99.9% of requests meet the SLI over a rolling 30-day window). 3. `Error Budget`: The allowable unreliability ($1 - \\text{SLO}$, so $0.1\\%$ or ~43 minutes of downtime per month for a 99.9% SLO). It balances velocity and reliability: if the error budget is healthy, developers ship features aggressively; if the error budget is exhausted, feature releases are paused and engineering resources pivot entirely to reliability and hardening.",
        "positive_paraphrase_1": {
            "query": "Explain how SLIs, SLOs, and Error Budgets work together in SRE to govern feature release velocity.",
            "reason": "Direct paraphrase explaining the trio of SLI, SLO, and error budgets governing release velocity.",
        },
        "positive_paraphrase_2": {
            "query": "In Site Reliability Engineering, what is an Error Budget and how does exhausting it halt feature rollouts?",
            "reason": "Syntactic variation focusing on error budgets and release freezes upon budget exhaustion.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you calculate multi-window multi-burn-rate alerts for an SLO in Prometheus to reduce false alarm paging?",
            "response": "Configure alerting rules combining 1-hour and 6-hour burn rate windows (e.g. burn rate > 14.4 over 1h AND 6h), triggering alerts only when error budget is rapidly consuming.",
            "reason": "Asks how to calculate multi-window burn rate alert rules rather than explaining the core definitions and concepts.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Sloth or Pyrra Prometheus rule definition in YAML configuring a 99.9% availability SLO on HTTP requests.",
            "response": "```yaml\nversion: \"prometheus/v1\"\nservice: \"api\"\nslos:\n  - name: \"availability\"\n    objective: 99.9\n    sli:\n      events:\n        error_query: sum(rate(http_requests_total{status=~\"5..\"}[5m]))\n        total_query: sum(rate(http_requests_total[5m]))\n```",
            "reason": "Requests specific YAML rule configuration code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does an SLA (Service Level Agreement) differ from an SLO regarding legal and contractual penalties?",
            "response": "An SLO is an internal engineering target; an SLA is an external legal contract with customers specifying financial refunds or penalties if availability drops below the agreement.",
            "reason": "Contrasts internal SLOs with legal SLAs rather than explaining SLI, SLO, and error budget mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is setting an unrealistic SLO target like 100% availability detrimental to product innovation?",
            "response": "A 100% SLO leaves an error budget of exactly zero, making any code update, deployment, or network fluctuation an unacceptable violation, completely halting engineering velocity.",
            "reason": "Discusses the philosophical flaw of 100% availability targets rather than explaining how the three concepts interact.",
        },
    },

    # 206. Reproducible Builds & Determinism
    {
        "intent_group_id": "ig_infra_reproducible_builds_determinism",
        "domain": "infrastructure_deployment",
        "subtopic": "CI/CD, Supply Chain & Secrets",
        "task_kind": "explanation",
        "dimensions": {"security": {"status": "known", "value": "supply_chain"}},
        "canonical_query": "What are reproducible builds and what non-deterministic factors (timestamps, file ordering, compiler paths) must be eliminated?",
        "canonical_response": "A build is reproducible (deterministic) if given the exact same source code, build environment, and build instructions, it produces byte-for-byte identical binary outputs across different machines and compilation times. This is vital for software supply chain security, proving that a binary has not been compromised with backdoors during CI. Non-deterministic factors that must be eliminated include: embedded compile timestamps (fixed using the `SOURCE_DATE_EPOCH` environment variable), non-deterministic filesystem directory traversal order (fixed by sorting files before archiving/linking), absolute host paths embedded in debug symbols, and locale/timezone discrepancies.",
        "positive_paraphrase_1": {
            "query": "Explain what reproducible builds are and how eliminating compile timestamps and file ordering ensures byte-for-byte identical binaries.",
            "reason": "Direct paraphrase explaining reproducible builds and eliminating timestamps/file ordering.",
        },
        "positive_paraphrase_2": {
            "query": "Why does SOURCE_DATE_EPOCH enable deterministic builds across independent build servers?",
            "reason": "Syntactic variation focusing on SOURCE_DATE_EPOCH eliminating compile timestamp non-determinism.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you verify whether two binaries are bit-for-bit identical using diffoscope on Linux?",
            "response": "Run `diffoscope binary1.tar.gz binary2.tar.gz` to unpack archive formats and perform recursive structural diffs of ELF, zip, and tar files.",
            "reason": "Asks how to use the diffoscope tool rather than explaining the concepts and causes of build non-determinism.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the GCC compiler flags (-ffile-prefix-map) to strip host build directory paths from debug symbols.",
            "response": "```bash\ngcc -g -ffile-prefix-map=$(pwd)=. -fdebug-prefix-map=$(pwd)=. -o binary main.c\n```",
            "reason": "Requests specific GCC compiler flags rather than explaining reproducible build architecture.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Nix package manager achieve bit-for-bit reproducible environments using functional packaging and cryptographic hashes?",
            "response": "Nix treats packages as pure immutable functions, storing all outputs in isolated `/nix/store/<hash>-package` paths derived strictly from source and dependency hashes.",
            "reason": "Asks about Nix package manager architecture rather than general reproducible build concepts.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between hermetic builds (isolated dependencies) and reproducible builds (identical output bytes)?",
            "response": "A hermetic build executes in an isolated sandbox without network access using fixed pinned tools; a reproducible build goes further, guaranteeing byte-for-byte bit equality of the final artifact.",
            "reason": "Contrasts hermetic builds with reproducible builds rather than explaining the non-deterministic factors.",
        },
    },

    # 207. CI/CD Dependency Cache Keys
    {
        "intent_group_id": "ig_infra_ci_dependency_cache_keys",
        "domain": "infrastructure_deployment",
        "subtopic": "CI/CD, Supply Chain & Secrets",
        "task_kind": "configuration",
        "dimensions": {"ci_cd": {"status": "known", "value": "caching"}},
        "canonical_query": "How do content-hash-based cache keys (actions/cache) prevent stale dependencies and speed up CI/CD workflows?",
        "canonical_response": "In CI/CD (e.g. GitHub Actions `actions/cache`), downloading and compiling language dependencies (`node_modules`, `~/.cargo`, Python virtualenvs) on every run wastes minutes. Content-hash-based cache keys compute a cryptographic hash of the exact dependency manifest lockfiles: `key: ${{ runner.os }}-npm-${{ hashFiles('**/package-lock.json') }}`. If no dependencies change, the key matches, restoring the cache instantly. When a dependency is added or updated, the lockfile hash changes, creating a cache miss and forcing a fresh installation that is saved under the new key. Fallback `restore-keys` allow prefix matching to download the nearest historical cache.",
        "positive_paraphrase_1": {
            "query": "Explain how hashFiles on lockfiles generates deterministic CI cache keys and prevents stale dependencies.",
            "reason": "Direct paraphrase explaining hashFiles on lockfiles, deterministic keys, and restoring caches.",
        },
        "positive_paraphrase_2": {
            "query": "Why does using package-lock.json hash in CI cache keys ensure dependencies are invalidated whenever packages change?",
            "reason": "Syntactic variation focusing on lockfile hash invalidating caches when dependencies change.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure GitHub Actions actions/cache for Python poetry or pip with restore-keys fallbacks?",
            "response": "Use `actions/cache@v4` with `path: ~/.cache/pip`, `key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}`, and `restore-keys: ${{ runner.os }}-pip-`.",
            "reason": "Asks for GitHub Actions YAML syntax for pip caching rather than explaining how hash keys work.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a GitHub Actions workflow step caching ~/.m2/repository for Maven based on pom.xml hash.",
            "response": "```yaml\n- uses: actions/cache@v4\n  with:\n    path: ~/.m2/repository\n    key: ${{ runner.os }}-maven-${{ hashFiles('**/pom.xml') }}\n    restore-keys: ${{ runner.os }}-maven-\n```",
            "reason": "Requests specific Maven GitHub Actions YAML code rather than explaining cache key mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does GitLab CI runner cache compare to GitHub Actions cache regarding distributed S3 object storage?",
            "response": "GitLab CI runners compress caches into zip files and push them to shared S3/MinIO buckets keyed by `cache:key:files: [package-lock.json]`.",
            "reason": "Compares GitLab CI caching with GitHub Actions rather than explaining content-hash caching principles.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does caching dynamic build directories without proper lockfile hashing introduce non-reproducible test failures?",
            "response": "If dynamic build artifacts are cached without strict invalidation, deleted or renamed classes persist in the cache, causing succeeding tests locally to fail intermittently in CI.",
            "reason": "Focuses on cache pollution and stale artifact contamination rather than how lockfile hash keys work.",
        },
    },

    # 208. Git Branch Protection Rules & PR Status Checks
    {
        "intent_group_id": "ig_infra_git_branch_protection_status_checks",
        "domain": "infrastructure_deployment",
        "subtopic": "CI/CD, Supply Chain & Secrets",
        "task_kind": "configuration",
        "dimensions": {"git": {"status": "known", "value": "branch_protection"}},
        "canonical_query": "How do GitHub branch protection rules and mandatory PR status checks enforce code quality gates before merging?",
        "canonical_response": "Branch protection rules on default branches (`main`/`master`) prevent direct pushes and force all changes through Pull Requests. Key enforcement controls include: requiring pull request reviews before merging (with a minimum number of approving reviews and dismissals on new commits), requiring status checks to pass before merging (e.g. unit test suites, linting, SAST security scans), and requiring branches to be up to date with main before merging. This guarantees that unreviewed, failing, or conflicting code cannot enter the primary branch.",
        "positive_paraphrase_1": {
            "query": "Explain how branch protection rules and required CI status checks prevent broken code from being merged into main.",
            "reason": "Direct paraphrase explaining branch protection rules, required status checks, and quality gates.",
        },
        "positive_paraphrase_2": {
            "query": "Why is 'Require branches to be up to date before merging' essential for preventing semantic conflicts in Git?",
            "reason": "Syntactic variation focusing on up-to-date branch checks preventing semantic merge bugs.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure GitHub branch protection rules programmatically using the GitHub REST API or Terraform?",
            "response": "In Terraform, use resource `github_branch_protection` setting `enforce_admins = true`, `required_status_checks`, and `required_pull_request_reviews`.",
            "reason": "Asks how to automate branch protection via Terraform or REST API rather than explaining the quality gate concepts.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a GitHub Actions workflow that triggers on pull_request and outputs a check named 'lint-and-test'.",
            "response": "```yaml\nname: Quality Gate\non: [pull_request]\njobs:\n  lint-and-test:\n    runs-on: ubuntu-latest\n    steps:\n    - uses: actions/checkout@v4\n    - run: npm test\n```",
            "reason": "Requests specific GitHub Actions workflow YAML rather than explaining branch protection rules.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do GitHub Merge Queues differ from standard 'Require branches to be up to date' in high-velocity teams?",
            "response": "Merge Queues automatically serialize and test pull requests in speculative batches against upcoming main heads, avoiding merge serial bottlenecks.",
            "reason": "Asks specifically about Merge Queues rather than general branch protection rules and status checks.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What risk arises if administrators are exempt from branch protection rules (Do not enforce for administrators)?",
            "response": "If admins bypass protection, accidental force-pushes (`git push --force`) or urgent un-reviewed commits can bypass CI gates, corrupting git history or deploying broken code.",
            "reason": "Focuses on the security risk of admin bypass rather than the operation of quality gate status checks.",
        },
    },

    # 209. Immutable Container Tags vs Mutable 'latest'
    {
        "intent_group_id": "ig_infra_immutable_container_tags",
        "domain": "infrastructure_deployment",
        "subtopic": "CI/CD, Supply Chain & Secrets",
        "task_kind": "explanation",
        "dimensions": {"container": {"status": "known", "value": "docker"}},
        "canonical_query": "Why is using mutable container image tags like :latest dangerous in production, and how do immutable SHA digests fix this?",
        "canonical_response": "The `:latest` tag (and version tags like `:v1`) is mutable: whenever a new image is pushed with the same tag, the tag pointer moves to the new image. In production, this causes non-deterministic deployments: if pods restart on a new node with `imagePullPolicy: Always`, they download a different binary than sibling pods running on older nodes. Rollbacks become impossible because the old image reference was overwritten. Immutable tags using git commit SHAs (`:sha-a1b2c3d`) or pinning directly to the cryptographic sha256 digest (`image@sha256:7f83b1...`) guarantee that every deployment and restart pulls the exact same byte-for-byte image binary.",
        "positive_paraphrase_1": {
            "query": "Explain why mutable :latest tags lead to inconsistent deployments and how immutable sha256 image digests solve this.",
            "reason": "Direct paraphrase explaining mutable tag risks and immutable sha256 image digests.",
        },
        "positive_paraphrase_2": {
            "query": "What problems occur in Kubernetes clusters when container images use floating tags instead of git commit SHAs?",
            "reason": "Syntactic variation focusing on Kubernetes node pull inconsistencies with floating tags.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you enforce image immutability in AWS ECR or Harbor registry settings?",
            "response": "In AWS ECR repository settings, enable `imageTagMutability: IMMUTABLE`, which causes the registry to reject any `docker push` attempting to overwrite an existing tag.",
            "reason": "Asks how to configure registry immutability settings rather than explaining the operational risks of mutable tags.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide a Kubernetes Pod manifest referencing an image pinned strictly to its sha256 content digest.",
            "response": "```yaml\napiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: web\n    image: myrepo/app@sha256:45b23dee08af5e43a7fa6c4e1aa81a74fb699fb142299e6145b37b8164742359\n```",
            "reason": "Requests specific Kubernetes YAML syntax rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Cosign sign container images and verify signatures in Kubernetes admission controllers?",
            "response": "Cosign signs the container image digest with an OIDC identity and stores the signature in the OCI registry, verified at admission by Kyverno or Sigstore Policy Controller.",
            "reason": "Asks about cryptographic container image signing with Cosign rather than mutable vs immutable image tags.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference in behavior between imagePullPolicy: Always and imagePullPolicy: IfNotPresent in Kubernetes?",
            "response": "`Always` forces kubelet to contact the registry on every container start to verify the digest; `IfNotPresent` reuses locally cached layers without network verification if the tag exists locally.",
            "reason": "Contrasts imagePullPolicy options rather than explaining the hazards of mutable tags.",
        },
    },

    # 210. Secrets Management: Vault vs Environment Variables
    {
        "intent_group_id": "ig_infra_vault_vs_env_vars_secrets",
        "domain": "infrastructure_deployment",
        "subtopic": "CI/CD, Supply Chain & Secrets",
        "task_kind": "explanation",
        "dimensions": {"security": {"status": "known", "value": "secrets_management"}},
        "canonical_query": "What are the security vulnerabilities of static environment variable secrets, and how does HashiCorp Vault dynamic secrets mitigate them?",
        "canonical_response": "Storing static secrets (API keys, database passwords) in environment variables has severe security vulnerabilities: environment variables are easily leaked through process crash logs, debug endpoints (`/proc/<PID>/environ`), child process inheritance, and error monitoring tools (Sentry). Moreover, static secrets are rarely rotated. HashiCorp Vault mitigates this using 'dynamic secrets': instead of storing a static password, applications authenticate to Vault (via Kubernetes service accounts) and Vault generates unique, short-lived credentials on-demand (e.g. creating a PostgreSQL user with a 1-hour lease). When the lease expires, Vault automatically revokes the credentials in the database.",
        "positive_paraphrase_1": {
            "query": "Explain the security risks of environment variable secrets and how HashiCorp Vault dynamic short-lived credentials solve them.",
            "reason": "Direct paraphrase explaining env var leak vectors and Vault dynamic short-lived credentials.",
        },
        "positive_paraphrase_2": {
            "query": "Why are short-lived dynamic credentials with automated leases superior to static passwords stored in .env files?",
            "reason": "Syntactic variation focusing on short-lived dynamic credentials and automated lease revocation.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure Vault Agent Sidecar Injector in Kubernetes using pod annotations (vault.hashicorp.com/agent-inject)?",
            "response": "Add annotations to the Pod template: `vault.hashicorp.com/agent-inject: 'true'`, `vault.hashicorp.com/role: 'app-role'`, and `vault.hashicorp.com/agent-inject-secret-config: 'secret/data/config'`.",
            "reason": "Asks for Vault Kubernetes sidecar injector annotations rather than comparing dynamic secrets with environment variables.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write Python code using hvac to authenticate to Vault with a Kubernetes JWT token and read a secret.",
            "response": "```python\nimport hvac\nclient = hvac.Client(url='https://vault:8200')\nclient.auth.kubernetes.login(role='my-role', jwt=open('/var/run/secrets/kubernetes.io/serviceaccount/token').read())\nsecret = client.secrets.kv.v2.read_secret_version(path='database')\n```",
            "reason": "Requests specific Python hvac code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does AWS Secrets Manager handle automatic secret rotation using AWS Lambda functions?",
            "response": "AWS Secrets Manager triggers a Lambda rotation function that generates a new password in the secret store, updates the target database, and tests connectivity before marking the version current.",
            "reason": "Asks about AWS Secrets Manager Lambda rotation rather than HashiCorp Vault dynamic secret leases.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is mounting secrets into tmpfs in-memory volumes (/run/secrets) safer than passing them as environment variables?",
            "response": "`tmpfs` volumes reside purely in volatile RAM without touching persistent disk and avoid leaking into `/proc/<PID>/environ`, child process trees, or container inspection metadata.",
            "reason": "Contrasts tmpfs secret volume mounts with environment variables rather than explaining Vault dynamic credentials.",
        },
    },
]
