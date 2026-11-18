"""
Domain 9: Data Processing (21 intent groups).
Covers Stream Processing & Event Streaming, Serialization Formats & Schemas,
Analytical Storage & Columnar Formats, Distributed Query Execution.
"""

from typing import Any, Dict, List

DATA_PROCESSING_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 169. Reactive Streams Backpressure
    {
        "intent_group_id": "ig_data_reactive_streams_backpressure",
        "domain": "data_processing",
        "subtopic": "Stream Processing & Event Streaming",
        "task_kind": "explanation",
        "dimensions": {"paradigm": {"status": "known", "value": "reactive"}},
        "canonical_query": "How does backpressure in Reactive Streams prevent consumer out-of-memory errors during traffic bursts?",
        "canonical_response": "In traditional push-based streaming, a fast producer pushes data uncontrollably to a slower consumer, causing unbounded memory buffering on the consumer until an OutOfMemoryError occurs. Reactive Streams inverts this model using non-blocking, asynchronous pull-based flow control via the `Subscription.request(n)` protocol. The consumer explicitly signals demand to the producer by requesting exactly $n$ items. The producer is strictly forbidden from emitting more than $n$ items until the consumer processes the current batch and requests more, naturally regulating throughput to consumer capacity.",
        "positive_paraphrase_1": {
            "query": "Explain how pull-based demand signaling in Reactive Streams prevents consumer buffer exhaustion.",
            "reason": "Direct paraphrase explaining pull-based demand signaling and buffer overflow prevention.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Subscription.request(n) protocol allow slow subscribers to throttle fast publishers?",
            "reason": "Syntactic variation focusing on Subscription.request(n) throttling fast publishers.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement backpressure buffering strategies like onBackpressureDrop or onBackpressureBuffer in Project Reactor?",
            "response": "Use `flux.onBackpressureDrop(droppedItem -> log(droppedItem))` to discard items when downstream demand is zero, or `onBackpressureBuffer(1000)` to set a bounded buffer.",
            "reason": "Asks how to use Project Reactor operators rather than explaining the core Reactive Streams specification mechanism.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Java implementation of a custom Subscriber that requests 10 items at a time upon completing processing.",
            "response": "```java\nsubscriber = new Subscriber<Data>() {\n    Subscription s;\n    public void onSubscribe(Subscription s) { this.s = s; s.request(10); }\n    public void onNext(Data d) { if (--remaining == 0) s.request(10); }\n};\n```",
            "reason": "Requests specific Java Subscriber implementation code rather than explaining the architectural principle.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Go channels implement flow control and backpressure using bounded channel buffers?",
            "response": "In Go, sending to a full buffered channel (`ch <- val`) blocks the sending goroutine until a receiving goroutine reads a value (`<-ch`), natively implementing backpressure without protocol overhead.",
            "reason": "Asks about Go channel synchronization rather than the Reactive Streams specification.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between TCP window flow control and application-level Reactive Streams backpressure?",
            "response": "TCP flow control operates at byte level in the OS kernel socket buffers using TCP receive windows, whereas Reactive Streams operates at the application object/record level inside user-space runtimes.",
            "reason": "Contrasts TCP socket window flow control with application-level streaming backpressure.",
        },
    },

    # 170. Tumbling vs Sliding Windows
    {
        "intent_group_id": "ig_data_tumbling_vs_sliding_windows",
        "domain": "data_processing",
        "subtopic": "Stream Processing & Event Streaming",
        "task_kind": "explanation",
        "dimensions": {"engine": {"status": "known", "value": "streaming"}},
        "canonical_query": "What is the difference between tumbling windows and sliding (hopping) windows in stream processing?",
        "canonical_response": "In stream processing (e.g. Flink, Spark Streaming, Kafka Streams), tumbling windows are fixed-size, contiguous, and non-overlapping time intervals (e.g. every 5 minutes: [0:00-0:05), [0:05-0:10)); each event belongs to exactly one window. Sliding (hopping) windows have a fixed duration but advance by a slide step smaller than the window duration (e.g. 5-minute window advancing every 1 minute); because the slide interval is smaller than the duration, windows overlap and each event falls into multiple concurrent windows.",
        "positive_paraphrase_1": {
            "query": "Compare tumbling windows and sliding windows in stream processing: how do window overlaps differ?",
            "reason": "Direct paraphrase comparing tumbling and sliding stream windows regarding overlap and assignment.",
        },
        "positive_paraphrase_2": {
            "query": "Why do events belong to multiple windows in sliding time windows but exactly one in tumbling windows?",
            "reason": "Syntactic variation focusing on multi-window event assignment in sliding vs tumbling windows.",
        },
        "hard_negative_diff_operation": {
            "query": "What are session windows in stream processing and how do session inactivity gap timeouts work?",
            "response": "Session windows group events by periods of activity separated by gaps of inactivity; when no events arrive for longer than the gap timeout, the current session window closes.",
            "reason": "Explains session windows rather than comparing tumbling vs sliding windows.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Apache Flink SQL query that computes average transaction amount over a 10-minute tumbling window.",
            "response": "```sql\nSELECT window_start, window_end, AVG(amount)\nFROM TABLE(TUMBLE(TABLE Transactions, DESCRIPTOR(ts), INTERVAL '10' MINUTE))\nGROUP BY window_start, window_end;\n```",
            "reason": "Requests specific Flink SQL query syntax rather than conceptual comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Redis implement a sliding window rate limiter using Sorted Sets (ZSET)?",
            "response": "Redis uses `ZADD` with timestamp as score, `ZREMRANGEBYSCORE` to prune elements outside the window, and `ZCARD` to count elements currently in the window.",
            "reason": "Asks about Redis ZSET rate limiting algorithms rather than stream processing window concepts.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens when the slide parameter equals the window duration in a sliding window definition?",
            "response": "When the slide interval equals the window duration (`slide == duration`), the sliding window degenerates into a non-overlapping tumbling window.",
            "reason": "Analyzes the mathematical edge case where sliding degenerates to tumbling rather than general comparison.",
        },
    },

    # 171. Event Time Watermarks
    {
        "intent_group_id": "ig_data_event_time_watermarks",
        "domain": "data_processing",
        "subtopic": "Stream Processing & Event Streaming",
        "task_kind": "explanation",
        "dimensions": {"engine": {"status": "known", "value": "flink"}},
        "canonical_query": "How do event time watermarks handle out-of-order and late-arriving events in stream processing engines?",
        "canonical_response": "Event time represents when an event occurred on the source device, which often differs from processing time (when the engine receives it) due to network jitter or device disconnection. A watermark $W(t)$ is a monotonically increasing metadata marker flowing through the stream asserting that the engine expects no more events with an event timestamp earlier than $t$. When the watermark reaches a window's end time, the engine triggers the window computation. Out-of-order events arriving before the watermark are seamlessly merged; events arriving with timestamps older than the watermark are classified as 'late data' and handled via side outputs, window accumulation, or dropped.",
        "positive_paraphrase_1": {
            "query": "Explain how watermarks enable stream processors to trigger event-time windows despite out-of-order arrivals.",
            "reason": "Direct paraphrase explaining watermarks, out-of-order event handling, and window triggering.",
        },
        "positive_paraphrase_2": {
            "query": "In Apache Flink, what is the role of watermarks in bounding event lateness and triggering aggregations?",
            "reason": "Syntactic variation focusing on bounding lateness and triggering computations via watermarks.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure allowedLateness and side outputs in Apache Flink for late data that arrives after the watermark?",
            "response": "In Flink, append `.allowedLateness(Time.minutes(5))` to update existing windows, or `.sideOutputLateData(outputTag)` to redirect late records to an alternate stream.",
            "reason": "Asks how to configure Flink APIs for late data rather than explaining watermark mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Java custom WatermarkGenerator in Flink that allows a maximum out-of-orderness of 5 seconds.",
            "response": "```java\npublic class BoundedOutOfOrdernessGenerator implements WatermarkGenerator<MyEvent> {\n    long maxTs = 0;\n    public void onEvent(MyEvent e, long ts, WatermarkOutput out) { maxTs = Math.max(maxTs, e.ts); }\n    public void onPeriodicEmit(WatermarkOutput out) { out.emitWatermark(new Watermark(maxTs - 5000)); }\n}\n```",
            "reason": "Requests specific Java WatermarkGenerator implementation rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Google Cloud Dataflow and Apache Beam define allowed lateness and triggers for streaming pipelines?",
            "response": "Apache Beam uses triggers (e.g. `AfterWatermark`, `Repeatedly`) to control when window panes are fired before, at, and after the watermark.",
            "reason": "Asks about Apache Beam trigger constructs rather than the fundamental concept of stream watermarks.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between event time, processing time, and ingestion time in streaming systems?",
            "response": "Event time is when the event was generated at the client; ingestion time is when Kafka or the message broker records it; processing time is the local clock time of the streaming node processing the record.",
            "reason": "Contrasts the three time semantics rather than explaining how watermarks handle out-of-order arrivals.",
        },
    },

    # 172. Stream Engine Checkpointing (Chandy-Lamport)
    {
        "intent_group_id": "ig_data_stream_checkpointing_chandy_lamport",
        "domain": "data_processing",
        "subtopic": "Stream Processing & Event Streaming",
        "task_kind": "explanation",
        "dimensions": {"algorithm": {"status": "known", "value": "chandy_lamport"}},
        "canonical_query": "How does the Chandy-Lamport distributed snapshot algorithm enable exactly-once stream processing without stopping execution?",
        "canonical_response": "In stream processing (e.g. Apache Flink's asynchronous barrier snapshotting), checkpoint coordinators inject special lightweight 'barrier' markers into the data streams at source operators. Barriers flow through the DAG alongside normal data records without halting the stream. When an operator receives barriers from all its incoming channels, it takes an asynchronous snapshot of its local state (writing to durable storage like S3/HDFS) and forwards the barrier downstream. Upon a worker failure, all operators roll back to their state from the last completed barrier snapshot and rewind Kafka source offsets, achieving exactly-once consistency.",
        "positive_paraphrase_1": {
            "query": "Explain how asynchronous stream barrier snapshotting based on Chandy-Lamport achieves consistent fault tolerance.",
            "reason": "Direct paraphrase explaining barrier snapshotting and fault recovery in stream engines.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do checkpoint barriers flowing through an operator DAG allow distributed state recovery without stopping data flow?",
            "reason": "Syntactic variation focusing on checkpoint barriers flowing with data to capture distributed state.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure unaligned checkpoints in Apache Flink to prevent checkpoint timeouts during severe backpressure?",
            "response": "Set `execution.checkpointing.unaligned.enabled: true` in `flink-conf.yaml`, which allows barriers to overtake in-flight channel buffers and snapshot channel data directly.",
            "reason": "Asks how to configure unaligned checkpoints for backpressure rather than explaining the standard barrier algorithm.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Provide the Flink configuration lines to set checkpointing intervals to 10 seconds with RocksDB state backend.",
            "response": "```java\nenv.enableCheckpointing(10000);\nenv.setStateBackend(new EmbeddedRocksDBStateBackend(true));\n```",
            "reason": "Requests specific Flink Java API code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Apache Spark Structured Streaming implement fault tolerance using write-ahead logs (WAL) and micro-batches?",
            "response": "Spark micro-batching records offsets into a write-ahead log in durable storage before executing a batch, replaying the batch upon executor failure.",
            "reason": "Asks about Spark micro-batch WAL recovery rather than continuous barrier snapshotting.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between at-least-once and exactly-once processing guarantees in distributed messaging?",
            "response": "At-least-once guarantees that no records are lost, but duplicates may be processed upon retry. Exactly-once guarantees that system state reflects each record's effect exactly once through deduplication or two-phase commit.",
            "reason": "Contrasts at-least-once vs exactly-once semantics rather than explaining the checkpoint snapshot algorithm.",
        },
    },

    # 173. Kafka Partition Key Hashing & Ordering
    {
        "intent_group_id": "ig_data_kafka_partitioning_ordering",
        "domain": "data_processing",
        "subtopic": "Stream Processing & Event Streaming",
        "task_kind": "explanation",
        "dimensions": {"broker": {"status": "known", "value": "kafka"}},
        "canonical_query": "How does Kafka guarantee strict message ordering within a partition, and why is global ordering not guaranteed across partitions?",
        "canonical_response": "Kafka guarantees strict total order only within a single partition: each partition is an append-only commit log where records receive sequential, monotonically increasing 64-bit offsets. When a producer sends a message with a record key, the default partitioner hashes the key (using murmur2) to determine the partition (`hash(key) % num_partitions`), guaranteeing that all events with the identical key land in the same partition in exact chronological order. However, across different partitions, there is no shared ordering mechanism or cross-partition offset clock, meaning global ordering across partitions cannot be guaranteed.",
        "positive_paraphrase_1": {
            "query": "Explain how Kafka achieves per-partition ordering using record keys and why cross-partition global order is not preserved.",
            "reason": "Direct paraphrase explaining partition hashing and ordering guarantees within vs across partitions.",
        },
        "positive_paraphrase_2": {
            "query": "Why does Kafka hash message keys to assign partitions, and what are the limitations on total message order across a topic?",
            "reason": "Syntactic variation focusing on message key hashing and the lack of total topic order.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure max.in.flight.requests.per.connection and enable.idempotence in Kafka producers to avoid out-of-order retries?",
            "response": "Set `enable.idempotence=true` and `max.in.flight.requests.per.connection=5` (or 1), ensuring the broker assigns sequence numbers and deduplicates out-of-order in-flight retries.",
            "reason": "Asks about producer idempotence and retry configuration rather than explaining partition ordering mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a custom Kafka Partitioner class in Java that routes premium customer IDs to dedicated partitions.",
            "response": "```java\npublic class VIPPartitioner implements Partitioner {\n    public int partition(String topic, Object key, byte[] keyBytes, Object val, byte[] valBytes, Cluster c) {\n        return key.toString().startsWith(\"VIP\") ? 0 : 1;\n    }\n}\n```",
            "reason": "Requests custom Java Partitioner code rather than conceptual explanation of ordering guarantees.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does AWS Kinesis shard partitioning and sequence numbers compare to Kafka partitions and offsets?",
            "response": "Kinesis shards use MD5 hash ranges (`HashKeyRange`) across an explicit 128-bit integer space, assigning string sequence numbers rather than sequential 64-bit integers.",
            "reason": "Compares AWS Kinesis shards with Kafka rather than explaining Kafka partition ordering.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens to message key partitioning when a Kafka topic's partition count is increased on a live cluster?",
            "response": "Because partition assignment uses `hash(key) % num_partitions`, increasing the partition count changes the modulus divisor, causing future messages for an existing key to route to a new partition.",
            "reason": "Focuses on repartitioning side effects on hashing rather than the fundamental ordering guarantees.",
        },
    },

    # 174. Dead Letter Queues (DLQ) & Retry Backoff
    {
        "intent_group_id": "ig_data_dead_letter_queues_retries",
        "domain": "data_processing",
        "subtopic": "Stream Processing & Event Streaming",
        "task_kind": "explanation",
        "dimensions": {"pattern": {"status": "known", "value": "dlq"}},
        "canonical_query": "What is the role of a Dead Letter Queue (DLQ) in event-driven systems and how do retry topics with backoff prevent poison pills?",
        "canonical_response": "In event processing, a 'poison pill' is a malformed or unprocessable message (e.g. schema incompatibility or logic bug) that repeatedly crashes consumer workers. Without handling, the consumer enters an infinite crash loop, blocking subsequent valid messages on the partition. A retry pattern routes failing messages to progressive retry topics (e.g. `orders-retry-10s`, `orders-retry-60s`) with exponential backoff. If max retries are exceeded, the message is routed to a Dead Letter Queue (DLQ), acknowledging it on the primary topic to unblock the partition while preserving the failed payload and stack trace for offline inspection and replay.",
        "positive_paraphrase_1": {
            "query": "Explain how retry topics and Dead Letter Queues prevent poison pill messages from stalling event consumers.",
            "reason": "Direct paraphrase explaining poison pills, exponential retry topics, and unblocking consumers via DLQs.",
        },
        "positive_paraphrase_2": {
            "query": "Why are unprocessable messages offloaded to a DLQ in streaming architectures instead of crashing the consumer loop?",
            "reason": "Syntactic variation focusing on unblocking partition consumption by offloading failed messages to DLQs.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure AWS SQS Dead-Letter Queues with redrive policies and maxReceiveCount in Terraform?",
            "response": "In Terraform `aws_sqs_queue`, configure `redrive_policy = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dlq.arn, maxReceiveCount = 5 })`.",
            "reason": "Asks for Terraform AWS SQS configuration syntax rather than explaining the architectural pattern.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python Kafka consumer script that catches processing exceptions and forwards failing records to a DLQ topic.",
            "response": "```python\nfor msg in consumer:\n    try:\n        process(msg.value)\n    except Exception as e:\n        producer.send('orders-dlq', key=msg.key, value=msg.value, headers=[('error', str(e).encode())])\n```",
            "reason": "Requests specific Python consumer implementation code rather than explaining architectural design.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does RabbitMQ implement Dead Letter Exchanges (x-dead-letter-exchange) using message TTLs?",
            "response": "RabbitMQ queues declare `x-dead-letter-exchange` and `x-message-ttl`; when a message expires or is rejected with `basic.nack(requeue=false)`, it is automatically republished to the dead letter exchange.",
            "reason": "Asks about RabbitMQ DLX headers rather than general event stream retry and DLQ architecture.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can routing messages to retry topics violate strict FIFO message processing order for that entity?",
            "reason": "Focuses on ordering violation trade-offs during retry topic routing rather than poison pill handling.",
            "response": "Moving a failed event for User A to a 60-second retry topic allows subsequent valid events for User A arriving on the main topic to be processed first, causing out-of-order state mutations.",
        },
    },

    # 175. Protobuf vs JSON Binary Encoding
    {
        "intent_group_id": "ig_data_protobuf_vs_json_encoding",
        "domain": "data_processing",
        "subtopic": "Serialization Formats & Schemas",
        "task_kind": "explanation",
        "dimensions": {"format": {"status": "known", "value": "protobuf"}},
        "canonical_query": "Why is Protocol Buffers (Protobuf) binary serialization significantly smaller and faster to parse than JSON?",
        "canonical_response": "JSON is a text-based, self-describing format: every message repeats field names as strings (`\"user_id\": 12345`), encodes numbers as variable-length ASCII digits, and requires lexical scanning and character escaping. Protobuf uses binary encoding governed by a pre-compiled schema: field names are replaced by compact 1-byte or 2-byte integer tags (`(field_number << 3) | wire_type`), integers use variable-length zig-zag varints, and values are packed without delimiter tokens or string parsing, reducing payload size by 60-80% and speeding up serialization/deserialization by 5x-10x.",
        "positive_paraphrase_1": {
            "query": "Explain how field tags and varint binary encoding make Protobuf more compact and faster to parse than text JSON.",
            "reason": "Direct paraphrase explaining field tags, varints, and binary packing advantages over JSON text.",
        },
        "positive_paraphrase_2": {
            "query": "What architectural differences between Protobuf binary format and JSON text strings explain the performance gains?",
            "reason": "Syntactic variation focusing on binary tags and varints vs text delimiters and string field names.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you compile .proto files into Python and Go source files using the protoc CLI compiler?",
            "response": "Run `protoc --python_out=. --go_out=. --go_opt=paths=source_relative schema.proto` to generate language-specific serialization bindings.",
            "reason": "Asks for protoc CLI compiler commands rather than comparing serialization formats and encoding mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a .proto schema defining a UserMessage with id (int64), email (string), and an enum Role.",
            "response": "```protobuf\nsyntax = \"proto3\";\nenum Role { USER = 0; ADMIN = 1; }\nmessage UserMessage {\n  int64 id = 1;\n  string email = 2;\n  Role role = 3;\n}\n```",
            "reason": "Requests specific .proto schema definition code rather than explaining binary efficiency differences.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does gRPC utilize HTTP/2 binary framing and Protobuf to outperform REST over HTTP/1.1?",
            "response": "gRPC combines Protobuf binary serialization with HTTP/2 binary framing, multiplexed streams on a single TCP connection, and HPACK header compression.",
            "reason": "Compares gRPC over HTTP/2 with REST rather than focusing specifically on Protobuf vs JSON encoding.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does JSON retain superior human readability and debugging convenience over Protobuf despite higher bandwidth cost?",
            "response": "JSON payloads can be directly inspected in browser dev tools, curl, and text editors without needing external `.proto` schema definitions or special decoding decoders.",
            "reason": "Focuses on human readability and developer tooling convenience rather than binary encoding efficiency.",
        },
    },

    # 176. Apache Avro Schema Evolution
    {
        "intent_group_id": "ig_data_avro_schema_evolution",
        "domain": "data_processing",
        "subtopic": "Serialization Formats & Schemas",
        "task_kind": "explanation",
        "dimensions": {"format": {"status": "known", "value": "avro"}},
        "canonical_query": "How does Apache Avro handle schema evolution and what are backward, forward, and full compatibility in a Schema Registry?",
        "canonical_response": "In Avro, serialized binary records do not contain field tags or field names: decoding relies on having access to both the 'writer schema' (schema used to write the record) and 'reader schema' (schema used to read it). In Confluent Schema Registry: 'Backward compatibility' means consumers using a new schema can read data written with older schemas (new fields must have default values); 'Forward compatibility' means old consumers can read data written with new schemas (deleted fields must have defaults); 'Full compatibility' satisfies both backward and forward rules, allowing independent consumer and producer upgrades.",
        "positive_paraphrase_1": {
            "query": "Explain Avro schema evolution and the definitions of BACKWARD, FORWARD, and FULL compatibility in Schema Registry.",
            "reason": "Direct paraphrase explaining Avro schema evolution and compatibility modes.",
        },
        "positive_paraphrase_2": {
            "query": "Why must new Avro fields specify default values to maintain backward compatibility in Kafka pipelines?",
            "reason": "Syntactic variation focusing on default values enabling backward and full compatibility.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you check schema compatibility via the Confluent Schema Registry REST API using curl?",
            "response": "Send `POST /compatibility/subjects/{subject}/versions/latest` with the proposed JSON schema in the request payload, which returns `{\"is_compatible\": true}`.",
            "reason": "Asks how to use the Schema Registry HTTP API rather than explaining the evolution concepts and compatibility modes.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Apache Avro JSON schema for an OrderEvent with an optional discountCode field that has a null default.",
            "response": "```json\n{\n  \"type\": \"record\",\n  \"name\": \"OrderEvent\",\n  \"fields\": [\n    {\"name\": \"id\", \"type\": \"string\"},\n    {\"name\": \"discountCode\", \"type\": [\"null\", \"string\"], \"default\": null}\n  ]\n}\n```",
            "reason": "Requests specific Avro JSON schema syntax rather than conceptual explanation of compatibility rules.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Protobuf achieve schema evolution using field tags and reserved field numbers?",
            "response": "Protobuf maps fields by immutable numeric tag IDs; fields can be renamed freely, but deprecated tags must be marked `reserved` to prevent reuse.",
            "reason": "Asks about Protobuf field tags and reserved numbers rather than Avro schema evolution and Schema Registry.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a field without a default value is deleted under BACKWARD compatibility versus FORWARD compatibility?",
            "response": "Deleting a field without a default is valid under BACKWARD compatibility (new reader doesn't need it), but breaks FORWARD compatibility (old reader expects it).",
            "reason": "Analyzes the exact asymmetric behavior of field deletion across backward vs forward compatibility.",
        },
    },

    # 177. MessagePack Binary Serialization
    {
        "intent_group_id": "ig_data_msgpack_serialization",
        "domain": "data_processing",
        "subtopic": "Serialization Formats & Schemas",
        "task_kind": "explanation",
        "dimensions": {"format": {"status": "known", "value": "msgpack"}},
        "canonical_query": "How does MessagePack achieve compact binary encoding while remaining schemaless like JSON?",
        "canonical_response": "MessagePack preserves JSON's dynamic, schemaless object model (maps, arrays, strings, numbers, booleans) without requiring a pre-compiled schema or IDL file. It replaces JSON ASCII delimiters (braces, quotes, colons) with compact 1-byte type prefix headers (format specifiers). Small integers (-32 to 127) are encoded directly in a single byte (`fixint`), short strings encode string length directly into the prefix byte (`fixstr`), and binary blobs (`bin` format) are supported natively without Base64 expansion overhead, typically reducing JSON payloads by 30-50%.",
        "positive_paraphrase_1": {
            "query": "Explain how MessagePack encodes JSON data types into compact binary format prefixes without schemas.",
            "reason": "Direct paraphrase explaining MessagePack's schemaless binary prefix encoding.",
        },
        "positive_paraphrase_2": {
            "query": "Why does MessagePack encode binary buffers without Base64 overhead while preserving dynamic dictionary keys?",
            "reason": "Syntactic variation focusing on native binary byte support and dynamic schema-free keys in MessagePack.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you serialize and deserialize Python dictionaries with msgpack-python using unpackb and packb?",
            "response": "Import `msgpack` and use `packed = msgpack.packb({'key': 'val'})` to encode, and `msgpack.unpackb(packed, raw=False)` to decode back to Python objects.",
            "reason": "Asks how to use the msgpack-python library API rather than explaining the internal binary format.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Node.js snippet using @msgpack/msgpack to encode an object and write it to a writable stream.",
            "response": "```javascript\nimport { encode } from '@msgpack/msgpack';\nconst buffer = encode({ id: 1, name: 'Alice' });\nstream.write(buffer);\n```",
            "reason": "Requests specific Node.js code rather than explaining format specifications.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does BSON (Binary JSON) in MongoDB differ from MessagePack regarding type support and indexing overhead?",
            "response": "BSON includes MongoDB-specific types (ObjectId, Regex, Date) and includes explicit document lengths to facilitate fast traversal, resulting in slightly larger payloads than MessagePack.",
            "reason": "Compares BSON with MessagePack rather than explaining MessagePack's binary format design.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Protobuf achieve higher compression ratios than MessagePack when transmitting repetitive records?",
            "response": "Protobuf replaces repeated dictionary string keys with compact numeric field tags via a pre-compiled schema, whereas MessagePack repeats the string keys in every individual record map.",
            "reason": "Contrasts MessagePack repeated string keys with Protobuf schema tags rather than explaining MessagePack's format.",
        },
    },

    # 178. FlatBuffers Zero-Copy Deserialization
    {
        "intent_group_id": "ig_data_flatbuffers_zero_copy",
        "domain": "data_processing",
        "subtopic": "Serialization Formats & Schemas",
        "task_kind": "explanation",
        "dimensions": {"format": {"status": "known", "value": "flatbuffers"}},
        "canonical_query": "How does FlatBuffers achieve zero-copy deserialization and direct memory-mapped access to structured data?",
        "canonical_response": "Unlike Protobuf or JSON which require allocating new objects and parsing byte streams into heap memory structures during deserialization, FlatBuffers lays out hierarchical data in a contiguous binary buffer using internal byte offsets and virtual tables (vtables). When an application receives a FlatBuffers byte array or memory-maps a file from disk, it does not parse or copy any data: accessor methods calculate memory offsets (`buffer_pointer + vtable_offset`) to read values directly from the buffer in place with zero allocation and zero deserialization latency.",
        "positive_paraphrase_1": {
            "query": "Explain how FlatBuffers vtables and internal byte offsets enable zero-copy reads directly from raw buffers.",
            "reason": "Direct paraphrase explaining FlatBuffers vtables, byte offsets, and zero-copy access.",
        },
        "positive_paraphrase_2": {
            "query": "Why does FlatBuffers require zero memory allocations when reading serialized data compared to Protobuf?",
            "reason": "Syntactic variation focusing on zero memory allocations and direct in-buffer access.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you compile FlatBuffers .fbs schemas using flatc to generate C++ and Rust header files?",
            "response": "Run `flatc --cpp --rust schema.fbs` to generate code bindings that contain table builders and accessors.",
            "reason": "Asks how to use the flatc compiler CLI rather than explaining the zero-copy buffer architecture.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a FlatBuffers .fbs schema defining a Monster table with pos (Vec3 struct), hp (short), and name (string).",
            "response": "```flatbuffers\nstruct Vec3 { x:float; y:float; z:float; }\ntable Monster { pos:Vec3; hp:short = 100; name:string; }\nroot_type Monster;\n```",
            "reason": "Requests specific FlatBuffers schema code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Cap'n Proto compare to FlatBuffers in achieving zero-copy pointer layout serialization?",
            "response": "Cap'n Proto also uses zero-copy pointer offsets, but aligns all words to 64-bit boundaries and integrates native RPC capabilities directly into the protocol.",
            "reason": "Compares Cap'n Proto with FlatBuffers rather than explaining FlatBuffers internal mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the trade-off of FlatBuffers zero-copy read performance regarding payload size and wire efficiency?",
            "response": "FlatBuffers payloads are typically larger than Protobuf because internal alignment padding, vtables, and 32-bit offset pointers take up more bytes than packed varints.",
            "reason": "Analyzes the wire size overhead trade-off rather than zero-copy memory access mechanics.",
        },
    },

    # 179. JSON Schema Validation Overhead
    {
        "intent_group_id": "ig_data_json_schema_validation_overhead",
        "domain": "data_processing",
        "subtopic": "Serialization Formats & Schemas",
        "task_kind": "explanation",
        "dimensions": {"format": {"status": "known", "value": "json_schema"}},
        "canonical_query": "Why does JSON Schema validation introduce significant CPU overhead in high-throughput microservices?",
        "canonical_response": "JSON Schema validation requires recursing through complex, dynamically-typed JSON abstract syntax trees (ASTs) or dictionary structures. For every incoming request, the validator performs extensive runtime type assertions, checks regex patterns (`pattern`), validates array uniqueness (`uniqueItems`), and resolves conditional branches (`oneOf`, `anyOf`, `allOf`). In interpreted languages (Python `jsonschema`, Node `ajv` without ahead-of-time compilation), this creates massive CPU overhead and memory allocations, often consuming 30-60% of total request handling time.",
        "positive_paraphrase_1": {
            "query": "Explain what causes heavy CPU consumption and latency overhead during JSON Schema validation in APIs.",
            "reason": "Direct paraphrase explaining the algorithmic and CPU causes of JSON Schema validation latency.",
        },
        "positive_paraphrase_2": {
            "query": "Why are operations like regex checks and oneOf/allOf evaluations expensive when validating JSON schemas at runtime?",
            "reason": "Syntactic variation focusing on regex and branch evaluation costs in JSON Schema validators.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you pre-compile JSON schemas ahead-of-time using Ajv in Node.js to maximize validation throughput?",
            "response": "Use `ajv.compile(schema)` once during service initialization and reuse the returned compiled validation function across all subsequent requests.",
            "reason": "Asks how to use Ajv pre-compilation APIs rather than explaining the fundamental CPU overhead causes.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Pydantic v2 BaseModel that validates email format, integer range, and string length.",
            "response": "```python\nfrom pydantic import BaseModel, EmailStr, Field\nclass User(BaseModel):\n    email: EmailStr\n    age: int = Field(ge=0, le=120)\n    name: str = Field(min_length=2, max_length=50)\n```",
            "reason": "Requests Pydantic model code rather than explaining JSON schema validation overhead.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Pydantic v2 achieve 5x-10x faster validation than v1 by implementing core validation in Rust (pydantic-core)?",
            "response": "Pydantic v2 delegates data parsing and schema validation to `pydantic-core`, written in Rust, avoiding Python bytecode interpretation overhead and GIL contention.",
            "reason": "Asks about Pydantic v2's Rust implementation rather than JSON Schema AST evaluation overhead.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does uniqueItems in JSON Schema require O(N^2) or hash set comparisons making it a denial-of-service vector?",
            "response": "To verify that all elements in an array are unique, especially arrays of deep nested objects, the validator must compute deep structural equality for every pair, enabling algorithmic ReDoS/DoS.",
            "reason": "Focuses on the specific algorithmic complexity of uniqueItems DoS rather than general validation overhead.",
        },
    },

    # 180. Apache Parquet Columnar Storage
    {
        "intent_group_id": "ig_data_parquet_columnar_storage",
        "domain": "data_processing",
        "subtopic": "Analytical Storage & Columnar Formats",
        "task_kind": "explanation",
        "dimensions": {"format": {"status": "known", "value": "parquet"}},
        "canonical_query": "How does Apache Parquet's columnar storage format and encoding schemes (RLE, dictionary) accelerate analytical queries?",
        "canonical_response": "Unlike row-based formats (CSV, JSON) that store full records sequentially, Parquet stores data column-by-column partitioned into Row Groups, Column Chunks, and Pages. For analytical queries (`SELECT AVG(age) FROM users`), the query engine only reads the specific byte ranges for the `age` column, skipping all other columns entirely (projection pushdown). Storing identical data types together enables high-efficiency compression and encoding algorithms: Dictionary Encoding replaces repeated strings with small integer IDs, and Run-Length Encoding (RLE) compresses consecutive identical values into `(value, count)` pairs.",
        "positive_paraphrase_1": {
            "query": "Explain how Parquet's column chunks, dictionary encoding, and RLE speed up analytical aggregation queries.",
            "reason": "Direct paraphrase explaining Parquet structure, encoding algorithms, and analytical query acceleration.",
        },
        "positive_paraphrase_2": {
            "query": "Why does column-oriented storage in Parquet drastically reduce disk I/O compared to row-oriented CSV or JSON?",
            "reason": "Syntactic variation focusing on projection pushdown and disk I/O reduction in Parquet.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you inspect Parquet file metadata, row group counts, and schema using the parquet-tools CLI?",
            "response": "Run `parquet-tools meta filename.parquet` to inspect row groups, compression codecs, and column chunk byte sizes.",
            "reason": "Asks how to use the parquet-tools CLI rather than explaining internal encoding and query execution benefits.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script using PyArrow to read a Parquet file selecting only two specific columns.",
            "response": "```python\nimport pyarrow.parquet as pq\ntable = pq.read_table('data.parquet', columns=['user_id', 'total_amount'])\n```",
            "reason": "Requests PyArrow code implementation rather than conceptual explanation of Parquet architecture.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Apache ORC columnar format compare to Apache Parquet in the Hadoop/Hive ecosystem?",
            "response": "ORC is heavily optimized for Hive with built-in Bloom filters and ACID support, while Parquet is optimized for broad cross-engine compatibility (Spark, Presto, DuckDB).",
            "reason": "Compares ORC with Parquet rather than explaining Parquet's internal encoding schemes.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What are the Dremel encoding definition and repetition levels that allow Parquet to encode nested lists and structs?",
            "response": "Definition levels track how many optional parent fields are defined (handling nulls), while repetition levels indicate at what recursive list depth an element repeats.",
            "reason": "Focuses on Dremel nested structure encoding rather than dictionary/RLE compression and projection pushdown.",
        },
    },

    # 181. Apache Arrow Columnar In-Memory IPC
    {
        "intent_group_id": "ig_data_arrow_columnar_ipc",
        "domain": "data_processing",
        "subtopic": "Analytical Storage & Columnar Formats",
        "task_kind": "explanation",
        "dimensions": {"framework": {"status": "known", "value": "arrow"}},
        "canonical_query": "How does Apache Arrow's in-memory columnar format eliminate serialization overhead across programming languages and processes?",
        "canonical_response": "Before Apache Arrow, every database and data library (Pandas, Spark, DuckDB, C++, Java) used proprietary in-memory representations, requiring expensive serialization/deserialization when passing data across processes or language boundaries (e.g. JVM to Python). Arrow defines a standardized, language-independent in-memory columnar specification that aligns memory buffers for modern CPU SIMD vectorization. Processes can share Arrow record batches via shared memory (`/dev/shm` or Plasma store) using zero-copy memory pointers, eliminating 80-90% of cross-boundary serialization overhead.",
        "positive_paraphrase_1": {
            "query": "Explain how Apache Arrow provides a standardized in-memory columnar layout enabling zero-copy cross-language IPC.",
            "reason": "Direct paraphrase explaining Arrow in-memory standards and zero-copy cross-language sharing.",
        },
        "positive_paraphrase_2": {
            "query": "Why does Apache Arrow eliminate serialization bottlenecks when sharing data frames between Python, C++, and Java?",
            "reason": "Syntactic variation focusing on eliminating serialization bottlenecks across languages via Arrow.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you stream Arrow record batches over the network using the Arrow Flight RPC framework?",
            "response": "Implement a `FlightServerBase` with `do_get()` streaming Arrow RecordBatches over HTTP/2 gRPC channels without serializing to row objects.",
            "reason": "Asks about Arrow Flight RPC framework implementation rather than the fundamental in-memory format.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a PyArrow snippet that exports a Table to C Data Interface pointers for zero-copy C++ consumption.",
            "response": "```python\nimport pyarrow as pa\nfrom pyarrow.cffi import ffi\nschema_ptr, array_ptr = ffi.new('struct ArrowSchema*'), ffi.new('struct ArrowArray*')\ntable._export_to_c(int(ffi.cast('uintptr_t', array_ptr)))\n```",
            "reason": "Requests specific CFFI implementation code rather than explaining Arrow memory architecture.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Polars utilize Apache Arrow memory buffers to outperform Pandas in data frame operations?",
            "response": "Polars is built in Rust natively on Apache Arrow memory buffers, enabling multi-threaded query execution and SIMD operations without Python GIL locks.",
            "reason": "Asks about Polars vs Pandas query execution rather than Apache Arrow's general IPC standard.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between Apache Parquet (storage format) and Apache Arrow (in-memory format)?",
            "response": "Parquet is an on-disk columnar format optimized for maximum disk compression and metadata pruning; Arrow is an in-memory format optimized for CPU SIMD execution and zero-copy IPC.",
            "reason": "Contrasts Parquet on disk with Arrow in memory rather than explaining Arrow's cross-language IPC mechanism.",
        },
    },

    # 182. Delta Lake & Iceberg ACID Transactions
    {
        "intent_group_id": "ig_data_lakehouse_acid_transactions",
        "domain": "data_processing",
        "subtopic": "Analytical Storage & Columnar Formats",
        "task_kind": "explanation",
        "dimensions": {"storage": {"status": "known", "value": "lakehouse"}},
        "canonical_query": "How do modern lakehouse formats (Delta Lake, Apache Iceberg) provide ACID transactions on top of object storage?",
        "canonical_response": "Object stores (S3, GCS) lack native transactional primitives and POSIX atomic renames. Lakehouse table formats solve this by maintaining an atomic transaction log (Delta Log JSON files with atomic checkpoints, or Iceberg metadata trees and snapshot manifests). Every write operation (append, delete, update) generates immutable Parquet data files and commits a new atomic snapshot to the transaction log using optimistic concurrency control (OCC). Readers always query a frozen, consistent snapshot of the table without seeing dirty or uncommitted files, enabling serializable or snapshot isolation on cloud storage.",
        "positive_paraphrase_1": {
            "query": "Explain how transaction logs and immutable snapshots enable ACID guarantees in Delta Lake and Apache Iceberg.",
            "reason": "Direct paraphrase explaining transaction logs, snapshot trees, and ACID guarantees on object stores.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does optimistic concurrency control in lakehouse table formats prevent dirty reads on S3?",
            "reason": "Syntactic variation focusing on optimistic concurrency control preventing dirty reads on cloud object stores.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you run table maintenance with VACUUM and OPTIMIZE (Z-Order) on Delta Lake tables?",
            "response": "Run `OPTIMIZE table ZORDER BY (col1)` to co-locate related data into larger Parquet files, and `VACUUM table RETAIN 168 HOURS` to delete unreferenced historical files.",
            "reason": "Asks how to execute maintenance commands (OPTIMIZE/VACUUM) rather than explaining transactional architecture.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a PySpark script that performs a MERGE INTO (upsert) operation on a Delta Lake table.",
            "response": "```python\ndeltaTable.alias('t').merge(updates.alias('u'), 't.id = u.id') \\\n    .whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()\n```",
            "reason": "Requests specific PySpark merge API code rather than explaining how ACID transactions work.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Apache Hudi's Copy-on-Write (COW) compare to Merge-on-Read (MOR) table types?",
            "response": "COW rewrites entire Parquet files upon every update; MOR writes updates to compact delta log files (Avro) that are merged with base Parquet files at read time.",
            "reason": "Compares COW vs MOR in Apache Hudi rather than general ACID transaction log architecture.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens when two concurrent transactions attempt to write to the same Delta Lake partition simultaneously?",
            "response": "Under Optimistic Concurrency Control, the first transaction commits successfully, while the second detects a conflict during log commit and either replays automatically or fails.",
            "reason": "Focuses on write conflict resolution during concurrent OCC commits rather than general ACID snapshot design.",
        },
    },

    # 183. Row vs Columnar Storage (OLTP vs OLAP)
    {
        "intent_group_id": "ig_data_row_vs_columnar_storage",
        "domain": "data_processing",
        "subtopic": "Analytical Storage & Columnar Formats",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "oltp_vs_olap"}},
        "canonical_query": "What are the fundamental architectural trade-offs between row-oriented (OLTP) and column-oriented (OLAP) database storage engines?",
        "canonical_response": "Row-oriented storage (PostgreSQL, MySQL) stores entire tuples contiguously on disk pages. This optimizes Online Transaction Processing (OLTP): single-row inserts, updates, and primary key lookups (`SELECT * WHERE id = ?`) require reading or writing exactly one disk block with minimal I/O. Column-oriented storage (ClickHouse, Snowflake, DuckDB) stores each column's values contiguously across records. This optimizes Online Analytical Processing (OLAP): wide aggregations (`SELECT AVG(revenue) FROM sales`) read only the bytes for the referenced columns, skipping unreferenced data entirely and achieving 10x-100x speedups via SIMD vectorization and compression.",
        "positive_paraphrase_1": {
            "query": "Compare row-based and column-based database storage: why is row better for OLTP and column better for OLAP?",
            "reason": "Direct paraphrase comparing row-oriented OLTP with column-oriented OLAP storage trade-offs.",
        },
        "positive_paraphrase_2": {
            "query": "Why do analytical queries run much faster on columnar databases while transactional mutations favor row stores?",
            "reason": "Syntactic variation focusing on analytical read speedups vs transactional write efficiency.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure ClickHouse MergeTree primary keys and order-by expressions to optimize range scans?",
            "response": "Define `ENGINE = MergeTree() ORDER BY (tenant_id, created_at)`, creating a sparse index that skips non-matching 8192-row granules during queries.",
            "reason": "Asks how to configure ClickHouse MergeTree index keys rather than comparing row vs columnar storage architectures.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a DuckDB SQL query that queries a remote Parquet file directly using the read_parquet function.",
            "response": "```sql\nSELECT region, SUM(sales) FROM read_parquet('s3://bucket/sales/*.parquet') GROUP BY region;\n```",
            "reason": "Requests specific DuckDB SQL syntax rather than comparing OLTP and OLAP storage paradigms.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Hybrid Transactional/Analytical Processing (HTAP) databases like TiDB store data in both row and columnar formats?",
            "response": "TiDB maintains row-based TiKV for OLTP transactions and asynchronously replicates Raft logs to columnar TiFlash for OLAP analytics.",
            "reason": "Asks about HTAP dual-engine architecture rather than pure row vs columnar trade-offs.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why do wide updates and multi-column inserts become expensive and slow in columnar storage formats?",
            "response": "Inserting a single 100-column row into a columnar store requires opening, appending to, and updating 100 separate column files and compression buffers instead of a single page append.",
            "reason": "Focuses on write amplification of single-row inserts in columnar stores rather than general OLTP vs OLAP trade-offs.",
        },
    },

    # 184. Partition Pruning & Predicate Pushdown
    {
        "intent_group_id": "ig_data_partition_pruning_predicate_pushdown",
        "domain": "data_processing",
        "subtopic": "Distributed Query Execution",
        "task_kind": "explanation",
        "dimensions": {"engine": {"status": "known", "value": "query_optimizer"}},
        "canonical_query": "How do partition pruning and predicate pushdown minimize disk and network I/O in distributed query engines?",
        "canonical_response": "Partition pruning evaluates query filter conditions (e.g. `WHERE date = '2026-03-01'`) against directory structures or partition catalog metadata, allowing the engine to skip scanning entire directories of files that cannot contain matching rows. Predicate pushdown pushes filter expressions (e.g. `WHERE amount > 1000`) down into the storage format reader (e.g. Parquet reader): the reader inspects min/max column statistics stored in file footers and row group headers, discarding entire row groups and pages before decompressing or transferring bytes over the network.",
        "positive_paraphrase_1": {
            "query": "Explain how query engines use partition pruning and file footer min/max statistics for predicate pushdown.",
            "reason": "Direct paraphrase explaining directory partition skipping and row group min/max predicate pushdown.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do partition pruning and storage-layer predicate evaluation prevent unnecessary data reads?",
            "reason": "Syntactic variation focusing on avoiding data reads via partition skipping and storage-layer filters.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you verify whether a Spark SQL query is applying partition filters and pushed filters using EXPLAIN?",
            "response": "Run `EXPLAIN EXTENDED SELECT ...` and look for `PartitionFilters: [...]` and `PushedFilters: [...]` in the `FileScan` operator.",
            "reason": "Asks how to inspect Spark physical plans with EXPLAIN rather than explaining the architectural mechanisms.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Hive DDL statement that creates an external table partitioned by year and month.",
            "response": "```sql\nCREATE EXTERNAL TABLE sales (id INT, amount DOUBLE)\nPARTITIONED BY (year INT, month INT)\nSTORED AS PARQUET LOCATION 's3://bucket/sales/';\n```",
            "reason": "Requests Hive table DDL code rather than explaining how query optimizers execute pruning.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does PostgreSQL implement partition pruning at query planning and execution time using declarative partitioning?",
            "response": "Postgres evaluates constraint boundaries during planning and runtime using `enable_partition_pruning = on` to exclude sub-tables from the query append node.",
            "reason": "Asks specifically about PostgreSQL declarative partitioning rather than distributed query engine mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does wrapping a filtered column in a function (e.g. WHERE UPPER(city) = 'PARIS') prevent predicate pushdown?",
            "response": "Storage min/max statistics only record raw values; evaluating a function requires reading and transforming every value, preventing the reader from using min/max bounds to skip data.",
            "reason": "Focuses on expression wrapping preventing index/statistic usage rather than general pruning principles.",
        },
    },

    # 185. Idempotent ETL & Deduplication
    {
        "intent_group_id": "ig_data_idempotent_etl_deduplication",
        "domain": "data_processing",
        "subtopic": "Distributed Query Execution",
        "task_kind": "explanation",
        "dimensions": {"pipeline": {"status": "known", "value": "etl"}},
        "canonical_query": "Why is idempotency essential in ETL data pipelines and how is it implemented using UPSERT and partition overwrite strategies?",
        "canonical_response": "In distributed ETL pipelines, transient failures (network drops, worker preemption) trigger automated retries. An idempotent pipeline guarantees that executing the same job multiple times produces the exact same end state without duplicate records or corrupted aggregations. For batch pipelines, it is implemented using atomic partition replacement (`INSERT OVERWRITE PARTITION (date = ...)`), which completely replaces the partition atomically. For streaming or record-level pipelines, it is implemented using deterministic natural keys or deduplication hashes with `MERGE INTO` (UPSERT) or unique constraint deduplication tables.",
        "positive_paraphrase_1": {
            "query": "Explain how atomic partition overwrite and UPSERT operations ensure idempotency across retried ETL jobs.",
            "reason": "Direct paraphrase explaining idempotent batch and streaming ETL patterns and retry safety.",
        },
        "positive_paraphrase_2": {
            "query": "Why do reliable data pipelines require idempotent writes to handle worker failures and replay safely?",
            "reason": "Syntactic variation focusing on worker failures and replay safety via idempotent writes.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement row deduplication in dbt using dbt-utils dedup macros and window functions?",
            "response": "Use `{{ dbt_utils.deduplicate(relation=ref('stg_orders'), partition_by='order_id', order_by='updated_at desc') }}` to pick the latest record.",
            "reason": "Asks how to use dbt deduplication macros rather than explaining ETL pipeline idempotency principles.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a SQL query using ROW_NUMBER() OVER (PARTITION BY id ORDER BY timestamp DESC) to deduplicate records.",
            "response": "```sql\nWITH ranked AS (\n  SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) as rn FROM raw_events\n)\nSELECT * FROM ranked WHERE rn = 1;\n```",
            "reason": "Requests specific SQL deduplication query code rather than explaining idempotent pipeline architecture.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Apache Airflow ensure task idempotency using execution_date and templated partition paths?",
            "response": "Airflow passes `{{ ds }}` as parameter to write targets (`s3://data/date={{ ds }}/`), ensuring re-running a dag run for a specific date overwrites that date's partition.",
            "reason": "Asks about Airflow parameter templating rather than general ETL idempotency mechanisms.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between at-least-once pipeline ingestion and effectively-once pipeline publishing?",
            "response": "At-least-once ingestion accepts duplicates into intermediate queues; effectively-once publishing achieves exact consistency by deduplicating records at the sink before user consumption.",
            "reason": "Contrasts ingestion guarantees with sink publishing rather than explaining idempotent ETL write strategies.",
        },
    },

    # 186. Bloom Filters in Data Systems
    {
        "intent_group_id": "ig_data_bloom_filters_query_engines",
        "domain": "data_processing",
        "subtopic": "Distributed Query Execution",
        "task_kind": "explanation",
        "dimensions": {"algorithm": {"status": "known", "value": "bloom_filter"}},
        "canonical_query": "How do Bloom filters accelerate point lookups in LSM-tree databases and join operations in distributed query engines?",
        "canonical_response": "A Bloom filter is a space-efficient probabilistic data structure that tests set membership with zero false negatives (if it says 'not present', the item is definitely absent; if it says 'present', it may be a false positive). In LSM-tree databases (RocksDB, Cassandra), each SSTable has an in-memory Bloom filter: if the filter returns negative, the engine avoids slow disk reads to that SSTable entirely. In distributed query engines (Spark, Presto), a Bloom filter built on the small dimension table during a join is pushed down to scan nodes on the large fact table to prune non-matching records before the expensive network shuffle.",
        "positive_paraphrase_1": {
            "query": "Explain how Bloom filters prevent unnecessary disk reads in LSM trees and reduce shuffle traffic in distributed joins.",
            "reason": "Direct paraphrase explaining Bloom filters in LSM tables and join shuffle pruning.",
        },
        "positive_paraphrase_2": {
            "query": "Why are zero false negatives crucial for Bloom filters when skipping SSTable disk scans or filtering join keys?",
            "reason": "Syntactic variation focusing on the zero false negative guarantee enabling safe disk and join skipping.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you calculate the optimal bit array size (m) and number of hash functions (k) for a target false positive rate in a Bloom filter?",
            "response": "Use $m = -\\frac{n \\ln p}{(\\ln 2)^2}$ for bit array size and $k = \\frac{m}{n} \\ln 2$ for hash functions, where $n$ is element count and $p$ is false positive probability.",
            "reason": "Asks for mathematical formulas to size Bloom filters rather than explaining system architecture applications.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python implementation of a Bloom filter using bitarray and mmh3 hash functions.",
            "response": "```python\nimport mmh3, bitarray\nclass BloomFilter:\n    def __init__(self, size=1000, k=3):\n        self.bits = bitarray.bitarray(size); self.bits.setall(0); self.k = k\n```",
            "reason": "Requests Python implementation code rather than explaining LSM and query engine optimizations.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How do Cuckoo filters differ from Bloom filters regarding support for item deletion and lookup speed?",
            "response": "Cuckoo filters use cuckoo hashing to store compact fingerprints in buckets, allowing items to be deleted dynamically, which standard Bloom filters cannot do.",
            "reason": "Compares Cuckoo filters with Bloom filters rather than explaining Bloom filter usage in data systems.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens to the false positive rate of a Bloom filter when the number of inserted elements exceeds capacity?",
            "response": "As inserted elements exceed the design capacity, the bit array becomes saturated with 1s, causing the false positive rate to degrade toward 100% until every lookup reports 'present'.",
            "reason": "Focuses on saturation behavior and false positive degradation rather than systems integration.",
        },
    },

    # 187. Distributed Shuffle Bottlenecks
    {
        "intent_group_id": "ig_data_distributed_shuffle_bottlenecks",
        "domain": "data_processing",
        "subtopic": "Distributed Query Execution",
        "task_kind": "explanation",
        "dimensions": {"engine": {"status": "known", "value": "spark"}},
        "canonical_query": "Why is the distributed shuffle phase the most common scalability and performance bottleneck in MapReduce and Apache Spark?",
        "canonical_response": "The shuffle phase occurs during wide transformations (`groupByKey`, `reduceByKey`, wide joins) where records must be redistributed across executors according to key hashes. Every mapper writes partitioned shuffle files to local disk and serves them to all reducers over the network. This induces an all-to-all communication pattern ($M \\times R$ connections) causing disk I/O bottlenecks, network bandwidth saturation, connection timeouts, and memory pressure. Severe data skew (where a single key has millions of records) concentrates immense work on a single reducer, causing stragglers and out-of-memory executor failures.",
        "positive_paraphrase_1": {
            "query": "Explain what causes disk spills, network saturation, and stragglers during the shuffle phase in Apache Spark.",
            "reason": "Direct paraphrase explaining shuffle mechanics, disk spills, and skew stragglers.",
        },
        "positive_paraphrase_2": {
            "query": "Why does the all-to-all communication pattern of distributed shuffles degrade cluster throughput at scale?",
            "reason": "Syntactic variation focusing on all-to-all network transfers and disk I/O bottlenecks in shuffles.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure spark.sql.shuffle.partitions and enable Adaptive Query Execution (AQE) to mitigate shuffle bottlenecks?",
            "response": "Enable AQE with `spark.sql.adaptive.enabled=true` and `spark.sql.adaptive.coalescePartitions.enabled=true` to automatically merge small post-shuffle partitions.",
            "reason": "Asks how to tune Spark AQE configuration parameters rather than explaining the architectural bottleneck causes.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Show how to use salting (adding random prefixes to keys) in PySpark to fix a skewed shuffle join.",
            "response": "```python\ndf = df.withColumn('salted_key', concat(col('key'), lit('_'), (rand() * 10).cast('int')))\n```",
            "reason": "Requests specific PySpark code for salting rather than explaining shuffle mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Ray's distributed object store handle data sharing between tasks compared to Spark's shuffle files?",
            "response": "Ray uses Plasma, an in-memory shared-memory object store based on Apache Arrow, allowing zero-copy reads between tasks on the same node.",
            "reason": "Asks about Ray Plasma object store rather than MapReduce and Spark shuffle bottlenecks.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does reduceByKey perform map-side combine while groupByKey does not, reducing shuffle volume?",
            "response": "`reduceByKey` merges values locally on each mapper before shuffling, reducing network bytes by orders of magnitude; `groupByKey` sends every unaggregated record over the network.",
            "reason": "Focuses on map-side combine optimization rather than general shuffle architecture and bottlenecks.",
        },
    },

    # 188. Broadcast Hash Joins vs Shuffle Hash Joins
    {
        "intent_group_id": "ig_data_broadcast_vs_shuffle_hash_join",
        "domain": "data_processing",
        "subtopic": "Distributed Query Execution",
        "task_kind": "explanation",
        "dimensions": {"engine": {"status": "known", "value": "spark"}},
        "canonical_query": "How does a Broadcast Hash Join (Map-Side Join) work in Apache Spark and when should it be used instead of a Shuffle Hash Join?",
        "canonical_response": "In a standard Shuffle Hash Join (or Sort-Merge Join), both large datasets are repartitioned and shuffled across the network based on join keys, incurring heavy disk I/O and network transfer. In a Broadcast Hash Join, the driver node collects the entire smaller table (dimension table), serializes it, and broadcasts a copy to every worker executor in the cluster. Each executor builds an in-memory hash table of the broadcasted dataset and streams its local partition of the large table through it. It eliminates the shuffle phase completely and is optimal when one table is small enough to fit comfortably in executor memory (typically <10-50MB, controlled by `spark.sql.autoBroadcastJoinThreshold`).",
        "positive_paraphrase_1": {
            "query": "Explain the mechanics of Broadcast Hash Joins in Spark and why they eliminate network shuffles for small tables.",
            "reason": "Direct paraphrase explaining broadcast join mechanics and shuffle elimination.",
        },
        "positive_paraphrase_2": {
            "query": "When is a map-side broadcast join preferred over a shuffle join and how does broadcast table size limit its use?",
            "reason": "Syntactic variation focusing on small table size limits and avoiding shuffle joins via broadcasting.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you explicitly hint a broadcast join in PySpark or Spark SQL using the broadcast function?",
            "response": "In PySpark, use `df_large.join(broadcast(df_small), 'key')`, or in SQL use `SELECT /*+ BROADCAST(small_table) */ * FROM ...`.",
            "reason": "Asks for broadcast hint syntax in PySpark/SQL rather than explaining the operational mechanisms and trade-offs.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a PySpark script that joins two DataFrames and verifies via explain() that BroadcastHashJoin was selected.",
            "response": "```python\ndf = df1.join(broadcast(df2), 'id')\ndf.explain()\n```",
            "reason": "Requests specific Python code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Presto/Trino perform broadcast joins and distributed hash joins across coordinator and worker nodes?",
            "response": "In Trino, `broadcast` replicate the entire build table to all workers; `distributed` partitions both build and probe tables by hash among workers.",
            "reason": "Asks about Trino join distribution modes rather than Apache Spark join mechanics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What catastrophic failure occurs if a user forces a broadcast join on a table that exceeds driver or executor memory?",
            "response": "The driver node runs out of memory collecting the dataset, or executors crash with `java.lang.OutOfMemoryError: Java heap space` when building the hash table.",
            "reason": "Focuses on OOM crash failure mode when broadcasting oversized tables rather than general join mechanics.",
        },
    },

    # 189. Data Lineage and Provenance
    {
        "intent_group_id": "ig_data_lineage_and_provenance",
        "domain": "data_processing",
        "subtopic": "Distributed Query Execution",
        "task_kind": "explanation",
        "dimensions": {"governance": {"status": "known", "value": "data_lineage"}},
        "canonical_query": "What is data lineage in modern data architectures and how does OpenLineage capture dataset transformation graphs?",
        "canonical_response": "Data lineage tracks the end-to-end lifecycle, movement, and transformations of data from raw sources through intermediate ETL pipelines to downstream dashboards and ML models. It provides observability for root-cause impact analysis (e.g. which reports break if an upstream column changes) and regulatory compliance (GDPR, BCBS 239). OpenLineage is an open standard that captures lineage via an event-based specification: pipeline tools (Spark, Airflow, dbt) emit JSON RunEvents containing Job, Dataset (inputs and outputs), and facets (schema, column-level lineage, assertions) to a central catalog (like Marquez or DataHub).",
        "positive_paraphrase_1": {
            "query": "Explain how data lineage and standards like OpenLineage track data provenance and transformations across pipelines.",
            "reason": "Direct paraphrase explaining data lineage, provenance tracking, and the OpenLineage standard.",
        },
        "positive_paraphrase_2": {
            "query": "Why is column-level data lineage critical for pipeline impact analysis and how do tools capture input/output metadata?",
            "reason": "Syntactic variation focusing on column-level lineage and capturing dataset metadata.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure the OpenLineage Spark listener in spark-defaults.conf to send lineage to a Marquez backend?",
            "response": "Add `spark.extraListeners = io.openlineage.spark.agent.OpenLineageSparkListener` and set `spark.openlineage.transport.url = http://marquez:5000`.",
            "reason": "Asks for specific Spark configuration parameters rather than explaining data lineage principles and standards.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an OpenLineage JSON RunEvent payload representing a completed Spark job reading from Table A and writing to Table B.",
            "response": "```json\n{\n  \"eventType\": \"COMPLETE\",\n  \"job\": {\"namespace\": \"prod\", \"name\": \"etl_orders\"},\n  \"inputs\": [{\"namespace\": \"s3\", \"name\": \"raw_orders\"}],\n  \"outputs\": [{\"namespace\": \"dw\", \"name\": \"fact_orders\"}]\n}\n```",
            "reason": "Requests specific JSON event schema syntax rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does dbt generate documentation and interactive DAG lineage graphs using manifest.json?",
            "response": "dbt parses SQL `ref()` and `source()` macros into a dependency DAG, compiling relationships into `target/manifest.json` rendered by `dbt docs generate`.",
            "reason": "Asks specifically about dbt DAG generation rather than OpenLineage and general data provenance.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between table-level lineage and column-level lineage in data catalogs?",
            "response": "Table-level lineage tracks dependencies between coarse datasets, while column-level lineage traces the exact mathematical transformations mapping each source column to final target columns.",
            "reason": "Contrasts table-level vs column-level lineage granularity rather than explaining overall lineage architecture.",
        },
    },
]
