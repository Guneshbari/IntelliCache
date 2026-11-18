"""
Domain 2: Frameworks and Libraries (21 intent groups).
Covers FastAPI, React, PyTorch, Tokio, Node.js, and Express.
"""

from typing import Any, Dict, List

FL_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 22. FastAPI Dependency Injection
    {
        "intent_group_id": "ig_fl_fastapi_dependency_injection",
        "domain": "frameworks_and_libraries",
        "subtopic": "FastAPI",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "fastapi"}},
        "canonical_query": "How does FastAPI's Depends() system handle dependency injection and database session lifecycle?",
        "canonical_response": "FastAPI resolves dependencies declared with Depends() in route handlers hierarchically before executing the endpoint. When a dependency uses 'yield', FastAPI executes code up to yield, provides the value (such as an async database session) to the handler, and executes the cleanup code in a finally block after the response is sent.",
        "positive_paraphrase_1": {
            "query": "In what way does Depends() in FastAPI manage request-scoped dependencies and yield-based session teardown?",
            "reason": "Direct paraphrase explaining FastAPI's Depends system and yield-based session management.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how FastAPI's dependency injection container handles sub-dependencies and database connection cleanup.",
            "reason": "Syntactic variation focusing on sub-dependencies and connection cleanup.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you override FastAPI dependencies during unit testing using app.dependency_overrides?",
            "response": "In test fixtures, set `app.dependency_overrides[get_db] = get_test_db`, which forces FastAPI to resolve the test database session instead of the production dependency.",
            "reason": "Asks how to override dependencies during testing rather than explaining the runtime lifecycle.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete FastAPI application with an async SQLAlchemy session dependency and a CRUD user endpoint.",
            "response": "```python\nfrom fastapi import FastAPI, Depends\nfrom sqlalchemy.ext.asyncio import AsyncSession\napp = FastAPI()\nasync def get_db(): ...\n@app.get('/users')\nasync def list_users(db: AsyncSession = Depends(get_db)): ...\n```",
            "reason": "Requests concrete CRUD endpoint code rather than explaining dependency injection mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Spring Boot @Autowired dependency injection lifecycle compare to FastAPI's Depends?",
            "response": "Spring Boot creates and manages singleton or prototype beans in an ApplicationContext at container startup, whereas FastAPI resolves dependencies on a per-request basis via call graphs.",
            "reason": "Asks about Spring Boot @Autowired rather than FastAPI Depends.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if an unhandled exception occurs in a FastAPI endpoint regarding yield dependency cleanup?",
            "response": "Even if the endpoint raises an HTTPException or unhandled exception, FastAPI still enters the `finally` block of the yield dependency, guaranteeing database transactions can roll back and sessions close.",
            "reason": "Asks about error rollback behavior inside yield cleanup rather than standard lifecycle.",
        },
    },

    # 23. FastAPI Pydantic Response Model
    {
        "intent_group_id": "ig_fl_fastapi_pydantic_response_model",
        "domain": "frameworks_and_libraries",
        "subtopic": "FastAPI",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "fastapi"}},
        "canonical_query": "How does response_model in FastAPI enforce data filtering and prevent sensitive field leakage?",
        "canonical_response": "FastAPI uses response_model to filter outgoing endpoint data. When a handler returns an ORM object or dict, FastAPI serializes it through the Pydantic response_model, stripping out any fields (such as hashed_password or internal flags) not explicitly declared in the schema, and automatically generating OpenAPI documentation.",
        "positive_paraphrase_1": {
            "query": "Why does setting response_model in FastAPI route decorators guarantee sensitive attributes are not leaked to clients?",
            "reason": "Direct paraphrase asking how response_model prevents sensitive data exposure.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how FastAPI serializes and filters endpoint return values using Pydantic response schemas.",
            "reason": "Syntactic variation focusing on Pydantic response serialization and filtering.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure response_model_exclude_unset to omit default values from FastAPI JSON responses?",
            "response": "Add `@app.get('/path', response_model=MyModel, response_model_exclude_unset=True)` to ensure fields that were not explicitly set on the model instance are omitted from the serialized JSON.",
            "reason": "Asks how to use response_model_exclude_unset rather than explaining security data filtering.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FastAPI route returning a paginated list of items with total_count, items list, and next_cursor.",
            "response": "```python\nclass Page(BaseModel):\n    items: list[Item]\n    total: int\n@app.get('/items', response_model=Page)\nasync def get_items(): ...\n```",
            "reason": "Requests specific pagination schema and route code rather than explanation of response filtering.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does NestJS ClassSerializerInterceptor with @Exclude() compare to FastAPI response_model filtering?",
            "response": "NestJS uses class-transformer decorators like @Exclude() on DTO classes with an interceptor, while FastAPI enforces serialization schemas at the route decorator level via Pydantic.",
            "reason": "Asks about NestJS serialization interceptors rather than FastAPI response_model.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What performance bottleneck occurs when returning large raw lists through FastAPI response_model?",
            "response": "Serializing thousands of complex Pydantic models through response_model introduces significant CPU overhead; returning a custom JSONResponse directly bypasses validation for higher throughput.",
            "reason": "Asks about performance bottlenecks of response_model with large lists (trade-off/limitation).",
        },
    },

    # 24. FastAPI Background Tasks
    {
        "intent_group_id": "ig_fl_fastapi_background_tasks",
        "domain": "frameworks_and_libraries",
        "subtopic": "FastAPI",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "fastapi"}},
        "canonical_query": "How does FastAPI BackgroundTasks execute non-blocking operations after sending the HTTP response?",
        "canonical_response": "FastAPI integrates Starlette's BackgroundTasks class. When an endpoint adds a task via `tasks.add_task(func, *args)`, FastAPI sends the HTTP response immediately to the client and then executes the registered background function asynchronously on the event loop (or in a threadpool if synchronous).",
        "positive_paraphrase_1": {
            "query": "Explain how BackgroundTasks in FastAPI defers execution of tasks until after the HTTP response has returned.",
            "reason": "Direct paraphrase explaining post-response execution of BackgroundTasks.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does FastAPI run post-request background jobs without blocking client response latency?",
            "reason": "Syntactic variation focusing on non-blocking post-request job execution.",
        },
        "hard_negative_diff_operation": {
            "query": "When should you use a dedicated Celery distributed worker queue instead of FastAPI BackgroundTasks?",
            "response": "Use Celery for long-running CPU tasks, mission-critical operations requiring retries across process restarts, or tasks that must survive server crashes, as BackgroundTasks are lost if the process exits.",
            "reason": "Asks when to choose Celery over BackgroundTasks rather than explaining how BackgroundTasks works.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FastAPI route that sends a confirmation email in a BackgroundTask and logs task completion.",
            "response": "```python\n@app.post('/register')\nasync def register(email: str, bg: BackgroundTasks):\n    bg.add_task(send_email, email)\n    return {'status': 'queued'}\n```",
            "reason": "Requests concrete email notification endpoint code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Django async tasks with channels compare to FastAPI BackgroundTasks?",
            "response": "Django Channels handles async WebSockets and background worker consumers via Redis channels, whereas FastAPI BackgroundTasks is an in-process mechanism tied to the HTTP response cycle.",
            "reason": "Asks about Django Channels rather than FastAPI BackgroundTasks.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens to in-flight FastAPI BackgroundTasks if the Uvicorn process receives a SIGTERM signal?",
            "response": "Uvicorn attempts a graceful shutdown period, but if background tasks exceed the timeout or if the process is forcefully killed, unfinished BackgroundTasks are terminated without recovery.",
            "reason": "Asks about process shutdown failure modes rather than normal execution mechanics.",
        },
    },

    # 25. FastAPI Lifespan Context Manager
    {
        "intent_group_id": "ig_fl_fastapi_lifespan_handlers",
        "domain": "frameworks_and_libraries",
        "subtopic": "FastAPI",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "fastapi"}},
        "canonical_query": "How does the modern lifespan async context manager replace deprecated on_event in FastAPI?",
        "canonical_response": "The lifespan context manager uses an async generator: code before 'yield' runs at application startup (initializing database connection pools, ML model weights), and code after 'yield' runs at application shutdown (closing connections, freeing GPU memory), replacing fragmented on_event('startup') and on_event('shutdown') handlers.",
        "positive_paraphrase_1": {
            "query": "Why is the async lifespan context manager preferred over @app.on_event in modern FastAPI applications?",
            "reason": "Direct paraphrase explaining why lifespan async generators replace deprecated on_event handlers.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how FastAPI coordinates application startup and graceful shutdown using the lifespan parameter.",
            "reason": "Syntactic variation focusing on startup and shutdown coordination via lifespan.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you share initialized resources from the lifespan context manager using state dictionary in FastAPI?",
            "response": "Pass state into yield: `yield {'db_pool': pool}`, which FastAPI exposes as `request.state.db_pool` in endpoints and middleware.",
            "reason": "Asks how to share state through request.state rather than explaining lifespan architecture.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete FastAPI application using lifespan to connect to an async Redis client and close it on shutdown.",
            "response": "```python\n@asynccontextmanager\nasync def lifespan(app: FastAPI):\n    redis = await aioredis.from_url('redis://localhost')\n    yield {'redis': redis}\n    await redis.close()\napp = FastAPI(lifespan=lifespan)\n```",
            "reason": "Requests specific Redis lifespan code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Express.js handle server shutdown signals (SIGINT, SIGTERM) compared to FastAPI lifespan?",
            "response": "Express requires manually attaching listeners to `process.on('SIGTERM')` and invoking `server.close()`, while FastAPI handles OS signals automatically through ASGI server lifespan events.",
            "reason": "Asks about Express process signal listeners rather than FastAPI lifespan.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if an exception is raised in the startup portion (before yield) of a FastAPI lifespan handler?",
            "response": "If an exception is raised before yield, the ASGI server aborts startup immediately and terminates with a non-zero exit code without serving any HTTP traffic.",
            "reason": "Asks about startup abort behavior on failure rather than normal lifespan execution.",
        },
    },

    # 26. FastAPI CORSMiddleware
    {
        "intent_group_id": "ig_fl_fastapi_middleware_cors",
        "domain": "frameworks_and_libraries",
        "subtopic": "FastAPI",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "fastapi"}},
        "canonical_query": "How does FastAPI CORSMiddleware intercept browser preflight OPTIONS requests and manage Access-Control headers?",
        "canonical_response": "CORSMiddleware intercepts incoming HTTP requests before they reach endpoints. For OPTIONS preflight requests, it verifies the Origin against allow_origins, returning appropriate Access-Control-Allow-Origin, Methods, and Headers. For regular requests, it appends the allowed CORS headers to the response.",
        "positive_paraphrase_1": {
            "query": "Explain how CORSMiddleware in FastAPI responds to preflight OPTIONS checks and controls allowed origins.",
            "reason": "Direct paraphrase explaining CORS preflight handling and header injection in FastAPI.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does adding CORSMiddleware to a FastAPI app resolve cross-origin resource sharing restrictions in web browsers?",
            "reason": "Syntactic variation asking how CORSMiddleware resolves cross-origin restrictions.",
        },
        "hard_negative_diff_operation": {
            "query": "Why is setting allow_origins=['*'] with allow_credentials=True invalid and rejected by modern browsers?",
            "response": "The CORS specification explicitly prohibits wildcard '*' origins when credentials (cookies, authorization headers) are allowed, because it would expose authenticated user data to arbitrary domains.",
            "reason": "Asks about the browser credential wildcard restriction rather than explaining middleware mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Python code to configure CORSMiddleware on a FastAPI instance allowing localhost:3000 with GET and POST methods.",
            "response": "```python\napp.add_middleware(CORSMiddleware, allow_origins=['http://localhost:3000'], allow_methods=['GET', 'POST'], allow_headers=['*'])\n```",
            "reason": "Requests specific middleware configuration code rather than explanation of CORS mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Node.js cors npm package handle origin regex patterns compared to Starlette CORSMiddleware?",
            "response": "The cors npm package natively accepts RegExp objects for dynamic origin matching, whereas Starlette requires allow_origin_regex as a separate string parameter.",
            "reason": "Asks about Node.js cors npm regex support rather than FastAPI CORSMiddleware.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does CORS configuration NOT protect your API against non-browser clients like curl or Python requests?",
            "response": "CORS is strictly a client-side security sandbox enforced by web browsers. Non-browser HTTP clients like curl do not execute browser preflight checks or enforce origin restrictions.",
            "reason": "Asks why CORS fails to restrict non-browser clients (security boundary limitation).",
        },
    },

    # 27. React useEffect Dependencies
    {
        "intent_group_id": "ig_fl_react_use_effect_deps",
        "domain": "frameworks_and_libraries",
        "subtopic": "React",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "react"}},
        "canonical_query": "How does the React useEffect dependency array determine when side effects and cleanup functions run?",
        "canonical_response": "React compares values in the dependency array across renders using Object.is. If omitted, the effect runs after every render. If empty ([]), it runs once on mount and cleans up on unmount. If populated, React runs cleanup for the previous render and re-executes the effect whenever any dependency value changes.",
        "positive_paraphrase_1": {
            "query": "Explain how React uses Object.is shallow comparison on the useEffect dependency array to trigger effect execution.",
            "reason": "Direct paraphrase explaining dependency comparison and effect timing in React.",
        },
        "positive_paraphrase_2": {
            "query": "In what manner does the dependency array control mount, update, and cleanup cycles in React useEffect?",
            "reason": "Syntactic variation asking how dependencies govern effect lifecycles.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you prevent infinite render loops caused by referencing object or array literals inside useEffect dependencies?",
            "reason": "Asks how to solve the infinite render loop problem rather than explaining the dependency array rules.",
            "response": "Wrap the object or array in useMemo, or extract primitive properties into the dependency array, ensuring reference equality between renders.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a React custom hook useDebounce that uses useEffect and setTimeout to debounce a search query string.",
            "response": "```javascript\nfunction useDebounce(value, delay) {\n  const [debounced, setDebounced] = useState(value);\n  useEffect(() => {\n    const h = setTimeout(() => setDebounced(value), delay);\n    return () => clearTimeout(h);\n  }, [value, delay]);\n  return debounced;\n}\n```",
            "reason": "Requests specific debouncing hook implementation code rather than explanation of useEffect mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Vue 3 watchEffect automatic dependency tracking compare to React's explicit useEffect dependencies?",
            "response": "Vue 3 tracks reactive getters accessed during effect execution automatically via proxies, whereas React requires developers to explicitly declare all referenced values in the dependency array.",
            "reason": "Asks about Vue 3 watchEffect dependency tracking rather than React useEffect.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the consequence of suppressing ESLint react-hooks/exhaustive-deps rule in React components?",
            "response": "Suppressing exhaustive-deps leads to stale closures, where the effect reads outdated state or prop values from older renders, causing subtle and hard-to-diagnose bugs.",
            "reason": "Asks about the dangers of disabling lint rules (stale closure bug) rather than normal dependency lifecycle.",
        },
    },

    # 28. React useMemo vs useCallback
    {
        "intent_group_id": "ig_fl_react_use_memo_callback",
        "domain": "frameworks_and_libraries",
        "subtopic": "React",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "react"}},
        "canonical_query": "What is the distinction between useMemo and useCallback for performance optimization in React?",
        "canonical_response": "useMemo caches the computed result of calling a function (useMemo(() => compute(), [deps])), avoiding expensive recalculations between renders. useCallback caches the function instance itself (useCallback(fn, [deps])), preventing child components wrapped in React.memo from re-rendering due to changing function references.",
        "positive_paraphrase_1": {
            "query": "How do useMemo and useCallback differ regarding whether they memoize computed values or function references in React?",
            "reason": "Direct paraphrase asking to contrast memoizing values vs memoizing function references.",
        },
        "positive_paraphrase_2": {
            "query": "Explain the respective use cases for useMemo and useCallback when optimizing child re-renders in React.",
            "reason": "Syntactic variation focusing on use cases for value vs callback memoization.",
        },
        "hard_negative_diff_operation": {
            "query": "When does premature use of useMemo and useCallback actually degrade performance in React applications?",
            "response": "Overusing memoization adds memory overhead for dependency arrays and executes comparison checks on every render; for trivial calculations, the overhead exceeds the cost of re-rendering.",
            "reason": "Asks when memoization harms performance rather than explaining the difference between the hooks.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a React component demonstrating React.memo on a child button with a memoized onClick callback via useCallback.",
            "response": "```javascript\nconst Child = React.memo(({ onClick }) => <button onClick={onClick}>Click</button>);\nfunction Parent() {\n  const [c, setC] = useState(0);\n  const handleClick = useCallback(() => console.log('clicked'), []);\n  return <Child onClick={handleClick} />;\n}\n```",
            "reason": "Requests specific component code demonstrating React.memo and useCallback rather than conceptual distinction.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Svelte compiler-based reactivity eliminate the need for hooks like useMemo and useCallback?",
            "response": "Svelte compiles reactive declarations ($:) into surgical DOM updates at build time without a virtual DOM, eliminating runtime function identity checks and hook dependency arrays.",
            "reason": "Asks about Svelte build-time reactivity rather than React memoization hooks.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does useCallback(fn, deps) produce zero performance benefit if child components are NOT wrapped in React.memo?",
            "response": "By default, React re-renders all child components when a parent renders regardless of prop equality; preserving function reference identity provides no benefit unless the child skips re-renders via React.memo.",
            "reason": "Asks why useCallback is ineffective without React.memo (ineffective edge case).",
        },
    },

    # 29. React Custom Hooks
    {
        "intent_group_id": "ig_fl_react_custom_hooks",
        "domain": "frameworks_and_libraries",
        "subtopic": "React",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "react"}},
        "canonical_query": "How do React custom hooks enable stateful logic reuse without sharing state between component instances?",
        "canonical_response": "Custom hooks are JavaScript functions whose names start with 'use' and can call other hooks (useState, useEffect). They allow developers to extract and reuse stateful logic (such as fetching data or managing window size). Each component calling the hook receives its own isolated, independent instance of the internal state.",
        "positive_paraphrase_1": {
            "query": "Why does extracting reusable logic into a custom React hook give each consuming component independent state?",
            "reason": "Direct paraphrase explaining stateful logic reuse with isolated instance state.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how custom hooks compose built-in React hooks to encapsulate component behavior cleanly.",
            "reason": "Syntactic variation addressing encapsulation and composition of built-in hooks.",
        },
        "hard_negative_diff_operation": {
            "query": "What are the rules of hooks that must be strictly followed when authoring custom React hooks?",
            "response": "Hooks must only be called at the top level (never inside loops, conditions, or nested functions) and only from React function components or other custom hooks.",
            "reason": "Asks about the rules of hooks rather than explaining how custom hooks reuse stateful logic.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a TypeScript custom hook useLocalStorage that synchronizes state with browser localStorage.",
            "response": "```typescript\nfunction useLocalStorage<T>(key: string, initial: T) {\n  const [val, setVal] = useState<T>(() => {\n    const item = localStorage.getItem(key);\n    return item ? JSON.parse(item) : initial;\n  });\n  // sync to localStorage\n  return [val, setVal] as const;\n}\n```",
            "reason": "Requests specific useLocalStorage hook code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Vue 3 composables compare to React custom hooks in terms of execution model?",
            "response": "Vue composables execute once during component setup to establish reactive state tracking, while React custom hooks re-execute on every component render.",
            "reason": "Asks about Vue composables vs React custom hooks.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What common misconception causes developers to expect custom hooks to act like global state stores?",
            "response": "Developers sometimes mistake custom hooks for global singletons, incorrectly expecting changes in one component to update state in other components calling the same hook.",
            "reason": "Asks about the singleton misconception rather than explaining independent state reuse.",
        },
    },

    # 30. React Server Components
    {
        "intent_group_id": "ig_fl_react_server_components",
        "domain": "frameworks_and_libraries",
        "subtopic": "React",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "react"}},
        "canonical_query": "What is the architectural difference between React Server Components (RSC) and traditional SSR?",
        "canonical_response": "Traditional SSR renders components to HTML on the server and sends both HTML and the complete JavaScript bundle to the browser for client-side hydration. React Server Components execute exclusively on the server, have direct database access, emit a binary virtual DOM stream, and ship zero JavaScript bytes to client bundles.",
        "positive_paraphrase_1": {
            "query": "How do React Server Components differ from traditional server-side rendering regarding client bundle size and hydration?",
            "reason": "Direct paraphrase contrasting RSC with traditional SSR on bundle size and hydration.",
        },
        "positive_paraphrase_2": {
            "query": "Explain the architectural advantages of RSC over standard SSR in modern Next.js applications.",
            "reason": "Syntactic variation focusing on architectural advantages of Server Components.",
        },
        "hard_negative_diff_operation": {
            "query": "When must you add the 'use client' directive to a React component in a Next.js App Router project?",
            "response": "Add 'use client' when a component requires browser interactivity, event listeners (onClick, onChange), React state hooks (useState, useReducer), or browser APIs (localStorage).",
            "reason": "Asks when to use the 'use client' directive rather than contrasting RSC with SSR.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Next.js App Router server component that fetches user data directly from Prisma ORM and renders a table.",
            "response": "```tsx\nimport prisma from '@/lib/prisma';\nexport default async function UsersPage() {\n  const users = await prisma.user.findMany();\n  return <ul>{users.map(u => <li key={u.id}>{u.name}</li>)}</ul>;\n}\n```",
            "reason": "Requests specific Prisma server component page code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Astro Islands architecture compare to React Server Components for zero-JS landing pages?",
            "response": "Astro renders static HTML by default and hydrates individual interactive islands via client directives, whereas RSC integrates server and client trees within a single React runtime protocol.",
            "reason": "Asks about Astro Islands architecture rather than React Server Components.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can you NOT pass functions or event handlers as props from a Server Component to a Client Component?",
            "response": "Props passed across the server-client boundary must be serializable (JSON-like) because they are transmitted over the network in the RSC flight stream; functions cannot be serialized.",
            "reason": "Asks about serialization limitations across server-client prop boundaries.",
        },
    },

    # 31. React Context API
    {
        "intent_group_id": "ig_fl_react_context_api",
        "domain": "frameworks_and_libraries",
        "subtopic": "React",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "react"}},
        "canonical_query": "How does the React Context API resolve prop drilling across deep component hierarchies?",
        "canonical_response": "React Context provides a way to pass data through the component tree without manually passing props down through intermediate elements. A Context.Provider supplies a value, and any descendant component can consume that value directly using the useContext hook, decoupling producers from intermediate consumers.",
        "positive_paraphrase_1": {
            "query": "Explain how React Context and useContext eliminate the need to thread props through intermediate components.",
            "reason": "Direct paraphrase explaining how Context solves prop drilling.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Context.Provider pattern enable global or subtree state sharing in React?",
            "reason": "Syntactic variation focusing on Context.Provider subtree state sharing.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you split a React Context into separate state and dispatch contexts to prevent unnecessary re-renders?",
            "response": "Create one context for the state value and a second context for dispatch/setter functions; components that only dispatch actions subscribe to the dispatch context and do not re-render on state changes.",
            "reason": "Asks how to optimize context splitting rather than explaining prop drilling resolution.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete ThemeProvider component using React Context with light/dark toggle and a useTheme consumer hook.",
            "response": "```javascript\nconst ThemeCtx = createContext();\nexport function ThemeProvider({ children }) {\n  const [theme, setTheme] = useState('light');\n  return <ThemeCtx.Provider value={{ theme, toggle: () => setTheme(t => t === 'light' ? 'dark' : 'light') }}>{children}</ThemeCtx.Provider>;\n}\n```",
            "reason": "Requests specific theme provider code implementation rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Zustand or Redux Toolkit state management compare to React Context for large applications?",
            "response": "Zustand uses external stores with selector-based subscriptions that re-render only components whose selected state slice changed, whereas Context re-renders all consumers on any value change.",
            "reason": "Asks about Zustand/Redux vs Context rather than explaining Context itself.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes all consumers of a React Context to re-render even if they only read an unchanged property?",
            "response": "Passing a newly created object literal directly to `<Provider value={{ a, b }}>` creates a new object reference on every render, failing Object.is equality and triggering all consumers to re-render.",
            "reason": "Asks about the object literal reference re-render trap in Context providers.",
        },
    },

    # 32. React Error Boundaries
    {
        "intent_group_id": "ig_fl_react_error_boundaries",
        "domain": "frameworks_and_libraries",
        "subtopic": "React",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "react"}},
        "canonical_query": "How do React Error Boundaries catch rendering exceptions and prevent entire application crashes?",
        "canonical_response": "Error Boundaries are class components that implement componentDidCatch and/or static getDerivedStateFromError. When an unhandled error occurs during rendering, lifecycle methods, or constructors in their child tree, the boundary catches it, updates state to display a fallback UI, and prevents the entire component tree from unmounting.",
        "positive_paraphrase_1": {
            "query": "Explain how class components implementing getDerivedStateFromError catch render phase errors in React.",
            "reason": "Direct paraphrase explaining how getDerivedStateFromError catches rendering errors.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do Error Boundaries isolate UI failures to a specific subtree in React applications?",
            "reason": "Syntactic variation focusing on isolating UI failures to specific subtrees.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you catch asynchronous errors inside event handlers and fetch calls in React components?",
            "response": "Error Boundaries do not catch errors in event handlers or async code; you must handle them explicitly using try/catch blocks or promise .catch() within the handler.",
            "reason": "Asks how to catch async errors in event handlers rather than explaining Error Boundaries.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete React ErrorBoundary class component in TypeScript with retry capability and error reporting.",
            "response": "```typescript\nclass ErrorBoundary extends React.Component<{ fallback: ReactNode }, { hasError: boolean }> {\n  state = { hasError: false };\n  static getDerivedStateFromError() { return { hasError: true }; }\n  render() { return this.state.hasError ? this.props.fallback : this.props.children; }\n}\n```",
            "reason": "Requests specific TypeScript ErrorBoundary component code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Vue 3 onErrorCaptured component hook compare to React Error Boundaries?",
            "response": "Vue 3 provides `onErrorCaptured` lifecycle hook directly in Composition API components, allowing functional components to catch child errors without requiring class syntax.",
            "reason": "Asks about Vue 3 onErrorCaptured rather than React Error Boundaries.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Which types of errors are explicitly NOT caught by React Error Boundaries?",
            "response": "Error Boundaries do not catch errors inside event handlers, asynchronous callbacks (setTimeout, requestAnimationFrame), server-side rendering, or errors thrown in the boundary component itself.",
            "reason": "Asks for errors NOT caught by boundaries (negation / limitations).",
        },
    },

    # 33. PyTorch Autograd Engine
    {
        "intent_group_id": "ig_fl_pytorch_tensor_autograd",
        "domain": "frameworks_and_libraries",
        "subtopic": "PyTorch",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "pytorch"}},
        "canonical_query": "How does the PyTorch autograd engine construct dynamic computational graphs for backpropagation?",
        "canonical_response": "PyTorch builds dynamic computational graphs on-the-fly during the forward pass (define-by-run). Every tensor with requires_grad=True tracks operations via its grad_fn attribute. When backward() is called on the scalar loss, autograd traverses the DAG in reverse topological order, applying the chain rule to accumulate gradients into tensor.grad.",
        "positive_paraphrase_1": {
            "query": "Explain how PyTorch builds DAG computational graphs and computes gradients during the backward pass.",
            "reason": "Direct paraphrase explaining dynamic graph construction and backward gradient accumulation.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does autograd trace tensor operations via grad_fn to execute reverse-mode automatic differentiation?",
            "reason": "Syntactic variation focusing on grad_fn tracing and reverse-mode automatic differentiation.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you disable gradient tracking using torch.no_grad() and torch.inference_mode() during model evaluation?",
            "response": "Wrap evaluation code in `with torch.inference_mode():`, which disables autograd tracking and optimizes tensor view tracking, reducing memory consumption and accelerating forward passes.",
            "reason": "Asks how to disable gradient tracking rather than explaining how autograd builds graphs.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a PyTorch training loop for linear regression using nn.MSELoss and torch.optim.SGD with loss.backward().",
            "response": "```python\nfor epoch in range(100):\n    optimizer.zero_grad()\n    loss = criterion(model(x), y)\n    loss.backward()\n    optimizer.step()\n```",
            "reason": "Requests specific training loop code rather than explanation of autograd graph mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does TensorFlow static graph mode (tf.function) compare to PyTorch dynamic autograd graphs?",
            "response": "TensorFlow compiles Python code into a serialized static graph (GraphDef) before execution for compiler optimization, whereas PyTorch reconstructs a new dynamic graph on every forward pass.",
            "reason": "Asks about TensorFlow static graphs rather than PyTorch autograd.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if you forget to call optimizer.zero_grad() before loss.backward() in PyTorch?",
            "response": "PyTorch accumulates gradients into tensor.grad by default; omitting zero_grad() causes gradients from successive batches to add together, resulting in incorrect parameter updates.",
            "reason": "Asks about the gradient accumulation bug from omitting zero_grad().",
        },
    },

    # 34. PyTorch DataLoader Multiprocessing
    {
        "intent_group_id": "ig_fl_pytorch_dataloader_workers",
        "domain": "frameworks_and_libraries",
        "subtopic": "PyTorch",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "pytorch"}},
        "canonical_query": "How does PyTorch DataLoader utilize num_workers and pin_memory to accelerate GPU training throughput?",
        "canonical_response": "DataLoader uses num_workers to spawn background worker processes via multiprocessing, prefetching and batching data asynchronously while the GPU computes. pin_memory=True allocates page-locked (pinned) host memory, enabling fast asynchronous direct memory access (DMA) transfers from CPU RAM to GPU VRAM.",
        "positive_paraphrase_1": {
            "query": "Explain how multi-worker prefetching and pinned host memory in PyTorch DataLoader minimize GPU starvation.",
            "reason": "Direct paraphrase explaining multi-worker prefetching and pin_memory.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do num_workers and pin_memory in torch.utils.data.DataLoader optimize the CPU-to-GPU data pipeline?",
            "reason": "Syntactic variation addressing data pipeline optimization between CPU and GPU.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you write a custom collate_fn in PyTorch DataLoader to handle variable-length padded sequences?",
            "response": "Define a function that accepts a list of dataset tuples, extracts inputs, and uses `torch.nn.utils.rnn.pad_sequence(batch, batch_first=True)` to pad sequences to matching lengths.",
            "reason": "Asks how to write custom collate_fn padding code rather than explaining multiprocessing/pin_memory.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a PyTorch image dataset with Albumentations augmentations wrapped in a DataLoader with 4 workers.",
            "response": "```python\nloader = DataLoader(dataset, batch_size=64, num_workers=4, pin_memory=True, shuffle=True)\n```",
            "reason": "Requests specific image dataset code rather than explaining DataLoader multiprocessing.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does TensorFlow tf.data.Dataset.prefetch(tf.data.AUTOTUNE) compare to PyTorch DataLoader workers?",
            "response": "tf.data utilizes C++ background threads with runtime buffer tuning via AUTOTUNE, while PyTorch relies on Python multiprocessing with explicit worker process counts.",
            "reason": "Asks about TensorFlow tf.data prefetch vs PyTorch DataLoader.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does setting num_workers too high in PyTorch DataLoader often cause training to slow down?",
            "response": "Excessive worker processes saturate CPU cores and memory bandwidth, increasing inter-process communication (IPC) serialization overhead and shared-memory contention.",
            "reason": "Asks about performance degradation from over-allocating workers (diminishing returns).",
        },
    },

    # 35. PyTorch DistributedDataParallel
    {
        "intent_group_id": "ig_fl_pytorch_ddp_training",
        "domain": "frameworks_and_libraries",
        "subtopic": "PyTorch",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "pytorch"}},
        "canonical_query": "How does PyTorch DistributedDataParallel (DDP) synchronize gradients across multiple GPUs using AllReduce?",
        "canonical_response": "DDP spawns a separate process per GPU. Each process holds a full replica of model parameters and computes forward and backward passes on its local batch partition. During the backward pass, DDP overlaps computation and communication using ring AllReduce (via NCCL), averaging gradients across GPUs asynchronously.",
        "positive_paraphrase_1": {
            "query": "Explain how DDP overlaps gradient AllReduce communication with backpropagation across multi-GPU clusters.",
            "reason": "Direct paraphrase explaining DDP communication overlap and AllReduce gradient synchronization.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does PyTorch DistributedDataParallel achieve higher training throughput than single-process DataParallel?",
            "reason": "Syntactic variation contrasting multi-process DDP with single-process DataParallel.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you initialize a DDP process group using torch.distributed.init_process_group with NCCL backend?",
            "response": "```python\ntorch.distributed.init_process_group(backend='nccl', init_method='env://')\ntorch.cuda.set_device(local_rank)\n```",
            "reason": "Asks how to write the process group initialization code rather than explaining AllReduce mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a launch command and script using torchrun --nproc_per_node=4 to train a ResNet model with DDP.",
            "response": "```bash\ntorchrun --nproc_per_node=4 train_ddp.py\n```",
            "reason": "Requests specific torchrun command and script rather than explaining DDP gradient synchronization.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does DeepSpeed ZeRO stage 3 parameter partitioning differ from standard PyTorch DDP?",
            "response": "DDP replicates full optimizer states, gradients, and model parameters on every GPU, while DeepSpeed ZeRO-3 partitions all three across GPUs, drastically reducing per-device VRAM.",
            "reason": "Asks about DeepSpeed ZeRO-3 partitioning rather than standard DDP.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does setting find_unused_parameters=True in PyTorch DDP introduce substantial performance overhead?",
            "response": "find_unused_parameters=True forces DDP to traverse the entire autograd graph on every iteration to detect unreferenced parameters, adding CPU overhead and delaying gradient bucket AllReduce.",
            "reason": "Asks about the performance cost of find_unused_parameters (bottleneck analysis).",
        },
    },

    # 36. PyTorch Checkpointing
    {
        "intent_group_id": "ig_fl_pytorch_model_checkpointing",
        "domain": "frameworks_and_libraries",
        "subtopic": "PyTorch",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "pytorch"}},
        "canonical_query": "Why should you save state_dict instead of the complete model object when checkpointing PyTorch models?",
        "canonical_response": "Saving `model.state_dict()` serializes only parameter tensors and persistent buffers into a dictionary. Saving the entire model object with `torch.save(model)` pickles the Python class definition, which breaks when code refactors change directory paths, class names, or library versions.",
        "positive_paraphrase_1": {
            "query": "What is the architectural reason for saving model.state_dict() rather than pickling the full model in PyTorch?",
            "reason": "Direct paraphrase explaining why state_dict is preferred over pickling the full model.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how serializing state_dict ensures model checkpoints remain decoupled from specific Python class definitions.",
            "reason": "Syntactic variation focusing on decoupling parameter state from class definitions.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you resume training from a checkpoint including optimizer state_dict, epoch number, and loss?",
            "response": "Save a dictionary `{'epoch': e, 'model': model.state_dict(), 'opt': opt.state_dict()}` and restore with `model.load_state_dict(ckpt['model'])` and `opt.load_state_dict(ckpt['opt'])`.",
            "reason": "Asks how to package and restore training state rather than explaining why state_dict is used.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a PyTorch function that loads weights into a model with strict=False to ignore missing head layers.",
            "response": "```python\ndef load_backbone(model, path):\n    weights = torch.load(path, map_location='cpu')\n    model.load_state_dict(weights, strict=False)\n```",
            "reason": "Requests specific weight loading code with strict=False rather than explaining checkpointing design.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Safetensors format compare to PyTorch standard pickle-based .pt checkpoint files?",
            "response": "Safetensors uses a simple binary format with zero-copy memory mapping that completely eliminates arbitrary code execution vulnerabilities present in Python pickle files.",
            "reason": "Asks about Safetensors vs pickle rather than PyTorch state_dict vs full model saving.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What security vulnerability arises when loading untrusted PyTorch .pt model checkpoints using torch.load?",
            "response": "Standard `torch.load()` relies on Python's pickle module; malicious pickle files can execute arbitrary shell commands or malicious payloads during deserialization.",
            "reason": "Asks about pickle security vulnerabilities in torch.load rather than state_dict architecture.",
        },
    },

    # 37. PyTorch Custom Dataset
    {
        "intent_group_id": "ig_fl_pytorch_custom_dataset",
        "domain": "frameworks_and_libraries",
        "subtopic": "PyTorch",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "pytorch"}},
        "canonical_query": "How does subclassing torch.utils.data.Dataset with __len__ and __getitem__ enable map-style data access in PyTorch?",
        "canonical_response": "Map-style datasets implement `__len__`, which returns total sample count, and `__getitem__(index)`, which returns a single sample for a given integer key. This decoupling allows PyTorch Samplers to generate shuffled or weighted indices independently of data loading and preprocessing logic.",
        "positive_paraphrase_1": {
            "query": "Explain how implementing __len__ and __getitem__ in a custom Dataset supports index-based sampling in PyTorch.",
            "reason": "Direct paraphrase explaining how __len__ and __getitem__ support map-style sampling.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does subclassing Dataset in PyTorch separate data indexing from batch sampling and loading?",
            "reason": "Syntactic variation addressing separation of data indexing from sampling.",
        },
        "hard_negative_diff_operation": {
            "query": "When should you use torch.utils.data.IterableDataset instead of a map-style Dataset?",
            "response": "Use IterableDataset for streaming data from network sockets, infinite generators, or massive files where random index access is expensive or sample count cannot be determined.",
            "reason": "Asks when to choose IterableDataset over map-style Dataset.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a PyTorch Dataset class that loads audio waveform files from a directory and converts them to Mel-spectrograms.",
            "response": "```python\nclass AudioDataset(Dataset):\n    def __init__(self, paths): self.paths = paths\n    def __len__(self): return len(self.paths)\n    def __getitem__(self, i): return load_mel(self.paths[i])\n```",
            "reason": "Requests specific audio dataset code rather than explaining map-style Dataset mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does HuggingFace Datasets Apache Arrow memory-mapped dataset compare to PyTorch custom Datasets?",
            "response": "HuggingFace Datasets uses Arrow memory-mapping to stream zero-copy rows directly from disk cache, avoiding Python object allocation overhead present in custom PyTorch Datasets.",
            "reason": "Asks about HuggingFace Arrow datasets rather than PyTorch custom Datasets.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a custom PyTorch Dataset contains open file handles or database connections during multi-process loading?",
            "response": "When DataLoader forks worker processes, open file handles or socket descriptors are duplicated, leading to race conditions, file corruption, or connection resets across workers.",
            "reason": "Asks about resource descriptor duplication bugs in DataLoader workers.",
        },
    },

    # 38. Tokio spawn_blocking
    {
        "intent_group_id": "ig_fl_tokio_spawn_blocking",
        "domain": "frameworks_and_libraries",
        "subtopic": "Tokio",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "tokio"}},
        "canonical_query": "Why must CPU-heavy or synchronous I/O tasks in Tokio be offloaded to tokio::task::spawn_blocking?",
        "canonical_response": "Tokio's async runtime relies on cooperative multitasking across a small pool of worker threads (matching CPU core count). Running synchronous I/O or intensive CPU computations inside an async task blocks the worker thread from polling other tasks. `spawn_blocking` offloads work to a dedicated thread pool designed for blocking operations.",
        "positive_paraphrase_1": {
            "query": "Explain how tokio::task::spawn_blocking prevents thread starvation on Tokio's async worker threads.",
            "reason": "Direct paraphrase explaining thread starvation prevention via spawn_blocking.",
        },
        "positive_paraphrase_2": {
            "query": "Why does running synchronous blocking code directly inside an async fn stall the Tokio event loop?",
            "reason": "Syntactic variation focusing on worker thread stalling caused by blocking calls.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure the maximum blocking thread pool capacity in Tokio using Builder::max_blocking_threads?",
            "response": "Configure the runtime with `tokio::runtime::Builder::new_multi_thread().max_blocking_threads(512).build()`, capping concurrent blocking threads.",
            "reason": "Asks how to configure runtime thread limits rather than explaining why spawn_blocking is needed.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Rust async function that hashes a 500MB file using rayon inside tokio::task::spawn_blocking.",
            "response": "```rust\nasync fn hash_file(data: Vec<u8>) -> String {\n    tokio::task::spawn_blocking(move || compute_hash(&data)).await.unwrap()\n}\n```",
            "reason": "Requests specific hashing code using spawn_blocking rather than explaining runtime starvation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Go runtime handle blocking system calls without an explicit spawn_blocking API?",
            "response": "Go's runtime monitors OS threads with sysmon; when a thread blocks on a syscall, the runtime automatically detaches the logical P to run on another thread without explicit developer intervention.",
            "reason": "Asks about Go automatic syscall handoff rather than Tokio spawn_blocking.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if you spawn unbounded numbers of spawn_blocking tasks concurrently in Tokio?",
            "response": "Spawning unbounded blocking tasks creates hundreds of OS threads up to the limit (default 512), causing high context switching overhead and potentially exhausting OS thread limits.",
            "reason": "Asks about thread exhaustion from unbounded spawn_blocking calls (failure mode).",
        },
    },

    # 39. Tokio select! Macro
    {
        "intent_group_id": "ig_fl_tokio_select_macro",
        "domain": "frameworks_and_libraries",
        "subtopic": "Tokio",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "tokio"}},
        "canonical_query": "How does the tokio::select! macro race multiple asynchronous futures concurrently on a single thread?",
        "canonical_response": "The `tokio::select!` macro polls all specified futures simultaneously on the current thread. As soon as the first future completes with Ready, select! drops the remaining unfinished futures (cancelling them) and executes the corresponding handler branch.",
        "positive_paraphrase_1": {
            "query": "Explain how tokio::select! executes multiple futures simultaneously and drops losing branches upon completion.",
            "reason": "Direct paraphrase explaining racing futures and cancellation of losing branches.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does Tokio's select! macro coordinate concurrent async operations with cancellation on completion?",
            "reason": "Syntactic variation focusing on concurrency and cancellation semantics in select!.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you use biased mode in tokio::select! to enforce strict branch priority order?",
            "response": "Add `biased;` at the top of the select! block. This instructs the macro to poll branches in the exact lexical order declared rather than using pseudo-random branch polling.",
            "reason": "Asks how to use biased mode for prioritization rather than explaining general select! racing.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Rust function that uses tokio::select! to race a network request future against a 5-second sleep timeout.",
            "response": "```rust\ntokio::select! {\n    res = fetch_data() => Ok(res),\n    _ = tokio::time::sleep(Duration::from_secs(5)) => Err(\"Timeout\"),\n}\n```",
            "reason": "Requests specific timeout race code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does JavaScript Promise.race() compare to Rust tokio::select! regarding cancellation of losing operations?",
            "response": "Promise.race() ignores losing promises but cannot cancel them; in Rust, futures are lazy and dropping an unfinished future immediately cancels its execution.",
            "reason": "Asks about JavaScript Promise.race vs Tokio select! cancellation.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes cancel-safety bugs when using tokio::select! inside an async loop?",
            "response": "If a future that holds partially read state (like AsyncRead::read) is dropped by select! because another branch completed, unread bytes are discarded, causing data loss.",
            "reason": "Asks about cancel-safety bugs in select! loops (failure mode).",
        },
    },

    # 40. Tokio Broadcast Channel
    {
        "intent_group_id": "ig_fl_tokio_broadcast_channel",
        "domain": "frameworks_and_libraries",
        "subtopic": "Tokio",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "tokio"}},
        "canonical_query": "How does tokio::sync::broadcast manage multi-producer multi-consumer message distribution and slow receiver lag?",
        "canonical_response": "Tokio's broadcast channel implements a bounded ring buffer where messages sent by any producer are cloned to all active receivers. If a slow receiver falls behind and the ring buffer wraps around before it reads messages, the receiver receives a `RecvError::Lagged(count)` indicating missed messages.",
        "positive_paraphrase_1": {
            "query": "Explain how Tokio's broadcast channel handles slow consumers via RecvError::Lagged.",
            "reason": "Direct paraphrase explaining ring buffer wrapping and Lagged error handling in broadcast.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does tokio::sync::broadcast broadcast values to multiple subscriber tasks in Rust?",
            "reason": "Syntactic variation focusing on multi-subscriber broadcast distribution.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you use tokio::sync::mpsc for single-consumer multi-producer queues instead of broadcast?",
            "response": "Use `mpsc::channel(buffer)` where each message is delivered to exactly one receiver task, providing load-balancing queue semantics rather than pub/sub fanout.",
            "reason": "Asks how to use mpsc queue channels rather than explaining broadcast ring buffers.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Rust chat server using tokio::sync::broadcast with 3 simulated client tasks receiving messages.",
            "response": "```rust\nlet (tx, mut rx1) = broadcast::channel(16);\nlet mut rx2 = tx.subscribe();\ntx.send(\"hello\").unwrap();\n```",
            "reason": "Requests specific chat server code rather than explaining broadcast mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Go fan-out channel distribution compare to Tokio broadcast channels?",
            "response": "Go channels deliver messages to only one receiver; implementing broadcast requires spawning a dedicated dispatcher goroutine that explicitly copies messages to multiple individual channels.",
            "reason": "Asks about Go fan-out channel pattern rather than Tokio broadcast.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does a Tokio broadcast channel NOT block the sender when slow receivers fall behind?",
            "response": "Broadcast channels overwrite old buffer entries when capacity is exceeded to prioritize fast subscribers, sacrificing slow receiver delivery rather than blocking the publisher.",
            "reason": "Asks why the publisher is never blocked by slow receivers (design trade-off).",
        },
    },

    # 41. Node.js Stream Piping
    {
        "intent_group_id": "ig_fl_nodejs_stream_piping",
        "domain": "frameworks_and_libraries",
        "subtopic": "Node.js",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "nodejs"}},
        "canonical_query": "How does Node.js stream.pipeline() handle backpressure and error propagation compared to readable.pipe()?",
        "canonical_response": "While `readable.pipe()` chains streams, it does not automatically destroy intermediate streams or forward errors when one stream fails, risking memory and file descriptor leaks. `stream.pipeline()` manages backpressure cleanly, monitors all streams in the chain, and ensures all streams are destroyed if any error occurs.",
        "positive_paraphrase_1": {
            "query": "Why is stream.pipeline() preferred over .pipe() for stream error handling and resource cleanup in Node.js?",
            "reason": "Direct paraphrase asking why pipeline() is preferred over .pipe() for cleanup.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how stream.pipeline in Node.js guarantees stream closure when read or write errors occur.",
            "reason": "Syntactic variation focusing on stream closure guarantees during error conditions.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement a custom Transform stream in Node.js by overriding the _transform method?",
            "response": "```javascript\nclass Upper extends Transform {\n  _transform(chunk, enc, cb) { this.push(chunk.toString().toUpperCase()); cb(); }\n}\n```",
            "reason": "Asks how to implement a Transform stream subclass rather than explaining pipeline backpressure.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Node.js script that decompresses a .gz file, transforms content to uppercase, and writes to an output file using pipeline.",
            "response": "```javascript\nawait pipeline(fs.createReadStream('in.gz'), zlib.createGunzip(), upperTransform, fs.createWriteStream('out.txt'));\n```",
            "reason": "Requests specific gzip decompression pipeline code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Python io.BytesIO and itertools stream piping compare to Node.js streams?",
            "response": "Python itertools and generators use synchronous pull-based iteration, whereas Node.js streams use asynchronous event-driven push/pull models with internal highWaterMark buffers.",
            "reason": "Asks about Python generators vs Node.js streams.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes memory spikes when piping a fast readable stream to a slow writable stream without backpressure?",
            "response": "Without backpressure, the readable stream pushes data faster than the destination can write, buffering unlimited chunks in Node.js memory until the process runs out of RAM.",
            "reason": "Asks about memory spikes from absent backpressure (failure mode).",
        },
    },

    # 42. Express Error Handling Middleware
    {
        "intent_group_id": "ig_fl_express_middleware_error_handling",
        "domain": "frameworks_and_libraries",
        "subtopic": "Express",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "express"}},
        "canonical_query": "Why does Express.js require exactly four function parameters (err, req, res, next) for error-handling middleware?",
        "canonical_response": "Express inspects `fn.length` using JavaScript function arity. A middleware with 4 arguments is registered specifically as an error handler. When an endpoint or middleware passes an argument to `next(err)`, Express skips all standard 2- and 3-argument middlewares and invokes only 4-argument error handlers.",
        "positive_paraphrase_1": {
            "query": "How does Express use function parameter count (arity) to identify and route to error-handling middleware?",
            "reason": "Direct paraphrase explaining function arity checking for error middleware registration.",
        },
        "positive_paraphrase_2": {
            "query": "Explain why omitting the 'next' parameter in an Express error handler prevents Express from recognizing it.",
            "reason": "Syntactic variation addressing parameter count recognition in Express.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you handle unhandled async promise rejections in Express 4 routes without express-async-errors?",
            "response": "Wrap async route handler bodies in `try { await fn(); } catch (err) { next(err); }`, forwarding caught errors to Express's next() callback.",
            "reason": "Asks how to catch async rejections in Express 4 rather than explaining 4-parameter middleware arity.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Express centralized error-handling middleware that formats Joi validation errors and returns JSON.",
            "response": "```javascript\napp.use((err, req, res, next) => {\n  const status = err.status || 500;\n  res.status(status).json({ error: err.message });\n});\n```",
            "reason": "Requests specific JSON error formatting middleware code rather than explaining arity routing.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Koa.js async/await middleware cascading compare to Express 4 error-handling middleware?",
            "response": "Koa uses an onion model where upstream middleware wraps `await next()` in a try/catch block, catching downstream errors without specialized 4-parameter signatures.",
            "reason": "Asks about Koa.js onion model error handling rather than Express.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why must error-handling middleware be registered AFTER all app.use() and route declarations in Express?",
            "response": "Express executes middleware sequentially in the order registered; registering an error handler before routes means requests pass it before errors occur, never catching route failures.",
            "reason": "Asks about registration order requirements rather than function arity mechanics.",
        },
    },
]
