"""
Domain 6: API Design (21 intent groups).
Covers REST, Pagination, Filtering, GraphQL, Rate Limiting, and Webhooks.
"""

from typing import Any, Dict, List

API_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 106. REST Idempotency Keys
    {
        "intent_group_id": "ig_api_rest_idempotency_keys",
        "domain": "api_design",
        "subtopic": "REST",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "rest"}},
        "canonical_query": "How do Idempotency-Key HTTP headers guarantee safe retries for state-changing POST requests?",
        "canonical_response": "The client generates a unique UUID (Idempotency-Key) and attaches it to a POST request (such as a credit card payment). The server checks an atomic cache or database table for this key: if found, it skips executing the transaction and returns the exact cached response from the previous run; if not found, it executes the operation and stores the response against the key.",
        "positive_paraphrase_1": {
            "query": "Explain how payment APIs use Idempotency-Key headers to prevent double-charging during network retry spikes.",
            "reason": "Direct paraphrase explaining idempotency keys preventing duplicate payments on retries.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does tracking unique idempotency tokens on the server turn non-idempotent POST calls into safe retries?",
            "reason": "Syntactic variation focusing on turning non-idempotent POST requests into safe retries.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you handle in-progress concurrent requests arriving with the same Idempotency-Key before the first completes?",
            "response": "The server locks the idempotency key in cache upon receiving the first request; concurrent duplicate requests encountering the locked key return `HTTP 409 Conflict` or poll until the first finishes.",
            "reason": "Asks specifically how to handle in-progress concurrent key collisions rather than general safe retries.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FastAPI middleware class that intercepts Idempotency-Key headers and caches responses in Redis.",
            "response": "```python\nclass IdempotencyMiddleware(BaseHTTPMiddleware):\n    async def dispatch(self, request, call_next):\n        key = request.headers.get('Idempotency-Key')\n        # check redis, call_next, cache response\n```",
            "reason": "Requests specific FastAPI middleware implementation code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Stripe's public API specify and implement idempotency key TTL expiration policies?",
            "response": "Stripe stores idempotency keys for 24 hours; retried requests with the same key after 24 hours are treated as new requests, and changing the request payload under the same key returns an error.",
            "reason": "Asks about Stripe specific API implementation rules vs general REST idempotency concepts.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What error should an API return if a client sends an existing Idempotency-Key with a CHANGED request payload?",
            "response": "The server must return `HTTP 400 Bad Request` or `HTTP 422 Unprocessable Entity` with an error message indicating that payload parameters do not match the original idempotent request.",
            "reason": "Asks about payload mismatch error handling under existing keys (edge case error rule).",
        },
    },

    # 107. HTTP 401 vs 403
    {
        "intent_group_id": "ig_api_http_status_codes_401_403",
        "domain": "api_design",
        "subtopic": "REST",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "rest"}},
        "canonical_query": "What is the distinction between HTTP 401 Unauthorized and HTTP 403 Forbidden status codes in REST APIs?",
        "canonical_response": "HTTP 401 Unauthorized indicates a lack of valid authentication (the user is unauthenticated, missing credentials, or provided an expired token) and should include a WWW-Authenticate header. HTTP 403 Forbidden indicates that authentication succeeded, but the authenticated user lacks the authorization permissions required to access the resource.",
        "positive_paraphrase_1": {
            "query": "Explain the difference between authentication failure (401) and authorization permission denial (403).",
            "reason": "Direct paraphrase contrasting authentication (401) with authorization permission (403).",
        },
        "positive_paraphrase_2": {
            "query": "Why should an API return 401 when an API key is missing, but 403 when a user tries to access another tenant's data?",
            "reason": "Syntactic variation asking to differentiate 401 (missing credentials) from 403 (insufficient permissions).",
        },
        "hard_negative_diff_operation": {
            "query": "When should an API deliberately return HTTP 404 Not Found instead of HTTP 403 Forbidden for security reasons?",
            "response": "Return 404 instead of 403 when revealing the existence of a sensitive resource to an unauthorized user would leak confidential information (preventing resource enumeration attacks).",
            "reason": "Asks when to return 404 instead of 403 for obfuscation rather than contrasting 401 and 403.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python decorator @require_roles('admin') that raises HTTP 401 or HTTP 403 appropriately.",
            "response": "```python\ndef require_roles(role):\n    def decorator(fn):\n        def wrapper(user, *args, **kwargs):\n            if not user: raise HTTPException(401)\n            if role not in user.roles: raise HTTPException(403)\n            return fn(user, *args, **kwargs)\n        return wrapper\n    return decorator\n```",
            "reason": "Requests specific Python decorator implementation code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Spring Security map AuthenticationException and AccessDeniedException to HTTP status codes?",
            "response": "Spring Security delegates AuthenticationException to AuthenticationEntryPoint (returning 401) and AccessDeniedException to AccessDeniedHandler (returning 403).",
            "reason": "Asks about Spring Security exception mapping vs general HTTP status code specifications.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is HTTP 401 named 'Unauthorized' considered an unfortunate historical misnomer in the HTTP RFCs?",
            "response": "Because RFC 9110 specifies that 401 means 'Unauthenticated' (missing or invalid credentials), whereas true authorization failure is represented by 403 Forbidden.",
            "reason": "Asks about RFC nomenclature history and semantic misnomers.",
        },
    },

    # 108. HTTP PUT vs PATCH
    {
        "intent_group_id": "ig_api_http_put_vs_patch",
        "domain": "api_design",
        "subtopic": "REST",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "rest"}},
        "canonical_query": "What is the difference between HTTP PUT complete replacement and HTTP PATCH partial modification in REST?",
        "canonical_response": "PUT is an idempotent method that replaces the entire target resource with the request representation; any omitted fields in a PUT body should be reset to their default or null values. PATCH is not required to be idempotent and applies a partial modification to the resource, updating only the specific fields provided in the body.",
        "positive_paraphrase_1": {
            "query": "Compare HTTP PUT and HTTP PATCH regarding full entity replacement versus partial field updates.",
            "reason": "Direct paraphrase comparing full replacement (PUT) with partial modification (PATCH).",
        },
        "positive_paraphrase_2": {
            "query": "Why is PUT idempotent while PATCH allows delta updates in RESTful API design?",
            "reason": "Syntactic variation focusing on idempotency and delta updates in PUT vs PATCH.",
        },
        "hard_negative_diff_operation": {
            "query": "How does the JSON Patch format (RFC 6902) express operations like add, remove, and replace in a PATCH request?",
            "response": "RFC 6902 uses a JSON array of operations: `[{\"op\": \"replace\", \"path\": \"/email\", \"value\": \"new@example.com\"}]`, defining precise atomic state transformations.",
            "reason": "Asks specifically about RFC 6902 JSON Patch format syntax rather than comparing PUT vs PATCH.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FastAPI endpoint implementing a PATCH update using Pydantic model_dump(exclude_unset=True).",
            "response": "```python\n@app.patch('/items/{id}')\nasync def patch_item(id: int, patch_data: ItemUpdate):\n    stored = await db.get(id)\n    updated = stored.copy(update=patch_data.model_dump(exclude_unset=True))\n    await db.save(updated)\n    return updated\n```",
            "reason": "Requests specific FastAPI PATCH code implementation rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does JSON Merge Patch (RFC 7396) compare to JSON Patch (RFC 6902)?",
            "response": "RFC 7396 Merge Patch uses a partial JSON document where null removes fields, whereas RFC 6902 JSON Patch uses an explicit array of patch operation instructions.",
            "reason": "Asks about RFC 7396 vs RFC 6902 rather than PUT vs PATCH.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can an HTTP PATCH request be non-idempotent if it performs operations like relative increments?",
            "response": "If a PATCH body specifies an operation like `{'increment': 1}`, sending the request twice increments the value twice, making the operation non-idempotent.",
            "reason": "Asks why PATCH can be non-idempotent (relative increment scenario).",
        },
    },

    # 109. API Versioning Strategies
    {
        "intent_group_id": "ig_api_versioning_strategies",
        "domain": "api_design",
        "subtopic": "APIDesign",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "versioning"}},
        "canonical_query": "What are the trade-offs between URI path versioning (/v1/), header versioning, and query parameter versioning?",
        "canonical_response": "URI path versioning (`/v1/users`) is explicit, easy to test in browsers, and caches cleanly at CDNs, but violates pure REST URI resource identity. Header versioning (`Accept: application/vnd.company.v1+json`) preserves URI identity and supports fine-grained content negotiation, but complicates testing and caching. Query parameter versioning (`?version=1`) is simple but pollutes search params.",
        "positive_paraphrase_1": {
            "query": "Compare URI path versioning with custom HTTP header versioning in RESTful API architectures.",
            "reason": "Direct paraphrase comparing URI path vs custom header versioning.",
        },
        "positive_paraphrase_2": {
            "query": "What are the advantages and drawbacks of versioning APIs through URL paths versus Accept headers?",
            "reason": "Syntactic variation focusing on pros and cons of URL vs Accept header versioning.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Stripe handle date-based API versioning using the Stripe-Version header?",
            "response": "Stripe pins user accounts to a default date-version (e.g. 2024-04-10) and uses server-side transformation layers that translate between internal models and historical version representations.",
            "reason": "Asks about Stripe's date-based versioning implementation rather than comparing general versioning strategies.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Nginx routing rules to direct /v1/ requests to backend_v1 and /v2/ requests to backend_v2 upstream pools.",
            "response": "```nginx\nlocation /v1/ { proxy_pass http://backend_v1; }\nlocation /v2/ { proxy_pass http://backend_v2; }\n```",
            "reason": "Requests specific Nginx configuration directives rather than conceptual architectural comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does GraphQL schema evolution via @deprecated directives eliminate versioning compared to REST?",
            "response": "GraphQL avoids versioning by growing a single schema, adding new fields alongside old ones and marking outdated fields with `@deprecated(reason: \"...\")`.",
            "reason": "Asks about GraphQL deprecation vs REST versioning.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does changing response field types (e.g. from integer to string) ALWAYS constitute a breaking API change?",
            "response": "Strict client parsers and typed SDKs will crash on deserialization errors if a field type changes, breaking backward compatibility even if the field name remains unchanged.",
            "reason": "Asks about breaking changes in field types (compatibility principle).",
        },
    },

    # 110. HATEOAS Hypermedia
    {
        "intent_group_id": "ig_api_rest_hateoas_hypermedia",
        "domain": "api_design",
        "subtopic": "REST",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "hateoas"}},
        "canonical_query": "What is the core principle of HATEOAS (Hypermedia As The Engine Of Application State) in REST maturity level 3?",
        "canonical_response": "HATEOAS dictates that a REST client interacts with an application entirely through dynamic hypermedia links provided in server responses (similar to a user navigating web pages via HTML links). Clients do not hardcode URL routing rules; instead, responses include `_links` (e.g. HAL format) detailing available actions and next state transitions.",
        "positive_paraphrase_1": {
            "query": "Explain how HATEOAS uses embedded hypermedia links to guide client state transitions in Richardson Maturity Level 3.",
            "reason": "Direct paraphrase explaining Level 3 Richardson Maturity HATEOAS hypermedia links.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does HATEOAS decouple client navigation from hardcoded API endpoint URLs?",
            "reason": "Syntactic variation focusing on decoupling client routing via dynamic links.",
        },
        "hard_negative_diff_operation": {
            "query": "What is the JSON Hypertext Application Language (HAL) specification format for representing links?",
            "response": "HAL embeds a reserved `_links` object where link relationships (rel) map to objects containing an `href` URI, and an optional `_embedded` object for nested resources.",
            "reason": "Asks for HAL JSON syntax specification rather than explaining the HATEOAS architectural principle.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Spring HATEOAS controller method that adds self and cancel links to an OrderModel entity.",
            "response": "```java\nEntityModel<Order> model = EntityModel.of(order,\n  linkTo(methodOn(OrderController.class).one(id)).withSelfRel(),\n  linkTo(methodOn(OrderController.class).cancel(id)).withRel(\"cancel\"));\n```",
            "reason": "Requests specific Spring HATEOAS Java code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "Why has HATEOAS seen low adoption in modern mobile and frontend SPA development compared to OpenAPI/Swagger?",
            "response": "Frontends prefer compile-time typed SDKs generated from OpenAPI schemas; dynamic hypermedia link discovery adds client complexity without sufficient practical benefit for predictable SPAs.",
            "reason": "Asks why HATEOAS failed to achieve widespread adoption vs OpenAPI.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the Richardson Maturity Model and what characterizes Level 0, Level 1, Level 2, and Level 3?",
            "response": "Level 0: The Swamp of POX (single URI/POST); Level 1: Resources (individual URIs); Level 2: HTTP Verbs (GET/POST/PUT/DELETE with status codes); Level 3: Hypermedia Controls (HATEOAS).",
            "reason": "Asks for full Richardson Maturity Model levels rather than specifically explaining HATEOAS.",
        },
    },

    # 111. RFC 7807 Problem Details
    {
        "intent_group_id": "ig_api_error_response_rfc7807",
        "domain": "api_design",
        "subtopic": "APIDesign",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "problem_details"}},
        "canonical_query": "How does the RFC 7807 Problem Details for HTTP APIs standard format error payloads?",
        "canonical_response": "RFC 7807 standardizes error responses under the `application/problem+json` media type with five core URI/string properties: `type` (a URI reference identifying problem type), `title` (short human-readable summary), `status` (HTTP status code), `detail` (human-readable explanation specific to this occurrence), and `instance` (URI reference identifying the specific occurrence).",
        "positive_paraphrase_1": {
            "query": "Explain the standard fields (type, title, status, detail, instance) defined in RFC 7807 Problem Details for JSON APIs.",
            "reason": "Direct paraphrase explaining the 5 standard fields of RFC 7807.",
        },
        "positive_paraphrase_2": {
            "query": "Why should modern REST APIs adopt application/problem+json instead of custom error response formats?",
            "reason": "Syntactic variation focusing on adoption benefits of application/problem+json.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you extend an RFC 7807 error response with custom validation error extension members in JSON?",
            "response": "RFC 7807 allows arbitrary extension members at the root level; you can append an `invalid-params` array listing specific field names and reasons: `{\"invalid-params\": [{\"name\": \"age\", \"reason\": \"must be > 0\"}]}`.",
            "reason": "Asks how to add custom extension members rather than explaining the standard structure.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FastAPI custom exception handler that catches ValueError and returns an RFC 7807 compliant JSONResponse.",
            "response": "```python\n@app.exception_handler(ValueError)\nasync def handle_val_err(req, exc):\n    return JSONResponse(status_code=400, media_type='application/problem+json', content={\n        'type': 'https://example.com/errors/bad-value', 'title': 'Invalid Value', 'status': 400, 'detail': str(exc)\n    })\n```",
            "reason": "Requests specific FastAPI exception handler code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does GraphQL error format (errors array with message, locations, path) compare to RFC 7807?",
            "response": "GraphQL always returns HTTP 200 with an `errors` array alongside data, whereas RFC 7807 uses standard HTTP 4xx/5xx status codes with a single problem object.",
            "reason": "Asks about GraphQL error arrays vs REST RFC 7807.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why should the 'title' member in an RFC 7807 payload NOT change between different occurrences of the same problem type?",
            "response": "The RFC dictates that `title` provides a static, generic summary for that error type (e.g. 'Out of Credit'); dynamic, instance-specific explanations belong strictly in the `detail` property.",
            "reason": "Asks about the strict RFC rule separating static 'title' from dynamic 'detail'.",
        },
    },

    # 112. Cursor-Based vs Offset Pagination
    {
        "intent_group_id": "ig_api_cursor_vs_offset_pagination",
        "domain": "api_design",
        "subtopic": "Pagination",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "pagination"}},
        "canonical_query": "Why does cursor-based (keyset) pagination scale to deep pages while offset-limit pagination degrades to O(N)?",
        "canonical_response": "In offset-limit pagination (`OFFSET 1000000 LIMIT 20`), the database must scan and discard 1,000,000 index entries before returning 20 rows, degrading linearly with depth and suffering from page drift when rows are inserted. Keyset/cursor pagination (`WHERE id > :last_id ORDER BY id LIMIT 20`) uses B-tree index seeks to jump directly to the target record in O(log N) time with zero drift.",
        "positive_paraphrase_1": {
            "query": "Explain why cursor pagination avoids database sequential scans and prevents page drift compared to offset pagination.",
            "reason": "Direct paraphrase explaining B-tree seek performance and drift prevention in cursor pagination.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does keyset pagination maintain constant-time query latency when paginating millions of rows?",
            "reason": "Syntactic variation focusing on constant-time query latency across large datasets.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you encode and decode composite cursor values in Python using base64 for multiple sort columns?",
            "response": "```python\ncursor_str = base64.b64encode(json.dumps([created_at.isoformat(), row_id]).encode()).decode()\n# decode with base64.b64decode and json.loads\n```",
            "reason": "Asks how to base64-encode composite cursors rather than explaining database index mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the SQL query for bidirectional keyset pagination on (created_at DESC, id DESC) using a composite cursor.",
            "response": "```sql\nSELECT * FROM events\nWHERE (created_at, id) < ($1, $2)\nORDER BY created_at DESC, id DESC LIMIT 20;\n```",
            "reason": "Requests specific SQL query syntax rather than conceptual comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Relay GraphQL cursor connections specification (edges, node, pageInfo) standardize pagination?",
            "response": "Relay defines a standard GraphQL schema pattern with `edges { cursor, node }` and `pageInfo { hasNextPage, endCursor }`, decoupling client pagination from backend storage formats.",
            "reason": "Asks about GraphQL Relay connection specification vs SQL pagination.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does cursor-based pagination make jumping directly to arbitrary page numbers (like page 47) impossible?",
            "response": "Cursor pagination requires the client to possess the boundary token of the preceding page; without scanning intermediate records, the database cannot calculate where page 47 begins.",
            "reason": "Asks about the inability to jump to arbitrary page numbers (fundamental trade-off).",
        },
    },

    # 113. Sparse Fieldsets (Field Filtering)
    {
        "intent_group_id": "ig_api_field_filtering",
        "domain": "api_design",
        "subtopic": "APIDesign",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "filtering"}},
        "canonical_query": "How do sparse fieldsets using a ?fields= query parameter reduce network payload size and client parsing overhead?",
        "canonical_response": "Sparse fieldsets (standardized in JSON:API) allow clients to specify exactly which object attributes should be returned (e.g. `GET /users?fields=id,name`). The server filters the serialized payload, discarding unrequested heavy fields (like avatars or bios), significantly reducing serialization CPU time, bandwidth consumption, and client memory allocation.",
        "positive_paraphrase_1": {
            "query": "Explain how implementing ?fields= filtering in REST APIs solves client over-fetching without adopting GraphQL.",
            "reason": "Direct paraphrase explaining how ?fields= filtering addresses over-fetching.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does partial response selection through query parameters optimize mobile API performance?",
            "reason": "Syntactic variation focusing on bandwidth and mobile optimization via partial responses.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you project sparse fieldsets into an SQL SELECT clause dynamically using SQLAlchemy or Prisma?",
            "response": "Parse the requested fields query string into an array and pass it to SQLAlchemy's `.with_only_columns(*[getattr(User, f) for f in fields])` or Prisma's `select: { name: true }`.",
            "reason": "Asks how to project fields into ORM queries rather than explaining API design benefits.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FastAPI dependency that parses ?fields=a,b,c and returns a filtered Pydantic model response.",
            "response": "```python\ndef get_fields(fields: str = None):\n    return set(fields.split(',')) if fields else None\n```",
            "reason": "Requests specific FastAPI dependency code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does GraphQL query selection sets compare to REST sparse fieldsets for nested relational graphs?",
            "response": "REST ?fields= becomes unwieldy when selecting nested relations (e.g. `fields[users]=name&fields[posts]=title`), whereas GraphQL natively expresses arbitrary hierarchical selections.",
            "reason": "Asks about GraphQL nested selection vs REST sparse fieldsets.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why should mandatory fields like primary keys (id) always be included even if omitted from ?fields=?",
            "response": "Client-side caching libraries (like Apollo or normalized Redux stores) require the primary key (`id`) to maintain entity identity; omitting it breaks client-side cache normalization.",
            "reason": "Asks about primary key preservation for client cache normalization.",
        },
    },

    # 114. Multi-Column Sorting
    {
        "intent_group_id": "ig_api_sorting_query_design",
        "domain": "api_design",
        "subtopic": "APIDesign",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "sorting"}},
        "canonical_query": "How should RESTful query parameters be designed to support multi-column sorting with ascending and descending directions?",
        "canonical_response": "A clean industry standard (used by JSON:API and Stripe) uses a single comma-separated parameter: `?sort=-created_at,name`. Prefixing a field with `-` indicates descending order, while no prefix (or `+`) indicates ascending order. The server validates allowed sort fields against a strict allowlist and constructs an SQL `ORDER BY created_at DESC, name ASC` clause.",
        "positive_paraphrase_1": {
            "query": "Explain best practices for designing multi-column sort query parameters using comma-delimited strings with minus signs.",
            "reason": "Direct paraphrase explaining comma-delimited minus prefix sort parameters.",
        },
        "positive_paraphrase_2": {
            "query": "In what way should an API parse and validate sort parameters to map them safely to database ORDER BY clauses?",
            "reason": "Syntactic variation focusing on validation and database ORDER BY mapping.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you protect database sorting endpoints from SQL injection attacks when accepting user-supplied sort parameters?",
            "response": "Never concatenate raw sort strings into SQL; check field names against an explicit set of permitted columns (`ALLOWED_SORTS = {'created_at', 'name'}`) and reject any unknown identifier.",
            "reason": "Asks specifically about SQL injection defense rather than general API sorting parameter design.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python helper function that converts ?sort=-priority,name into SQLAlchemy order_by expressions.",
            "response": "```python\ndef parse_sort(sort_str, model):\n    order_clauses = []\n    for item in sort_str.split(','):\n        desc = item.startswith('-')\n        col = getattr(model, item.lstrip('-+'))\n        order_clauses.append(col.desc() if desc else col.asc())\n    return order_clauses\n```",
            "reason": "Requests specific SQLAlchemy parsing code rather than conceptual API design.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does OData query syntax ($orderby=Price desc, Name asc) compare to JSON:API sort conventions?",
            "response": "OData uses explicit SQL-like keywords (`$orderby=Price desc`), whereas JSON:API uses compact URI-friendly minus prefixes (`?sort=-price`).",
            "reason": "Asks about OData vs JSON:API sorting syntax.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why must sorting queries on non-unique columns always include a unique tie-breaker column (like id) in ORDER BY?",
            "response": "Without a unique deterministic tie-breaker, database sort ordering on matching values is non-deterministic, causing rows to swap positions or appear on multiple pages during pagination.",
            "reason": "Asks about non-deterministic pagination ordering without tie-breakers.",
        },
    },

    # 115. Bulk vs Batch Operations
    {
        "intent_group_id": "ig_api_bulk_vs_batch_operations",
        "domain": "api_design",
        "subtopic": "APIDesign",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "batch"}},
        "canonical_query": "What is the architectural difference between bulk operations and batch operations in REST API design?",
        "canonical_response": "Bulk operations execute a single homogeneous action across a collection of entities within a single payload (e.g. `POST /users/bulk` creating 1,000 users in one atomic SQL INSERT). Batch operations group multiple heterogeneous, independent HTTP requests into a single envelope payload (e.g. `POST /batch` containing a GET /user and a POST /order), executed by the server to reduce roundtrip overhead.",
        "positive_paraphrase_1": {
            "query": "Compare homogeneous bulk API endpoints with heterogeneous batch envelope requests in REST architectures.",
            "reason": "Direct paraphrase comparing bulk operations with batch request envelopes.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how bulk database operations differ from batch HTTP request multiplexing.",
            "reason": "Syntactic variation focusing on bulk database mutations vs HTTP batch multiplexing.",
        },
        "hard_negative_diff_operation": {
            "query": "How should an API structure HTTP 207 Multi-Status responses for partial batch execution failures?",
            "response": "HTTP 207 Multi-Status returns an array of individual status objects where each entry contains its own HTTP status code (e.g. 200 OK or 400 Bad Request) and response body.",
            "reason": "Asks how to format HTTP 207 responses rather than contrasting bulk vs batch operations.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script that accepts a batch envelope array, executes handlers sequentially, and returns a composite result.",
            "response": "```python\n@app.post('/batch')\nasync def handle_batch(requests: list[SubRequest]):\n    return [await execute_sub_request(r) for r in requests]\n```",
            "reason": "Requests specific batch execution code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Google Drive batch API multipart/mixed HTTP requests compare to JSON envelope batching?",
            "response": "Google Drive uses MIME `multipart/mixed` multipart encoding separating HTTP requests with boundary delimiters, whereas modern APIs prefer JSON arrays of request objects.",
            "reason": "Asks about MIME multipart batching vs JSON envelopes.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why should bulk operations strictly enforce a maximum item limit (e.g. max 500 items per request)?",
            "response": "Uncapped bulk endpoints can cause massive memory spikes, lock contention on database tables, gateway timeout errors, and denial-of-service vulnerabilities.",
            "reason": "Asks about payload size limits and DDoS protection on bulk endpoints.",
        },
    },

    # 116. GraphQL vs REST Overfetching
    {
        "intent_group_id": "ig_api_graphql_vs_rest_overfetching",
        "domain": "api_design",
        "subtopic": "GraphQL",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "graphql_vs_rest"}},
        "canonical_query": "How does GraphQL eliminate over-fetching and under-fetching compared to fixed REST endpoint payloads?",
        "canonical_response": "In REST, endpoints return fixed JSON data shapes; clients often download unneeded fields (over-fetching) or must make multiple consecutive roundtrip requests to different endpoints to assemble related data (under-fetching). In GraphQL, clients send an explicit query document requesting only the exact fields and nested relationships needed, returned in a single roundtrip.",
        "positive_paraphrase_1": {
            "query": "Explain how declarative query selection in GraphQL solves both client over-fetching and under-fetching.",
            "reason": "Direct paraphrase explaining how declarative selection eliminates over- and under-fetching.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does GraphQL allow frontend clients to aggregate relational entities in one request without waterfall calls?",
            "reason": "Syntactic variation focusing on single-roundtrip relational aggregation without waterfall calls.",
        },
        "hard_negative_diff_operation": {
            "query": "Why is HTTP-level edge caching (with CDNs and browser caches) significantly harder with GraphQL than REST?",
            "response": "REST uses distinct URLs and GET requests that CDNs cache natively; GraphQL typically uses POST requests directed to a single endpoint (/graphql) with dynamic query bodies, breaking standard URL-based CDN caching.",
            "reason": "Asks about CDN caching difficulties with GraphQL rather than over-fetching.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a GraphQL query that fetches a user's name and their 3 most recent posts with comments count.",
            "response": "```graphql\nquery GetUserPosts {\n  user(id: \"1\") {\n    name\n    posts(first: 3) {\n      title\n      commentsCount\n    }\n  }\n}\n```",
            "reason": "Requests specific GraphQL query syntax rather than architectural comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Falcor JSON Graph compare to GraphQL for client data querying?",
            "response": "Netflix Falcor models domain data as a single virtual JSON graph on the server accessed via paths, while GraphQL uses typed schema definition language (SDL) and explicit query syntax.",
            "reason": "Asks about Netflix Falcor vs GraphQL.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "How can deeply nested GraphQL queries be abused to execute denial-of-service attacks on backend servers?",
            "response": "An attacker can craft circular recursive queries (e.g. author -> posts -> author -> posts) that exponentially amplify database joins and CPU computation unless query depth and complexity limits are enforced.",
            "reason": "Asks about recursive query DOS vulnerabilities in GraphQL.",
        },
    },

    # 117. GraphQL N+1 Problem and DataLoader
    {
        "intent_group_id": "ig_api_graphql_n_plus_one_dataloader",
        "domain": "api_design",
        "subtopic": "GraphQL",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "graphql"}},
        "canonical_query": "How does the DataLoader pattern resolve the N+1 database query problem in GraphQL field resolvers?",
        "canonical_response": "In GraphQL, nested resolvers execute independently; resolving the 'author' of N books triggers 1 query for books followed by N individual queries for authors (the N+1 problem). DataLoader batches all key lookups within a single event loop tick into a single `SELECT * FROM authors WHERE id IN (...)` query and memoizes results in a per-request cache.",
        "positive_paraphrase_1": {
            "query": "Explain how DataLoader uses batching and per-request memoization to eliminate N+1 queries in GraphQL resolvers.",
            "reason": "Direct paraphrase explaining batching and memoization in DataLoader.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does coalescing field resolver queries across an event loop turn N database roundtrips into one?",
            "reason": "Syntactic variation focusing on coalescing database queries across event loop ticks.",
        },
        "hard_negative_diff_operation": {
            "query": "Why must a new DataLoader instance be instantiated on every incoming HTTP request rather than shared globally?",
            "response": "Sharing a DataLoader globally leaks cached data across different users (violating authorization) and allows memory to grow unboundedly over time.",
            "reason": "Asks why DataLoader instances must be request-scoped rather than explaining how batching works.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a JavaScript DataLoader instance that batches user IDs and queries a PostgreSQL database with pg.",
            "response": "```javascript\nconst userLoader = new DataLoader(async (keys) => {\n  const res = await pool.query('SELECT * FROM users WHERE id = ANY($1)', [keys]);\n  return keys.map(k => res.rows.find(r => r.id === k));\n});\n```",
            "reason": "Requests specific JavaScript DataLoader code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does ORM eager loading (e.g. Hibernate join fetch or Prisma include) compare to GraphQL DataLoader?",
            "response": "ORM eager loading joins tables at query time based on static code configuration, while DataLoader dynamically batches queries at runtime based on the specific fields requested by the GraphQL query.",
            "reason": "Asks about ORM eager loading vs GraphQL DataLoader.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What bug occurs if a DataLoader batch function returns an array with fewer elements or mismatched order from input keys?",
            "response": "DataLoader strictly requires the returned array to have the exact same length and key ordering as the input keys; a mismatch causes DataLoader to return incorrect entity data to resolvers or throw an error.",
            "reason": "Asks about the array cardinality and ordering invariant bug in DataLoader batch functions.",
        },
    },

    # 118. GraphQL Federation vs Schema Stitching
    {
        "intent_group_id": "ig_api_graphql_schema_stitching",
        "domain": "api_design",
        "subtopic": "GraphQL",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "graphql"}},
        "canonical_query": "What is the architectural difference between Apollo Federation and legacy GraphQL Schema Stitching for microservices?",
        "canonical_response": "Schema Stitching imperatively merges multiple independent GraphQL schemas into a central gateway using custom configuration code and manual resolver delegation. Apollo Federation is a declarative architecture where individual subgraphs define entities with `@key` directives and the Apollo Gateway builds a unified supergraph, automatically resolving entities across services via query plans.",
        "positive_paraphrase_1": {
            "query": "Compare Apollo Federation declarative subgraphs with imperative GraphQL Schema Stitching.",
            "reason": "Direct paraphrase comparing declarative federation with imperative stitching.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how Apollo Federation composes distributed microservice schemas into a single unified supergraph.",
            "reason": "Syntactic variation focusing on supergraph composition via subgraphs.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you extend an entity across subgraphs in Apollo Federation using @key and @external directives?",
            "response": "Declare the entity with `extend type User @key(fields: \"id\")`, mark foreign fields as `@external`, and implement a reference resolver (`__resolveReference`) in the subgraph.",
            "reason": "Asks how to write schema directives and reference resolvers rather than comparing federation vs stitching.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Apollo Router configuration YAML file connecting an accounts subgraph and products subgraph.",
            "response": "```yaml\nsupergraph:\n  listen: 0.0.0.0:4000\nsubgraphs:\n  accounts: routing_url: http://accounts:4001\n  products: routing_url: http://products:4002\n```",
            "reason": "Requests specific Apollo Router YAML configuration rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does an API Gateway with REST microservices compare to a Federated GraphQL supergraph gateway?",
            "response": "A REST gateway routes URLs to services leaving client aggregation to separate calls, while a Federated GraphQL gateway parses incoming queries, generates a multi-service query plan, and aggregates JSON responses into one.",
            "reason": "Asks about REST API Gateway vs GraphQL Federation.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if two subgraphs in Apollo Federation define identical fields with conflicting types?",
            "response": "Supergraph composition fails at build time with a composition error; fields must either share identical types or be marked with `@shareable` if owned across multiple subgraphs.",
            "reason": "Asks about composition build failures on conflicting field types.",
        },
    },

    # 119. GraphQL Query Complexity Analysis
    {
        "intent_group_id": "ig_api_graphql_query_complexity",
        "domain": "api_design",
        "subtopic": "GraphQL",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "graphql"}},
        "canonical_query": "How does GraphQL query depth and complexity analysis protect production servers from Denial of Service (DoS)?",
        "canonical_response": "Before execution, a complexity analyzer walks the Abstract Syntax Tree (AST) of the incoming GraphQL document. It computes maximum depth (nesting level) and assigns cost weights to individual fields (e.g. 1 point for scalar fields, 10 points for database-backed lists). If total computed complexity exceeds a safe threshold, the request is rejected with HTTP 400 before executing resolvers.",
        "positive_paraphrase_1": {
            "query": "Explain how AST query depth limiting and complexity cost calculation prevent malicious GraphQL queries.",
            "reason": "Direct paraphrase explaining AST depth and complexity cost validation.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does static complexity validation protect backend databases from deeply nested GraphQL joins?",
            "reason": "Syntactic variation focusing on protecting databases from deeply nested joins.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Automatic Persisted Queries (APQ) optimize bandwidth and protect GraphQL endpoints?",
            "response": "Clients send the SHA-256 hash of a query instead of the full query string; the server looks up the hash in cache and executes the registered query, rejecting arbitrary untrusted client queries.",
            "reason": "Asks about Automatic Persisted Queries (APQ) rather than query complexity analysis.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the TypeScript configuration for graphql-depth-limit rule in Apollo Server with maxDepth=5.",
            "response": "```typescript\nimport depthLimit from 'graphql-depth-limit';\nconst server = new ApolloServer({\n  validationRules: [depthLimit(5)],\n});\n```",
            "reason": "Requests specific Apollo Server validation rule code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does REST API rate limiting (IP-based) compare to GraphQL query cost rate limiting?",
            "response": "REST rate limits count raw HTTP requests regardless of payload weight, while GraphQL cost rate limiting deducts points based on the actual computation and database cost of the specific query.",
            "reason": "Asks about REST request counting vs GraphQL cost-based rate limiting.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is simple query depth limiting INSUFFICIENT to stop DoS without complexity cost estimation?",
            "response": "A shallow query with depth of only 2 can request 100 parallel root fields, each requesting 1,000 items (e.g. `users(first: 1000)`), saturating database connections despite passing depth checks.",
            "reason": "Asks why depth limiting alone fails against horizontal query fan-out.",
        },
    },

    # 120. GraphQL Subscriptions
    {
        "intent_group_id": "ig_api_graphql_subscriptions",
        "domain": "api_design",
        "subtopic": "GraphQL",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "graphql"}},
        "canonical_query": "How do GraphQL subscriptions use WebSockets and pub/sub engines to push real-time event updates?",
        "canonical_response": "GraphQL subscriptions establish a persistent WebSocket connection using a subprotocol (like `graphql-transport-ws`). When a client subscribes to an event, the server registers an AsyncIterator connected to an internal PubSub engine (like Redis). When an event is published, the server executes the subscription selection set and pushes the formatted result over the socket.",
        "positive_paraphrase_1": {
            "query": "Explain how GraphQL subscriptions use persistent WebSocket connections and AsyncIterators to deliver live updates.",
            "reason": "Direct paraphrase explaining WebSocket subprotocols and AsyncIterators in subscriptions.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do pub/sub engines and GraphQL subscriptions enable real-time event push to frontend clients?",
            "reason": "Syntactic variation focusing on pub/sub engines and event push.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you secure GraphQL subscriptions using JWT authentication in the onConnect connection parameter?",
            "response": "Inspect `connectionParams.authorization` in the `onConnect` WebSocket lifecycle hook, verify the JWT token, and reject the connection if invalid or expired before establishing the session.",
            "reason": "Asks how to implement JWT authentication in onConnect rather than explaining pub/sub mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a GraphQL subscription resolver in JavaScript using graphql-subscriptions PubSub for postAdded events.",
            "response": "```javascript\nconst pubsub = new PubSub();\nconst resolvers = {\n  Subscription: {\n    postAdded: { subscribe: () => pubsub.asyncIterator(['POST_ADDED']) }\n  }\n};\n```",
            "reason": "Requests specific subscription resolver code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does HTTP @defer and @stream directives compare to GraphQL WebSocket subscriptions?",
            "response": "Defer and stream deliver incremental chunks of a single query over a single multipart HTTP response, while subscriptions stream discrete independent events indefinitely over persistent WebSockets.",
            "reason": "Asks about defer/stream directives vs subscriptions.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why do GraphQL subscriptions scale poorly compared to standard stateless GraphQL queries under high client counts?",
            "response": "Subscriptions require stateful, persistent TCP/WebSocket connections and active memory structures for each client on server nodes, requiring distributed Redis pub/sub backplanes to scale.",
            "reason": "Asks about stateful connection scaling bottlenecks in subscriptions.",
        },
    },

    # 121. GraphQL Input Validation
    {
        "intent_group_id": "ig_api_graphql_input_validation",
        "domain": "api_design",
        "subtopic": "GraphQL",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "graphql"}},
        "canonical_query": "How do custom GraphQL scalars and input object types enforce semantic data validation at the schema layer?",
        "canonical_response": "GraphQL schemas define `input` types for structured mutation arguments. Custom scalar types (like DateTime, EmailAddress, or PositiveInt) implement serialization and parsing functions: `parseValue` validates variables, `parseLiteral` validates AST query literals, and `serialize` validates output types, rejecting invalid data before resolvers execute.",
        "positive_paraphrase_1": {
            "query": "Explain how custom GraphQL scalars use parseValue and parseLiteral to validate input arguments at schema boundaries.",
            "reason": "Direct paraphrase explaining custom scalar validation methods parseValue and parseLiteral.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does defining custom scalar types in GraphQL prevent malformed data from reaching mutation resolvers?",
            "reason": "Syntactic variation focusing on malformed data rejection at schema boundaries.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement input validation in GraphQL using third-party libraries like Zod or Yup schema validators?",
            "response": "Define a Zod schema matching the input type and validate arguments inside the resolver: `const data = UserSchema.parse(args.input)`, throwing a UserInputError if validation fails.",
            "reason": "Asks how to use Zod inside resolvers rather than explaining custom scalar schema methods.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a custom GraphQLScalarType for EmailAddress that validates email format using a regex pattern.",
            "response": "```javascript\nconst EmailScalar = new GraphQLScalarType({\n  name: 'Email',\n  parseValue(v) { if (!isEmail(v)) throw new Error('Invalid email'); return v; },\n  serialize(v) { return v; }\n});\n```",
            "reason": "Requests specific scalar implementation code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does JSON Schema validation in REST compare to GraphQL input object validation?",
            "response": "JSON Schema supports complex conditional logic (if-then-else, patternProperties) across standalone JSON files, whereas GraphQL input types provide static typed field structures in SDL.",
            "reason": "Asks about JSON Schema vs GraphQL input validation.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can GraphQL custom scalar parseLiteral fail on valid inputs if variables are used instead of query literals?",
            "response": "Because `parseLiteral` is invoked only for hardcoded AST literals in the query document; inputs supplied via the separate JSON `variables` map are passed directly to `parseValue` instead.",
            "reason": "Asks about the difference between parseLiteral (AST) and parseValue (variables).",
        },
    },

    # 122. Token Bucket Rate Limiting
    {
        "intent_group_id": "ig_api_token_bucket_rate_limiting",
        "domain": "api_design",
        "subtopic": "RateLimiting",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "rate_limiting"}},
        "canonical_query": "How does the Token Bucket rate-limiting algorithm accommodate temporary traffic bursts while enforcing an average rate limit?",
        "canonical_response": "The Token Bucket holds a maximum capacity of B tokens. Tokens are continuously added to the bucket at a constant refill rate R (tokens per second). When a request arrives, it attempts to consume a token: if tokens are available, the request proceeds immediately; if the bucket is empty, the request is rejected with HTTP 429. This allows bursts up to capacity B while bounding long-term rate to R.",
        "positive_paraphrase_1": {
            "query": "Explain how the token bucket algorithm allows bursty traffic up to bucket capacity while capping average request rates.",
            "reason": "Direct paraphrase explaining burst capacity and average refill rates in token bucket.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the token refill rate and bucket capacity model rate limiting for web APIs?",
            "reason": "Syntactic variation focusing on token refill rates and capacity constraints.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement an atomic Token Bucket rate limiter in Redis using a single Lua script?",
            "response": "```lua\nlocal key = KEYS[1]; local cap = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local now = tonumber(ARGV[3])\n-- calculate tokens added since last update, consume 1, and update timestamp\n```",
            "reason": "Asks for specific Redis Lua script implementation rather than explaining algorithm concepts.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python thread-safe TokenBucket class using time.monotonic() and threading.Lock.",
            "response": "```python\nclass TokenBucket:\n    def __init__(self, cap, rate): self.cap = cap; self.rate = rate; self.tokens = cap; self.last = time.monotonic()\n    def consume(self): ...\n```",
            "reason": "Requests specific Python class code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Nginx limit_req module implement rate limiting based on the leaky bucket algorithm?",
            "response": "Nginx limit_req uses a leaky bucket where requests enter a queue and are drained at a fixed rate, delaying requests or permitting bursts only up to a configured burst parameter.",
            "reason": "Asks about Nginx leaky bucket vs Token bucket algorithm.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What HTTP headers must an API return to inform clients of their remaining tokens and reset time under RFC 6585?",
            "response": "Standard headers include `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (or `X-RateLimit-*`), and `Retry-After: <seconds>` on HTTP 429 rejections.",
            "reason": "Asks about standard RateLimit HTTP response headers.",
        },
    },

    # 123. Leaky Bucket Rate Limiting
    {
        "intent_group_id": "ig_api_leaky_bucket_rate_limiting",
        "domain": "api_design",
        "subtopic": "RateLimiting",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "rate_limiting"}},
        "canonical_query": "How does the Leaky Bucket rate-limiting algorithm smooth irregular traffic bursts into a constant outflow rate?",
        "canonical_response": "The Leaky Bucket represents a FIFO queue with a fixed capacity. Incoming requests enter the queue at arbitrary bursty rates. The bucket 'leaks' requests out to backend services at a strictly constant, smoothed outflow rate. If requests arrive faster than the leak rate and the bucket overflows its capacity, incoming requests are immediately dropped or rejected with HTTP 429.",
        "positive_paraphrase_1": {
            "query": "Explain how the Leaky Bucket queue smooths bursty incoming requests into a steady outflow stream.",
            "reason": "Direct paraphrase explaining traffic smoothing and constant outflow in leaky bucket.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Leaky Bucket algorithm protect downstream databases from sudden spikes in traffic?",
            "reason": "Syntactic variation focusing on downstream protection from sudden traffic spikes.",
        },
        "hard_negative_diff_operation": {
            "query": "What is the primary operational difference between Token Bucket and Leaky Bucket algorithms?",
            "response": "Token Bucket permits instantaneous bursts to pass immediately if tokens exist, while Leaky Bucket strictly throttles outflow to a constant rate, introducing queue delay on bursts.",
            "reason": "Contrasts Token Bucket with Leaky Bucket rather than explaining Leaky Bucket mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python async worker queue that pulls items from an asyncio.Queue at a fixed rate of 10 items per second.",
            "response": "```python\nasync def worker(queue):\n    while True:\n        item = await queue.get()\n        await process(item)\n        await asyncio.sleep(0.1)\n```",
            "reason": "Requests specific async queue code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Uber's ratelimit Go package implement leaky bucket traffic shaping?",
            "response": "Uber's ratelimit package calculates the inter-request time delta and uses sleep to delay execution, ensuring callers space their requests out evenly in time.",
            "reason": "Asks about Uber's Go package implementation vs general leaky bucket concept.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Leaky Bucket increase API response latency for legitimate clients during traffic bursts?",
            "response": "Because requests are placed into a FIFO queue and delayed until prior requests leak out, adding queue waiting time to end-to-end client latency.",
            "reason": "Asks about latency penalties introduced by queuing in Leaky Bucket.",
        },
    },

    # 124. Sliding Window Rate Limiting
    {
        "intent_group_id": "ig_api_sliding_window_rate_limiting",
        "domain": "api_design",
        "subtopic": "RateLimiting",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "rate_limiting"}},
        "canonical_query": "How does Sliding Window Counter rate limiting eliminate the boundary burst flaw of Fixed Window algorithms?",
        "canonical_response": "Fixed window counters allow up to 2x the limit if requests arrive at the end of window 1 and the beginning of window 2 (boundary burst). Sliding window counter estimates request density by weighting the previous window count by the overlap percentage: `count = current_count + prev_count * ((window_size - elapsed) / window_size)`. This smooths the rate limit across rolling boundaries.",
        "positive_paraphrase_1": {
            "query": "Explain how the sliding window counter algorithm prevents 2x traffic spikes at window reset boundaries.",
            "reason": "Direct paraphrase explaining how sliding window counters prevent 2x boundary spikes.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does weighted historical window blending achieve memory-efficient sliding rate limiting in Redis?",
            "reason": "Syntactic variation focusing on weighted window blending and memory efficiency.",
        },
        "hard_negative_diff_operation": {
            "query": "How does a Sliding Window Log using Redis Sorted Sets (ZADD timestamp) compare to Sliding Window Counter?",
            "response": "Sliding Window Log records every individual request timestamp in a ZSET and trims with ZREMRANGEBYSCORE, achieving 100% precision at the cost of high memory usage per user.",
            "reason": "Compares sliding window log to sliding window counter rather than explaining boundary burst resolution.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python function implementing the weighted sliding window counter formula with current and previous window counts.",
            "response": "```python\ndef is_allowed(prev_cnt, curr_cnt, window_size, elapsed, limit):\n    weight = (window_size - elapsed) / window_size\n    return (curr_cnt + prev_cnt * weight) < limit\n```",
            "reason": "Requests specific Python calculation function rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Cloudflare rate limiting engine implement sliding window tracking at edge PoPs?",
            "response": "Cloudflare tracks request rates across edge servers using sliding window counters with probabilistic sampling and syncs state across data centers via anycast routing.",
            "reason": "Asks about Cloudflare edge architecture vs general sliding window algorithm.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is sliding window counter considered an approximation rather than an exact count of rolling requests?",
            "response": "It assumes requests in the previous window were distributed uniformly; if all previous requests occurred in the final second, the weighted count slightly underestimates actual traffic burstiness.",
            "reason": "Asks about the uniform distribution approximation assumption.",
        },
    },

    # 125. Webhook HMAC Signatures
    {
        "intent_group_id": "ig_api_webhook_signature_verification",
        "domain": "api_design",
        "subtopic": "Webhooks",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "webhooks"}},
        "canonical_query": "How do webhook publishers use HMAC-SHA256 signatures and timestamp headers to guarantee authenticity and prevent replay attacks?",
        "canonical_response": "The publisher hashes the raw request body concatenated with a UNIX timestamp using a shared secret key via HMAC-SHA256, sending the signature and timestamp in headers (e.g. `X-Signature`, `X-Timestamp`). The consumer hashes the received raw body and timestamp with its secret, verifies equality using constant-time comparison, and rejects requests whose timestamp is older than 5 minutes to prevent replay attacks.",
        "positive_paraphrase_1": {
            "query": "Explain how HMAC-SHA256 signatures and timestamp verification validate webhook authenticity and block replays.",
            "reason": "Direct paraphrase explaining HMAC verification and replay prevention.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does signing webhook payloads with a pre-shared secret prevent spoofing and payload tampering?",
            "reason": "Syntactic variation focusing on preventing spoofing and tampering via secret signing.",
        },
        "hard_negative_diff_operation": {
            "query": "Why must webhook signature verification use constant-time string comparison (hmac.compare_digest)?",
            "response": "Standard string equality `==` short-circuits on the first mismatched byte, leaking execution time information that allows an attacker to deduce the valid signature byte-by-byte via timing attacks.",
            "reason": "Asks specifically about constant-time comparison timing attacks rather than general webhook verification.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python verification function that verifies a Stripe or GitHub webhook signature header against a raw body.",
            "response": "```python\ndef verify_webhook(raw_body, sig_header, secret):\n    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()\n    return hmac.compare_digest(expected, sig_header)\n```",
            "reason": "Requests specific Python verification code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does asymmetric public-key signing (Ed25519) compare to symmetric HMAC-SHA256 for webhooks?",
            "response": "Asymmetric signatures allow consumers to verify payloads using the publisher's public key without sharing a secret, preventing a compromised consumer from forging messages to other consumers.",
            "reason": "Asks about asymmetric Ed25519 vs symmetric HMAC for webhooks.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does parsing the webhook payload into a JSON object before verifying the HMAC signature cause verification failure?",
            "response": "JSON parsers reorder keys, modify whitespace, and alter float formatting; hashing the parsed and re-stringified JSON produces a completely different SHA-256 digest than the original raw bytes.",
            "reason": "Asks about raw byte verification failure from JSON parsing (common implementation pitfall).",
        },
    },

    # 126. Webhook Retry Backoff
    {
        "intent_group_id": "ig_api_webhook_retry_backoff",
        "domain": "api_design",
        "subtopic": "Webhooks",
        "task_kind": "explanation",
        "dimensions": {"api_style": {"status": "known", "value": "webhooks"}},
        "canonical_query": "How do webhook delivery systems use exponential backoff with full jitter to avoid overwhelming recovering client servers?",
        "canonical_response": "When a destination server returns an error or times out, the webhook system retries delivery using exponential backoff: `delay = base * 2^attempt`. Full jitter randomizes the delay between 0 and the exponential cap: `sleep = random(0, delay)`. This breaks synchronization across thousands of failed webhooks, preventing synchronized retry waves from crashing recovering destination servers.",
        "positive_paraphrase_1": {
            "query": "Explain how exponential backoff with jitter prevents the thundering herd retry storm on recovering webhook receivers.",
            "reason": "Direct paraphrase explaining exponential backoff with jitter preventing retry storms.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does adding randomness (jitter) to webhook retry intervals desynchronize concurrent delivery attempts?",
            "reason": "Syntactic variation focusing on desynchronizing retry attempts with jitter.",
        },
        "hard_negative_diff_operation": {
            "query": "How does a Dead Letter Queue (DLQ) handle webhooks that fail all configured retry attempts (e.g. after 72 hours)?",
            "response": "The delivery engine marks the event as failed, writes it to a persistent DLQ for inspection, alerts operators, and optionally disables the user's webhook endpoint if consecutive failures persist.",
            "reason": "Asks how DLQs handle permanently failed webhooks rather than explaining exponential backoff with jitter.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python retry scheduler function calculating exponential backoff with full jitter for attempt N.",
            "response": "```python\nimport random\ndef calc_backoff(attempt, base=1.0, max_delay=3600):\n    temp = min(max_delay, base * (2 ** attempt))\n    return random.uniform(0, temp)\n```",
            "reason": "Requests specific backoff calculation code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does AWS SQS message delay and redrive policy compare to custom webhook retry engines?",
            "response": "AWS SQS uses visibility timeouts and Dead Letter Queues with static retry counts, whereas specialized webhook systems support multi-day exponential schedules with custom status code routing.",
            "reason": "Asks about AWS SQS redrive vs webhook retry engines.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Which HTTP status codes (like 400 Bad Request or 404 Not Found) should deliberately NOT trigger webhook retries?",
            "response": "Client errors indicating permanent invalid state (400, 404, 410 Gone) should not be retried because re-sending the same payload will never succeed; only transient errors (500, 502, 503, 504, 429) warrant retries.",
            "reason": "Asks which status codes should abort retries (retry policy rules).",
        },
    },
]
