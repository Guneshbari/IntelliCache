"""
Domain 5: Protocols and Networking (21 intent groups).
Covers HTTP/2, HTTP/3, WebSockets, SSE, TLS, TCP/UDP, gRPC, DNS, NAT, and Web Security Headers.
"""

from typing import Any, Dict, List

NET_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 85. HTTP/2 Multiplexing
    {
        "intent_group_id": "ig_net_http2_multiplexing",
        "domain": "protocols_and_networking",
        "subtopic": "HTTP",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "http2"}},
        "canonical_query": "How does HTTP/2 binary framing and multiplexing eliminate HTTP/1.1 head-of-line blocking on TCP connections?",
        "canonical_response": "In HTTP/1.1, requests on a single TCP connection must be processed sequentially; a slow response blocks all subsequent requests. HTTP/2 splits messages into independent binary frames tagged with a stream ID. Frames from multiple concurrent streams interleave across a single TCP connection and reassemble at the receiver, eliminating application-layer head-of-line blocking.",
        "positive_paraphrase_1": {
            "query": "Explain how HTTP/2 binary framing interleaves streams over one TCP socket to prevent request blocking.",
            "reason": "Direct paraphrase explaining binary framing and stream interleaving in HTTP/2.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does HTTP/2 multiplexing resolve HTTP/1.1 request-response pipelining head-of-line stalls?",
            "reason": "Syntactic variation focusing on resolving HTTP/1.1 pipelining stalls.",
        },
        "hard_negative_diff_operation": {
            "query": "Why does HTTP/2 still suffer from TCP transport-layer head-of-line blocking during packet loss?",
            "reason": "Asks why TCP-layer packet loss still causes stalls rather than explaining binary framing multiplexing.",
            "response": "Because HTTP/2 runs over a single TCP byte stream; if a single TCP packet is dropped, the OS TCP stack pauses all multiplexed HTTP/2 streams until the missing packet is retransmitted.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Nginx configuration to enable HTTP/2 on port 443 with an SSL certificate.",
            "response": "```nginx\nserver {\n    listen 443 ssl http2;\n    server_name example.com;\n    ssl_certificate /path/cert.pem;\n    ssl_certificate_key /path/key.pem;\n}\n```",
            "reason": "Requests specific Nginx configuration directives rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does HTTP/3 over QUIC compare to HTTP/2 over TCP regarding transport-layer packet loss?",
            "response": "QUIC runs over UDP and manages independent streams at the transport layer; dropping a packet on stream A delays only stream A, allowing other streams to continue without pause.",
            "reason": "Asks about HTTP/3 QUIC vs HTTP/2 TCP transport packet loss.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "How does HPACK header compression in HTTP/2 prevent CRIME and BREACH compression attacks?",
            "response": "HPACK avoids dynamic compression on sensitive headers (marked never-indexed) and uses static Huffman tables and indexed header tables to prevent side-channel payload size inference.",
            "reason": "Asks about HPACK compression security rather than binary framing multiplexing.",
        },
    },

    # 86. HTTP/2 vs HTTP/3 QUIC
    {
        "intent_group_id": "ig_net_http2_vs_http3_quic",
        "domain": "protocols_and_networking",
        "subtopic": "HTTP",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "http3"}},
        "canonical_query": "Why does HTTP/3 utilize the QUIC protocol over UDP instead of TCP to eliminate transport-layer head-of-line blocking?",
        "canonical_response": "While HTTP/2 multiplexes streams on top of TCP, TCP enforces in-order byte delivery for the entire connection; a single lost TCP packet stalls all HTTP/2 streams. HTTP/3 runs over QUIC on UDP, implementing stream multiplexing, flow control, and loss recovery directly in the QUIC transport layer. Each stream is isolated, so packet loss on one stream does not impact others.",
        "positive_paraphrase_1": {
            "query": "Explain how QUIC over UDP in HTTP/3 isolates packet loss per stream and eliminates TCP transport stalls.",
            "reason": "Direct paraphrase explaining how QUIC over UDP isolates packet loss.",
        },
        "positive_paraphrase_2": {
            "query": "What is the primary architectural advantage of HTTP/3's UDP-based QUIC transport over HTTP/2's TCP connection?",
            "reason": "Syntactic variation focusing on architectural advantages of UDP/QUIC over TCP.",
        },
        "hard_negative_diff_operation": {
            "query": "How does QUIC achieve 0-RTT connection establishment on repeated client connections?",
            "response": "QUIC merges the transport handshake and TLS 1.3 handshake into a single exchange; returning clients use previously negotiated session tickets to send encrypted application data in the very first packet.",
            "reason": "Asks specifically about 0-RTT connection resumption rather than head-of-line blocking elimination.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the curl command and HTTP headers to verify if a website supports HTTP/3 via Alt-Svc headers.",
            "response": "```bash\ncurl -I --http3 https://cloudflare.com\n# check for Alt-Svc: h3=\":443\"; ma=86400\n```",
            "reason": "Requests specific curl command and header inspection rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does WebRTC SCTP data channels over UDP compare to HTTP/3 QUIC streams?",
            "response": "SCTP over DTLS provides unordered or partially ordered datagram delivery between peer browsers, whereas QUIC provides structured bidirectional byte streams for client-server web delivery.",
            "reason": "Asks about WebRTC SCTP vs HTTP/3 QUIC.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why do some enterprise corporate firewalls block UDP port 443 and break HTTP/3 connections?",
            "response": "Many legacy firewalls restrict outbound UDP traffic to prevent DDoS attacks or DNS tunneling, forcing clients to fall back to TCP-based HTTP/2 or HTTP/1.1.",
            "reason": "Asks about network firewall UDP blocking (network deployment issue).",
        },
    },

    # 87. WebSocket Handshake
    {
        "intent_group_id": "ig_net_websocket_handshake",
        "domain": "protocols_and_networking",
        "subtopic": "WebSockets",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "websocket"}},
        "canonical_query": "How does the WebSocket HTTP 101 Switching Protocols upgrade handshake transition an HTTP connection to a full-duplex socket?",
        "canonical_response": "The client initiates an HTTP/1.1 GET with headers `Upgrade: websocket`, `Connection: Upgrade`, and a random base64 `Sec-WebSocket-Key`. The server computes a SHA-1 hash of the key concatenated with a fixed GUID (258EAFA5-E914-47DA-95CA-C5AB0DC85B11) and responds with `HTTP/1.1 101 Switching Protocols` and `Sec-WebSocket-Accept`. Both sides then switch to binary WebSocket framing.",
        "positive_paraphrase_1": {
            "query": "Explain how the Sec-WebSocket-Key and HTTP 101 Switching Protocols header exchange establishes a WebSocket connection.",
            "reason": "Direct paraphrase explaining the Sec-WebSocket-Key and HTTP 101 upgrade handshake.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the initial HTTP upgrade request negotiate a persistent bidirectional WebSocket tunnel?",
            "reason": "Syntactic variation focusing on HTTP upgrade negotiation of persistent tunnels.",
        },
        "hard_negative_diff_operation": {
            "query": "Why does the WebSocket protocol require all client-to-server frames to be masked with a 4-byte XOR mask?",
            "response": "Masking randomizes frame payloads on the network to prevent malicious web pages from injecting predictable byte sequences that could trick intermediary caching proxies into cache poisoning attacks.",
            "reason": "Asks about 4-byte client payload masking security rather than the HTTP 101 upgrade handshake.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Node.js raw net.createServer script that parses the Sec-WebSocket-Key and sends the 101 response header.",
            "response": "```javascript\nconst crypto = require('crypto');\nconst key = req.headers['sec-websocket-key'];\nconst accept = crypto.createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');\n// send HTTP/1.1 101\n```",
            "reason": "Requests specific raw socket implementation code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does WebTransport over HTTP/3 compare to traditional WebSocket connections?",
            "response": "WebTransport supports multiple multiplexed unidirectional and bidirectional streams as well as unreliable datagrams over a single QUIC connection, avoiding WebSocket TCP head-of-line blocking.",
            "reason": "Asks about WebTransport over HTTP/3 vs WebSockets.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes Nginx to drop WebSocket connections after 60 seconds if proxy_read_timeout is not tuned?",
            "response": "Nginx proxy_read_timeout defaults to 60s; if no application messages are exchanged within 60s, Nginx assumes the connection is dead and sends a TCP FIN unless heartbeats or timeouts are configured.",
            "reason": "Asks about the 60-second idle timeout drop in Nginx proxies (proxy configuration issue).",
        },
    },

    # 88. WebSocket Heartbeats
    {
        "intent_group_id": "ig_net_websocket_heartbeats",
        "domain": "protocols_and_networking",
        "subtopic": "WebSockets",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "websocket"}},
        "canonical_query": "Why are periodic Ping and Pong frames essential for maintaining long-lived WebSocket connections through intermediate NAT gateways?",
        "canonical_response": "Intermediate stateful firewalls, load balancers, and NAT routers maintain connection tracking tables that drop idle TCP connections after inactivity timeouts (often 30-60s) without sending TCP RST packets (half-open sockets). Periodic WebSocket Ping frames (opcode 0x9) provoke immediate Pong frames (opcode 0xA), refreshing NAT state and actively detecting dead connections.",
        "positive_paraphrase_1": {
            "query": "Explain how WebSocket Ping and Pong frames prevent intermediate routers and NAT from dropping idle connections.",
            "reason": "Direct paraphrase explaining how Ping/Pong frames refresh NAT tables and detect dead sockets.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do heartbeat frames resolve half-open zombie connections in persistent WebSocket architectures?",
            "reason": "Syntactic variation focusing on half-open zombie socket detection.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement client-side automatic reconnection with exponential backoff for closed WebSockets?",
            "response": "On the `onclose` event, initialize a timer with `delay = min(max_delay, base_delay * 2^retries + jitter)` and instantiate a new WebSocket instance when the timer fires.",
            "reason": "Asks how to write client-side reconnection backoff logic rather than explaining Ping/Pong frame mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python websockets server script with ping_interval=20 and ping_timeout=10 configuration.",
            "response": "```python\nimport asyncio, websockets\nasync def echo(ws): async for m in ws: await ws.send(m)\nstart = websockets.serve(echo, '0.0.0.0', 8765, ping_interval=20, ping_timeout=10)\n```",
            "reason": "Requests specific Python configuration code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does TCP Keepalive (SO_KEEPALIVE) at the OS socket layer compare to WebSocket-layer Ping/Pong heartbeats?",
            "response": "TCP keepalives operate at the OS transport layer and are often disabled or tuned to hours by default, whereas WebSocket Ping/Pong operates at the application layer with millisecond precision.",
            "reason": "Asks about OS TCP keepalive vs WebSocket application-layer heartbeats.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a WebSocket client receives an unsolicited Pong frame without having sent a Ping?",
            "response": "Per RFC 6455, an unsolicited Pong frame is valid and serves as a unidirectional heartbeat; the receiver simply reads and ignores it without taking error action or replying.",
            "reason": "Asks about protocol handling of unsolicited Pong frames (specific RFC edge case).",
        },
    },

    # 89. Server-Sent Events (SSE) Reconnection
    {
        "intent_group_id": "ig_net_sse_reconnection",
        "domain": "protocols_and_networking",
        "subtopic": "SSE",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "sse"}},
        "canonical_query": "How does the Server-Sent Events (SSE) protocol use the Last-Event-ID header to resume dropped event streams without message loss?",
        "canonical_response": "In SSE (`text/event-stream`), server events can include an `id: <value>` field. The browser EventSource client records this ID. If the HTTP connection drops, the browser automatically reconnects and includes a `Last-Event-ID: <value>` request header, allowing the server to replay any missed events from its backlog starting after that ID.",
        "positive_paraphrase_1": {
            "query": "Explain how EventSource in browsers uses the id field and Last-Event-ID header for seamless stream reconnection.",
            "reason": "Direct paraphrase explaining id tracking and Last-Event-ID reconnection in SSE.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Server-Sent Events specification provide built-in automatic reconnection and state catchup?",
            "reason": "Syntactic variation focusing on built-in reconnection and state catchup in SSE.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you set custom retry intervals in an SSE stream using the retry: field?",
            "response": "Send `retry: 5000\\n\\n` in the event stream, which configures the browser EventSource to wait 5,000 milliseconds before attempting to reconnect when the stream drops.",
            "reason": "Asks how to set retry intervals via the retry field rather than explaining Last-Event-ID replay.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FastAPI endpoint returning a StreamingResponse that emits SSE events with id, event, and data fields.",
            "response": "```python\n@app.get('/events')\nasync def sse(req: Request):\n    last_id = req.headers.get('last-event-id', 0)\n    async def gen():\n        for i in range(int(last_id)+1, 100):\n            yield f\"id: {i}\\nevent: update\\ndata: {{\"val\": {i}}}\\n\\n\"\n    return StreamingResponse(gen(), media_type='text/event-stream')\n```",
            "reason": "Requests specific FastAPI StreamingResponse code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does gRPC server-side streaming compare to HTTP SSE for real-time data push?",
            "response": "gRPC uses HTTP/2 with binary Protocol Buffers and custom metadata headers, whereas SSE uses standard HTTP/1.1 or HTTP/2 text streams supported natively by browser EventSource.",
            "reason": "Asks about gRPC server streaming vs SSE.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does intermediate proxy buffering (e.g., in Nginx without X-Accel-Buffering: no) completely break SSE streaming?",
            "response": "Proxies buffer responses in chunks (e.g. 4KB-16KB) before forwarding; without disabling buffering, SSE events are held in proxy memory and not delivered in real time to the browser.",
            "reason": "Asks about proxy response buffering breaking real-time SSE delivery (deployment issue).",
        },
    },

    # 90. SSE vs WebSockets
    {
        "intent_group_id": "ig_net_sse_vs_websocket",
        "domain": "protocols_and_networking",
        "subtopic": "RealTimeProtocols",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "sse_vs_ws"}},
        "canonical_query": "What are the architectural trade-offs between Server-Sent Events (SSE) and WebSockets for real-time web applications?",
        "canonical_response": "SSE is unidirectional (server-to-client only), runs over standard HTTP/1.1 or HTTP/2, uses simple text formatting (`text/event-stream`), traverses corporate firewalls effortlessly, and features built-in browser reconnection. WebSockets is bidirectional (full duplex), runs over a dedicated framing protocol, supports raw binary payloads, but requires custom heartbeat and reconnection handling.",
        "positive_paraphrase_1": {
            "query": "Compare Server-Sent Events with WebSockets regarding directionality, protocol complexity, and firewall traversal.",
            "reason": "Direct paraphrase comparing SSE with WebSockets on directionality and complexity.",
        },
        "positive_paraphrase_2": {
            "query": "When should an architect choose SSE over WebSockets for AI chat streaming and live notifications?",
            "reason": "Syntactic variation focusing on architectural choice of SSE vs WebSockets for chat/notifications.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you multiplex multiple logical channels over a single WebSocket connection using STOMP or WAMP protocols?",
            "response": "STOMP formats text frames with COMMANDs like SUBSCRIBE, SEND, and MESSAGE with destination headers, multiplexing topics over a single underlying WebSocket connection.",
            "reason": "Asks how to multiplex channels via STOMP rather than comparing SSE with WebSockets.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the JavaScript client code to connect to an SSE endpoint using EventSource with custom event listeners.",
            "response": "```javascript\nconst es = new EventSource('/stream');\nes.addEventListener('price_update', (e) => console.log(JSON.parse(e.data)));\nes.onerror = () => console.log('error');\n```",
            "reason": "Requests specific browser EventSource JavaScript code rather than architectural comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Long Polling compare to SSE and WebSockets for legacy browser support?",
            "response": "Long Polling repeatedly opens and closes HTTP connections as data becomes available, incurring high header overhead and latency compared to persistent SSE or WebSocket streams.",
            "reason": "Asks about Long Polling vs SSE/WebSockets.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does HTTP/1.1 browser connection pooling limit SSE to a maximum of 6 concurrent streams per domain?",
            "response": "Browsers enforce a strict limit of 6 simultaneous TCP connections per domain under HTTP/1.1; running 6 SSE tabs consumes all sockets, blocking all other HTTP requests unless HTTP/2 is enabled.",
            "reason": "Asks about the HTTP/1.1 6-connection per domain limit bottleneck on SSE.",
        },
    },

    # 91. TLS 1.3 Handshake Flow
    {
        "intent_group_id": "ig_net_tls13_handshake_flow",
        "domain": "protocols_and_networking",
        "subtopic": "TLS",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "tls13"}},
        "canonical_query": "How does the TLS 1.3 handshake reduce connection latency to 1-RTT compared to TLS 1.2?",
        "canonical_response": "In TLS 1.2, establishing a secure connection required 2 roundtrips (ClientHello/ServerHello, followed by KeyExchange and Finished). TLS 1.3 eliminates obsolete cipher suites and mandates ephemeral Diffie-Hellman (ECDHE). The client includes its key shares directly in the ClientHello packet, allowing the server to compute symmetric keys and complete the handshake in a single roundtrip (1-RTT).",
        "positive_paraphrase_1": {
            "query": "Explain how speculative key share exchange in ClientHello achieves a 1-RTT handshake in TLS 1.3.",
            "reason": "Direct paraphrase explaining speculative key sharing and 1-RTT in TLS 1.3.",
        },
        "positive_paraphrase_2": {
            "query": "Why is TLS 1.3 faster than TLS 1.2 during initial connection negotiation over high-latency networks?",
            "reason": "Syntactic variation focusing on latency reduction during connection negotiation.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you inspect the active TLS version and negotiated cipher suite using the openssl s_client CLI tool?",
            "response": "Run `openssl s_client -connect example.com:443 -tls1_3`, which displays the certificate chain, negotiated protocol (TLSv1.3), and cipher suite (e.g. TLS_AES_256_GCM_SHA384).",
            "reason": "Asks for openssl CLI usage rather than explaining the 1-RTT handshake protocol flow.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Python ssl module code to configure an SSLContext that strictly requires TLS 1.3 and disables TLS 1.2.",
            "response": "```python\nimport ssl\nctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)\nctx.minimum_version = ssl.TLSVersion.TLSv1_3\n```",
            "reason": "Requests specific Python ssl configuration code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does WireGuard cryptographic handshake compare to TLS 1.3 in VPN tunnel establishment?",
            "response": "WireGuard uses Noise protocol framework with pre-shared static public keys, achieving a 1-RTT handshake with immediate identity hiding and zero dynamic cipher negotiation.",
            "reason": "Asks about WireGuard Noise handshake vs TLS 1.3.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why was RSA key exchange completely removed from the TLS 1.3 specification?",
            "response": "Static RSA key exchange does not provide Forward Secrecy; if an attacker records encrypted network traffic and compromises the server's private key years later, all historical traffic can be decrypted.",
            "reason": "Asks why static RSA key exchange was deprecated (security justification).",
        },
    },

    # 92. TLS Forward Secrecy
    {
        "intent_group_id": "ig_net_tls_forward_secrecy",
        "domain": "protocols_and_networking",
        "subtopic": "TLS",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "forward_secrecy"}},
        "canonical_query": "How does Perfect Forward Secrecy (PFS) using Ephemeral Diffie-Hellman (ECDHE) protect past sessions from future key compromise?",
        "canonical_response": "PFS generates unique, ephemeral (temporary) public/private key pairs for each individual TLS session. The server uses its long-term private key only to sign the ephemeral key exchange. Once the session terminates and ephemeral keys are discarded from memory, past recorded traffic cannot be decrypted even if the server's long-term private certificate key is stolen.",
        "positive_paraphrase_1": {
            "query": "Explain how ephemeral session keys in ECDHE guarantee that compromised server certificates cannot decrypt recorded past traffic.",
            "reason": "Direct paraphrase explaining forward secrecy protection against retroactive decryption.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does Perfect Forward Secrecy decouple session decryption keys from long-term server identity certificates?",
            "reason": "Syntactic variation focusing on decoupling session keys from identity certificates.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure an Nginx server to only advertise PFS cipher suites like ECDHE-ECDSA-AES256-GCM-SHA384?",
            "response": "In nginx.conf, specify `ssl_ciphers 'ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384';` and `ssl_prefer_server_ciphers on;`.",
            "reason": "Asks for Nginx cipher suite configuration strings rather than explaining mathematical PFS concepts.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script demonstrating Diffie-Hellman key exchange mathematics using modular exponentiation.",
            "response": "```python\np, g = 23, 5\na = 6; A = (g ** a) % p\nb = 15; B = (g ** b) % p\nshared_A = (B ** a) % p; shared_B = (A ** b) % p\nassert shared_A == shared_B\n```",
            "reason": "Requests specific mathematical script code rather than conceptual TLS explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Signal Double Ratchet protocol implement forward secrecy for asynchronous mobile messaging?",
            "response": "Signal ratchets ephemeral Diffie-Hellman keys on every message exchange, deriving new session keys for each message and deleting old keys, guaranteeing forward and backward secrecy.",
            "reason": "Asks about Signal Double Ratchet vs TLS forward secrecy.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does TLS session ticket resumption without rotating session ticket encryption keys (STEKs) undermine Forward Secrecy?",
            "response": "Session tickets encrypt the master secret with a static server key (STEK); if an attacker compromises the STEK, they can decrypt tickets and recover the master keys for all resumed sessions.",
            "reason": "Asks about session ticket key compromise undermining PFS (vulnerability analysis).",
        },
    },

    # 93. TCP Three-Way Handshake
    {
        "intent_group_id": "ig_net_tcp_three_way_handshake",
        "domain": "protocols_and_networking",
        "subtopic": "TCP",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "tcp"}},
        "canonical_query": "What occurs during the TCP three-way handshake (SYN, SYN-ACK, ACK) and why are three steps required?",
        "canonical_response": "1. Client sends SYN with initial sequence number (ISN_c); 2. Server replies with SYN-ACK, acknowledging ISN_c + 1 and providing its own ISN_s; 3. Client replies with ACK acknowledging ISN_s + 1. Three steps are mathematically required because both sides must independently negotiate initial sequence numbers and verify bidirectional transmit and receive paths.",
        "positive_paraphrase_1": {
            "query": "Explain why establishing a reliable bidirectional TCP connection requires exactly three steps: SYN, SYN-ACK, and ACK.",
            "reason": "Direct paraphrase explaining the 3 steps of the TCP handshake and sequence synchronization.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the TCP 3-way handshake synchronize sequence numbers and verify two-way communication?",
            "reason": "Syntactic variation focusing on sequence number synchronization and bidirectional verification.",
        },
        "hard_negative_diff_operation": {
            "query": "How do SYN cookies defend a Linux server against TCP SYN flood denial-of-service attacks?",
            "response": "When the SYN queue overflows, the server encodes connection state into the initial sequence number (ISN) and sends SYN-ACK without allocating server memory, verifying state only when ACK arrives.",
            "reason": "Asks about SYN flood defense via SYN cookies rather than explaining the standard 3-way handshake.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Scapy Python script that crafts and transmits a raw TCP SYN packet to port 80 and prints the response.",
            "response": "```python\nfrom scapy.all import IP, TCP, sr1\nresp = sr1(IP(dst='1.1.1.1')/TCP(dport=80, flags='S'), timeout=2)\nresp.show()\n```",
            "reason": "Requests specific raw packet crafting code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does SCTP 4-way handshake with cryptographic state cookie compare to TCP 3-way handshake?",
            "response": "SCTP uses a 4-way handshake (INIT, INIT-ACK with State Cookie, COOKIE-ECHO, COOKIE-ACK) that allocates zero server memory until the client echoes the cookie, inherently immune to SYN floods.",
            "reason": "Asks about SCTP 4-way handshake vs TCP 3-way handshake.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can a TCP connection NOT be reliably established using only a two-way handshake (SYN and ACK)?",
            "response": "With only two steps, old duplicated SYN packets delayed by the network could trick the server into opening spurious connections, and the client would not confirm the server received its ACK.",
            "reason": "Asks why a two-way handshake fails (theoretical limitation).",
        },
    },

    # 94. TCP Slow Start and Congestion Control
    {
        "intent_group_id": "ig_net_tcp_slow_start_congestion",
        "domain": "protocols_and_networking",
        "subtopic": "TCP",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "tcp"}},
        "canonical_query": "How does TCP Slow Start exponentially grow the congestion window (cwnd) until ssthresh is reached?",
        "canonical_response": "TCP begins a connection with a small congestion window (typically 10 segments). During Slow Start, cwnd doubles every roundtrip time (RTT): for every ACK received, cwnd increments by 1 segment, producing exponential growth. Once cwnd reaches the slow-start threshold (ssthresh), TCP switches to linear Congestion Avoidance (additive increase).",
        "positive_paraphrase_1": {
            "query": "Explain how TCP probe network capacity by doubling cwnd each RTT during the slow start phase.",
            "reason": "Direct paraphrase explaining exponential cwnd doubling and ssthresh transition in TCP.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does TCP transition from exponential slow start to linear congestion avoidance when approaching capacity?",
            "reason": "Syntactic variation focusing on exponential to linear growth transition.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Google BBR congestion control differ from loss-based algorithms like CUBIC and Reno?",
            "response": "BBR measures actual bottleneck bandwidth and roundtrip propagation time directly, maximizing throughput while keeping inflight data minimal to eliminate bufferbloat, rather than treating packet loss as congestion.",
            "reason": "Asks about BBR vs CUBIC rather than explaining standard TCP slow start mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Linux sysctl commands to check and change the default TCP congestion control algorithm to bbr.",
            "response": "```bash\nsysctl net.ipv4.tcp_congestion_control\nsudo sysctl -w net.core.default_qdisc=fq\nsudo sysctl -w net.ipv4.tcp_congestion_control=bbr\n```",
            "reason": "Requests specific Linux sysctl configuration commands rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does QUIC congestion control implementation compare to kernel TCP congestion control?",
            "response": "QUIC congestion control is implemented in user-space inside application binaries, allowing rapid deployment of new algorithms without waiting for operating system kernel updates.",
            "reason": "Asks about user-space QUIC congestion control vs kernel TCP.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes TCP Fast Retransmit to trigger upon receiving three duplicate ACKs?",
            "response": "Receiving three duplicate ACKs strongly implies a specific segment was lost in transit while subsequent segments arrived; TCP retransmits the missing segment immediately without waiting for retransmission timer expiration.",
            "reason": "Asks about Fast Retransmit triggering on duplicate ACKs.",
        },
    },

    # 95. UDP vs TCP Tradeoffs
    {
        "intent_group_id": "ig_net_udp_vs_tcp_tradeoffs",
        "domain": "protocols_and_networking",
        "subtopic": "TransportProtocols",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "transport"}},
        "canonical_query": "What are the architectural trade-offs between UDP connectionless datagrams and TCP reliable byte streams?",
        "canonical_response": "TCP provides reliable, in-order byte stream delivery with flow control, congestion control, and retransmission, at the cost of connection setup latency (3-way handshake), head-of-line blocking, and higher packet overhead (20-byte header). UDP is a lightweight, connectionless datagram protocol (8-byte header) with zero handshake and no retransmissions, ideal for low-latency streaming and gaming.",
        "positive_paraphrase_1": {
            "query": "Compare UDP and TCP regarding reliability, connection overhead, packet ordering, and header size.",
            "reason": "Direct paraphrase comparing UDP with TCP on reliability and overhead.",
        },
        "positive_paraphrase_2": {
            "query": "Why is UDP preferred over TCP for real-time video streaming, DNS lookups, and multiplayer games?",
            "reason": "Syntactic variation focusing on why UDP is chosen for low-latency media and gaming.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement reliable messaging over UDP using application-level sequence numbers and ACK packets?",
            "response": "Attach an incrementing sequence number to each UDP datagram header; receivers reply with ACK datagrams, and senders maintain a timer to retransmit unacknowledged packets.",
            "reason": "Asks how to build a reliable protocol on top of UDP rather than comparing TCP and UDP.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python UDP server and client script using socket.SOCK_DGRAM that transmits JSON telemetry packets.",
            "response": "```python\n# server\ns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\ns.bind(('0.0.0.0', 9999))\nwhile True: data, addr = s.recvfrom(1024)\n```",
            "reason": "Requests specific Python UDP socket code rather than conceptual comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does SCTP (Stream Control Transmission Protocol) combine features of both TCP and UDP?",
            "response": "SCTP is message-oriented like UDP (preserving message boundaries) but provides reliable, in-order or partially ordered multi-stream delivery with multi-homing support like TCP.",
            "reason": "Asks about SCTP vs TCP/UDP.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a UDP packet exceeds the network interface MTU (Maximum Transmission Unit)?",
            "response": "The IP layer fragments the packet into multiple IP fragments; if even a single fragment is dropped by intermediate routers, the entire UDP datagram cannot be reassembled and is dropped.",
            "reason": "Asks about IP fragmentation and packet drop risk when UDP exceeds MTU.",
        },
    },

    # 96. gRPC HTTP/2 Transport
    {
        "intent_group_id": "ig_net_grpc_http2_transport",
        "domain": "protocols_and_networking",
        "subtopic": "RPC",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "grpc"}},
        "canonical_query": "How does gRPC leverage HTTP/2 streams and Protocol Buffers to achieve high-throughput microservice communication?",
        "canonical_response": "gRPC uses Protocol Buffers for strongly-typed, compact binary serialization. It maps remote procedure calls directly onto HTTP/2 streams using POST requests with `content-type: application/grpc`. By multiplexing multiple RPC calls across a single persistent TCP connection and utilizing HTTP/2 trailing headers for status codes, gRPC eliminates HTTP/1.1 serialization and connection overhead.",
        "positive_paraphrase_1": {
            "query": "Explain how gRPC uses HTTP/2 multiplexing, binary framing, and Protobuf encoding for RPC performance.",
            "reason": "Direct paraphrase explaining gRPC's utilization of HTTP/2 and Protobuf.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does gRPC achieve higher efficiency than REST over JSON in distributed systems?",
            "reason": "Syntactic variation focusing on efficiency advantages over REST/JSON.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement client-side load balancing in gRPC across Kubernetes headless services?",
            "response": "Configure the gRPC channel target with `dns:///my-service:50051` and enable round-robin load balancing policy in gRPC client channel options, resolving all backend pod IPs directly.",
            "reason": "Asks how to configure client-side load balancing rather than explaining HTTP/2 transport mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a .proto service definition file with unary, client-streaming, server-streaming, and bidirectional RPC methods.",
            "response": "```protobuf\nsyntax = \"proto3\";\nservice Greeter {\n  rpc SayHello (HelloReq) returns (HelloResp);\n  rpc Chat (stream Msg) returns (stream Msg);\n}\n```",
            "reason": "Requests specific Protobuf schema code rather than conceptual transport explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Apache Thrift binary protocol compare to gRPC over HTTP/2?",
            "response": "Thrift defines its own custom RPC transport protocols (TBufferedTransport, TFramedTransport) over raw TCP, whereas gRPC standardizes entirely on HTTP/2 binary framing and semantics.",
            "reason": "Asks about Apache Thrift vs gRPC.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why do standard L4 TCP load balancers fail to distribute gRPC traffic evenly across backend pods?",
            "response": "Because gRPC multiplexes all RPCs over a single persistent HTTP/2 TCP connection; an L4 balancer routes the TCP connection to a single backend, sending all subsequent RPCs to that same pod.",
            "reason": "Asks about L4 load balancing pinning bottlenecks with gRPC HTTP/2 connections.",
        },
    },

    # 97. DNS Resolution Hierarchy
    {
        "intent_group_id": "ig_net_dns_resolution_hierarchy",
        "domain": "protocols_and_networking",
        "subtopic": "DNS",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "dns"}},
        "canonical_query": "How does the hierarchical DNS resolution process resolve a domain name through Root, TLD, and Authoritative nameservers?",
        "canonical_response": "1. Client queries a Recursive Resolver (like 8.8.8.8); 2. If uncached, the resolver queries a Root Nameserver (.) for the Top-Level Domain (TLD); 3. The Root delegates to the TLD nameserver (.com); 4. The TLD delegates to the domain's Authoritative Nameserver; 5. The Authoritative server returns the A/AAAA record, which the recursive resolver caches and returns.",
        "positive_paraphrase_1": {
            "query": "Explain the step-by-step resolution chain from recursive resolver through Root, TLD, to Authoritative DNS servers.",
            "reason": "Direct paraphrase explaining the DNS hierarchy and recursive resolution steps.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do DNS delegation and TTL caching optimize global domain name lookups?",
            "reason": "Syntactic variation focusing on delegation hierarchy and TTL caching.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you trace the complete DNS delegation path step-by-step using the dig +trace command?",
            "response": "Run `dig +trace example.com`, which queries the root nameservers directly and prints referral NS records down through TLDs to the authoritative server.",
            "reason": "Asks how to use the dig +trace tool rather than explaining the hierarchical resolution architecture.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script using dnspython that queries MX and TXT records for a domain with custom timeout.",
            "response": "```python\nimport dns.resolver\nres = dns.resolver.Resolver()\nanswers = res.resolve('example.com', 'MX')\nfor r in answers: print(r.exchange, r.preference)\n```",
            "reason": "Requests specific dnspython code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does DNS-over-HTTPS (DoH) compare to standard plaintext UDP DNS over port 53?",
            "response": "Standard DNS transmits queries in cleartext over UDP port 53; DoH encrypts DNS queries inside HTTPS/TLS over port 443, preventing eavesdropping and ISP DNS manipulation.",
            "reason": "Asks about DoH encryption vs standard UDP DNS.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a domain's DNS TTL is set to 0 seconds or several days?",
            "response": "TTL=0 disables caching, forcing resolvers to query authoritative servers on every lookup (high latency/server load). A multi-day TTL delays propagation of IP changes for days during outages.",
            "reason": "Asks about extreme TTL trade-offs (0s vs days).",
        },
    },

    # 98. NAT Traversal (STUN / TURN)
    {
        "intent_group_id": "ig_net_nat_traversal_stun_turn",
        "domain": "protocols_and_networking",
        "subtopic": "Networking",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "nat"}},
        "canonical_query": "How do STUN and TURN protocols enable peer-to-peer WebRTC connections across restrictive NAT gateways?",
        "canonical_response": "STUN allows a client behind NAT to discover its public IP and mapped port by querying an external STUN server, enabling direct peer-to-peer UDP hole punching. If symmetric NAT prevents direct hole punching, TURN acts as an in-path relay server, forwarding media traffic between peers over a public relay address.",
        "positive_paraphrase_1": {
            "query": "Explain the difference between STUN hole punching and TURN relay servers in WebRTC ICE negotiation.",
            "reason": "Direct paraphrase contrasting STUN hole punching with TURN relay servers.",
        },
        "positive_paraphrase_2": {
            "query": "Why is a TURN relay necessary when peers are located behind restrictive symmetric NAT routers?",
            "reason": "Syntactic variation focusing on symmetric NAT restrictions requiring TURN.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure ICE servers in a WebRTC RTCPeerConnection configuration in JavaScript?",
            "response": "```javascript\nconst pc = new RTCPeerConnection({\n  iceServers: [\n    { urls: 'stun:stun.l.google.com:19302' },\n    { urls: 'turn:turn.example.com', username: 'u', credential: 'p' }\n  ]\n});\n```",
            "reason": "Asks for JavaScript RTCPeerConnection config rather than explaining protocol mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the coturn configuration file turnserver.conf setting up listening port 3478 and realm.",
            "response": "```text\nlistening-port=3478\nrealm=example.com\nuse-auth-secret\nstatic-auth-secret=secretkey\n```",
            "reason": "Requests specific coturn configuration file lines rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does UPnP (Universal Plug and Play) port forwarding compare to STUN NAT traversal?",
            "response": "UPnP instructs a local gateway router to open a port mapping via local network RPCs, while STUN discovers external mappings without reconfiguring router settings.",
            "reason": "Asks about UPnP vs STUN.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does symmetric NAT make STUN hole punching impossible between two peers?",
            "response": "Symmetric NAT allocates a new, unpredictable external port for every distinct destination IP/port; the mapped port returned by the STUN server cannot be reached by a different peer IP.",
            "reason": "Asks why symmetric NAT breaks STUN hole punching.",
        },
    },

    # 99. CIDR Subnet Calculation
    {
        "intent_group_id": "ig_net_cidr_subnet_calculation",
        "domain": "protocols_and_networking",
        "subtopic": "Networking",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "cidr"}},
        "canonical_query": "How does CIDR prefix notation (like /24 or /27) determine network masks and usable host IP addresses?",
        "canonical_response": "CIDR prefix /n indicates the first n bits of a 32-bit IPv4 address are the network prefix. The remaining (32 - n) bits identify hosts, giving 2^(32-n) total addresses. The first address is the Network Address and the last is the Broadcast Address, so total usable host addresses is `2^(32-n) - 2` (e.g. /24 has 254 usable hosts; /27 has 30 usable hosts).",
        "positive_paraphrase_1": {
            "query": "Explain how to calculate total and usable IPv4 host addresses from a CIDR prefix length.",
            "reason": "Direct paraphrase explaining CIDR subnet calculations for total and usable addresses.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do subnet masks and CIDR slash notation divide IP address space into network and host bits?",
            "reason": "Syntactic variation focusing on network vs host bit division via CIDR.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you calculate subnets programmatically in Python using the ipaddress standard library?",
            "response": "```python\nimport ipaddress\nnet = ipaddress.ip_network('192.168.1.0/24')\nprint(net.num_addresses, list(net.subnets(new_prefix=26)))\n```",
            "reason": "Asks for Python ipaddress module code rather than explaining binary CIDR mathematics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Divide the network 10.0.0.0/16 into 4 equal-sized subnets, listing network, broadcast, and usable ranges.",
            "response": "Subnets are /18: 10.0.0.0/18 (usable 10.0.0.1 - 10.0.63.254), 10.0.64.0/18, 10.0.128.0/18, 10.0.192.0/18.",
            "reason": "Requests specific subnet calculation exercises rather than explaining the general CIDR mechanism.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does IPv6 /64 prefix allocation compare to IPv4 /24 subnetting?",
            "reason": "Asks about IPv6 64-bit interface identifier sizing vs IPv4 CIDR.",
            "response": "IPv6 standard subnets are fixed at /64 (leaving 64 bits for SLAAC host interface IDs), providing 1.8x10^19 addresses per subnet and eliminating IPv4 address conservation concerns.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does AWS VPC reserve 5 IP addresses in every subnet instead of the standard 2 addresses?",
            "response": "AWS reserves the first 4 addresses (network, VPC router, DNS resolver, future use) and the last address (broadcast), leaving `2^(32-n) - 5` usable IPs in an AWS subnet.",
            "reason": "Asks about cloud provider (AWS) reservation overrides rather than standard RFC math.",
        },
    },

    # 100. MTU vs MSS and Fragmentation
    {
        "intent_group_id": "ig_net_mtu_mss_fragmentation",
        "domain": "protocols_and_networking",
        "subtopic": "Networking",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "mtu_mss"}},
        "canonical_query": "What is the relationship between Maximum Transmission Unit (MTU), Maximum Segment Size (MSS), and IP packet fragmentation?",
        "canonical_response": "MTU is the maximum size (typically 1500 bytes for Ethernet) of an IP packet that can pass over a physical network link without fragmentation. MSS is the maximum TCP payload size negotiated during the handshake, calculated as `MTU - 20 (IP header) - 20 (TCP header) = 1460 bytes`. If an IP packet exceeds the link MTU and has DF (Don't Fragment) set, it is dropped.",
        "positive_paraphrase_1": {
            "query": "Explain how MSS is derived from MTU and why exceeding MTU causes packet fragmentation or drops.",
            "reason": "Direct paraphrase explaining MTU, MSS derivation, and fragmentation.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do TCP header overheads determine MSS from link MTU sizes?",
            "reason": "Syntactic variation focusing on header overhead subtraction from MTU.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Path MTU Discovery (PMTUD) discover the smallest MTU along a network path using ICMP Type 3 Code 4?",
            "response": "PMTUD sends packets with the DF bit set; if an intermediate router cannot forward without fragmentation, it drops the packet and replies with ICMP 'Fragmentation Needed' including its link MTU.",
            "reason": "Asks specifically about PMTUD ICMP messaging rather than MTU/MSS definitions.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the iptables command to clamp TCP MSS to Path MTU (TCPMSS --clamp-mss-to-pmtu).",
            "response": "```bash\niptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n```",
            "reason": "Requests specific iptables clamping command rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Jumbo Frames (9000 byte MTU) in data center networks compare to standard 1500 MTU?",
            "response": "Jumbo Frames increase payload capacity per packet from 1500 to 9000 bytes, reducing CPU interrupt rates and packet processing overhead by 6x for high-throughput SAN storage.",
            "reason": "Asks about Jumbo Frames vs standard MTU.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens when a network firewall blocks ICMP Type 3 packets (causing a PMTUD black hole)?",
            "response": "The sender never receives the 'Fragmentation Needed' ICMP notification and keeps retransmitting large packets that get silently dropped, causing TCP connections to hang on large transfers.",
            "reason": "Asks about PMTUD black hole hangs from blocked ICMP (failure mode).",
        },
    },

    # 101. CORS Preflight OPTIONS
    {
        "intent_group_id": "ig_net_cors_preflight_options",
        "domain": "protocols_and_networking",
        "subtopic": "WebSecurity",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "cors"}},
        "canonical_query": "Why do browsers send an HTTP preflight OPTIONS request before executing cross-origin requests?",
        "canonical_response": "For cross-origin requests that are not 'simple' (e.g. using methods like PUT/DELETE, or custom headers like Authorization or Content-Type: application/json), browsers send an OPTIONS preflight request to check if the target server understands CORS and permits the method and headers, preventing legacy servers from executing unauthorized state-changing operations.",
        "positive_paraphrase_1": {
            "query": "Explain why browsers execute an OPTIONS preflight check before non-simple cross-origin API calls.",
            "reason": "Direct paraphrase explaining non-simple CORS preflight checks.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the CORS preflight handshake protect web servers from unexpected cross-origin requests?",
            "reason": "Syntactic variation focusing on server protection via preflight checks.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you cache CORS preflight responses in the browser using the Access-Control-Max-Age header?",
            "response": "Include `Access-Control-Max-Age: 86400` in the preflight OPTIONS response, which instructs the browser to cache preflight permission for 24 hours without re-sending OPTIONS for subsequent requests.",
            "reason": "Asks how to cache preflights using Max-Age rather than explaining why preflights are sent.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the raw HTTP response headers for a successful preflight check allowing GET, POST, PUT with Authorization header.",
            "response": "```http\nHTTP/1.1 204 No Content\nAccess-Control-Allow-Origin: https://app.example.com\nAccess-Control-Allow-Methods: GET, POST, PUT\nAccess-Control-Allow-Headers: Authorization, Content-Type\n```",
            "reason": "Requests specific raw HTTP header code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does JSONP (JSON with Padding) bypass cross-origin restrictions compared to standard CORS?",
            "response": "JSONP exploited the fact that HTML `<script>` tags were not restricted by the Same-Origin Policy, loading executable JavaScript callbacks instead of modern CORS headers.",
            "reason": "Asks about legacy JSONP vs modern CORS.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Which HTTP methods and headers qualify as 'simple requests' that do NOT trigger a CORS preflight?",
            "response": "Simple requests use GET, POST, or HEAD with safe headers (Accept, Accept-Language, Content-Language) and Content-Type restricted to application/x-www-form-urlencoded, multipart/form-data, or text/plain.",
            "reason": "Asks specifically for the definition of simple requests that avoid preflight.",
        },
    },

    # 102. Content Security Policy (CSP)
    {
        "intent_group_id": "ig_net_csp_content_security_policy",
        "domain": "protocols_and_networking",
        "subtopic": "WebSecurity",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "csp"}},
        "canonical_query": "How does the Content-Security-Policy (CSP) HTTP response header mitigate Cross-Site Scripting (XSS) attacks?",
        "canonical_response": "CSP allows server operators to declare an allowlist of approved resource origins (scripts, images, stylesheets). Browsers enforce these rules by refusing to execute inline scripts (disabling inline `<script>` and `eval()` unless signed with a nonce or cryptographic hash) and blocking scripts loaded from unauthorized third-party domains.",
        "positive_paraphrase_1": {
            "query": "Explain how Content-Security-Policy restrictions on inline scripts and allowed domains prevent XSS execution.",
            "reason": "Direct paraphrase explaining how CSP headers block inline scripts and third-party XSS.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does CSP header enforcement protect browser contexts from malicious script injection?",
            "reason": "Syntactic variation focusing on script injection prevention via CSP.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you generate and validate a per-request cryptographic nonce in CSP script-src directives?",
            "response": "Generate a cryptographically random base64 string on the server, include it in the header `script-src 'nonce-RANDOM'`, and attach `nonce=\"RANDOM\"` to legitimate script tags.",
            "reason": "Asks how to generate and attach CSP nonces rather than explaining general XSS mitigation.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete production CSP header string that allows self, Google Fonts, and reports violations to an endpoint.",
            "response": "```http\nContent-Security-Policy: default-src 'self'; font-src https://fonts.gstatic.com; style-src 'self' https://fonts.googleapis.com; report-to /csp-violations\n```",
            "reason": "Requests specific CSP header string syntax rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Subresource Integrity (SRI) compare to Content Security Policy for CDN script security?",
            "response": "SRI verifies that a fetched script matches a specific SHA-384 hash to prevent CDN tampering, while CSP defines which domains scripts can be loaded from.",
            "reason": "Asks about SRI vs CSP.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does adding 'unsafe-inline' or 'unsafe-eval' to a CSP directive effectively neutralize its XSS protection?",
            "response": "'unsafe-inline' allows the browser to execute any arbitrary `<script>` or event handler injected by an attacker into HTML, completely defeating CSP's primary defense.",
            "reason": "Asks about neutralizing CSP with unsafe keywords (misconfiguration vulnerability).",
        },
    },

    # 103. HSTS Header
    {
        "intent_group_id": "ig_net_hsts_strict_transport",
        "domain": "protocols_and_networking",
        "subtopic": "WebSecurity",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "hsts"}},
        "canonical_query": "How does HTTP Strict Transport Security (HSTS) prevent SSL-stripping attacks and man-in-the-middle exploits?",
        "canonical_response": "HSTS communicates via the `Strict-Transport-Security: max-age=31536000; includeSubDomains` header. Once received, the browser automatically rewrites all plaintext `http://` links to `https://` internally before sending packets over the network, and refuses to allow users to bypass TLS certificate error warnings, defeating SSL-stripping proxies.",
        "positive_paraphrase_1": {
            "query": "Explain how HSTS forces browsers to use HTTPS and eliminates SSL-stripping MITM attacks.",
            "reason": "Direct paraphrase explaining how HSTS enforces HTTPS and blocks SSL stripping.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Strict-Transport-Security response header prevent insecure HTTP downgrades?",
            "reason": "Syntactic variation focusing on preventing HTTP downgrade attacks.",
        },
        "hard_negative_diff_operation": {
            "query": "What are the requirements to submit a domain to the Chrome HSTS Preload list (hstspreload.org)?",
            "response": "The domain must serve a valid TLS certificate, redirect HTTP to HTTPS, have `max-age` of at least 1 year (31536000), include `includeSubDomains`, and include the `preload` token.",
            "reason": "Asks for submission criteria for the preload list rather than explaining how HSTS works.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Nginx configuration snippet to add an HSTS header with a 1-year duration and preload enabled.",
            "response": "```nginx\nadd_header Strict-Transport-Security \"max-age=31536000; includeSubDomains; preload\" always;\n```",
            "reason": "Requests specific Nginx configuration syntax rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does DNS CAA (Certification Authority Authorization) records compare to HSTS headers?",
            "response": "CAA records specify which Certificate Authorities are allowed to issue TLS certificates for a domain, whereas HSTS instructs client browsers to only connect over HTTPS.",
            "reason": "Asks about DNS CAA records vs HSTS headers.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What vulnerability exists during a user's very first HTTP request to an unvisited domain without HSTS preloading?",
            "reason": "Asks about the Trust-On-First-Use (TOFU) vulnerability on first visits.",
            "response": "During the very first connection, the browser does not yet know the domain uses HSTS and sends an initial plaintext HTTP request, which an active attacker can intercept and strip.",
        },
    },

    # 104. Cookie SameSite Attribute
    {
        "intent_group_id": "ig_net_cookie_samesite_security",
        "domain": "protocols_and_networking",
        "subtopic": "WebSecurity",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "cookies"}},
        "canonical_query": "What is the difference between SameSite=Strict, Lax, and None cookie attributes for preventing CSRF attacks?",
        "canonical_response": "SameSite=Strict sends cookies only for first-party same-site requests, never on external cross-site links. SameSite=Lax (the browser default) permits cookies on top-level safe GET navigations from external sites but withholds them on cross-site POST requests. SameSite=None sends cookies on all cross-site requests but requires the Secure flag (HTTPS).",
        "positive_paraphrase_1": {
            "query": "Compare SameSite=Strict, SameSite=Lax, and SameSite=None behavior regarding cross-site cookie transmission.",
            "reason": "Direct paraphrase comparing Strict, Lax, and None cookie attributes.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how the SameSite cookie attribute prevents Cross-Site Request Forgery (CSRF) vulnerabilities.",
            "reason": "Syntactic variation focusing on CSRF protection via SameSite.",
        },
        "hard_negative_diff_operation": {
            "query": "How does the Double Submit Cookie pattern prevent CSRF when SameSite cookies cannot be used?",
            "response": "The server sets a random token in a cookie and requires the client to read the token via JavaScript and send it in a custom request header; attackers cannot read cross-origin cookies to mirror the header.",
            "reason": "Asks about the Double Submit Cookie pattern rather than explaining SameSite attributes.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Set-Cookie HTTP header string for a session cookie with HttpOnly, Secure, and SameSite=Strict.",
            "response": "```http\nSet-Cookie: session_id=xyz123; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=3600\n```",
            "reason": "Requests specific Set-Cookie header syntax rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Anti-CSRF Synchronizer Tokens (like in Django) compare to SameSite cookie attributes?",
            "response": "Synchronizer tokens embed a unique secret into HTML forms verified on POST submission, providing server-side defense independent of browser cookie policies.",
            "reason": "Asks about Django synchronizer tokens vs SameSite cookies.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does SameSite=Lax NOT protect against CSRF if state-changing operations are triggered via GET requests?",
            "response": "SameSite=Lax allows cookies on top-level GET navigations; if an API mistakenly performs mutations on GET requests, an attacker can trigger the mutation simply via an `<a>` link or `window.location`.",
            "reason": "Asks about the vulnerability of state-changing GET requests under SameSite=Lax.",
        },
    },

    # 105. HTTP Caching and ETag
    {
        "intent_group_id": "ig_net_http_caching_etag",
        "domain": "protocols_and_networking",
        "subtopic": "HTTP",
        "task_kind": "explanation",
        "dimensions": {"protocol": {"status": "known", "value": "caching"}},
        "canonical_query": "How do HTTP ETags and conditional If-None-Match headers prevent re-transmitting unmodified web resources?",
        "canonical_response": "An ETag is an entity tag (hash or version string) returned in the HTTP response. When the client's cache TTL expires, instead of re-downloading the entire payload, the client sends a conditional request with `If-None-Match: <etag>`. If the resource is unchanged, the server returns `HTTP 304 Not Modified` with zero body bytes, saving network bandwidth.",
        "positive_paraphrase_1": {
            "query": "Explain how If-None-Match headers and 304 Not Modified responses enable conditional HTTP caching.",
            "reason": "Direct paraphrase explaining ETag validation and 304 responses.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do strong and weak ETags allow web caches to revalidate stale resources efficiently?",
            "reason": "Syntactic variation focusing on cache revalidation using ETags.",
        },
        "hard_negative_diff_operation": {
            "query": "What is the difference between strong ETags and weak ETags (prefixed with W/)?",
            "response": "Strong ETags guarantee byte-for-byte binary equality, while weak ETags (W/\"...\") guarantee only semantic equivalence (e.g. identical data with different whitespace or compression).",
            "reason": "Asks specifically to distinguish strong vs weak ETags rather than explaining conditional caching.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python Flask endpoint that calculates the MD5 hash of a response, checks request If-None-Match, and returns 304.",
            "response": "```python\n@app.route('/data')\ndef get_data():\n    etag = hashlib.md5(payload).hexdigest()\n    if request.headers.get('If-None-Match') == etag: return '', 304\n    resp = make_response(payload); resp.set_etag(etag); return resp\n```",
            "reason": "Requests specific Flask conditional request code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Cache-Control: immutable directive compare to conditional ETag revalidation?",
            "response": "`Cache-Control: immutable` tells the browser the file will never change, completely suppressing conditional If-None-Match checks on user reload, unlike ETags which require a roundtrip validation.",
            "reason": "Asks about Cache-Control: immutable vs ETag revalidation.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can different server instances in a load-balanced cluster generate mismatched ETags for the same file?",
            "response": "By default, Apache and Nginx derive ETags using file inode numbers and file modification timestamps (mtime); across different physical servers, inodes differ, causing 304 validation to fail.",
            "reason": "Asks about server cluster inode ETag mismatch bugs.",
        },
    },
]
