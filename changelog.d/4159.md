Live monitoring now reads the active run's stream buffer and displays normalized
text chunks. A new stream clears a stale buffer, and chunks that cannot be
serialized retain their text with a serialization error marker. For objects
that define both methods, live-buffer serialization uses `to_dict` before
`model_dump`.

Normalized records reserve `_lionagi_stream_chunk: true`; a pre-existing legacy
record with that exact key and value is interpreted as normalized. Other legacy
records retain their previous text rendering, including text on non-text types.
The reader still examines the last five physical lines and retains its raw-line
fallback for malformed JSON.
