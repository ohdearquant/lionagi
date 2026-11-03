Live monitoring now reads the active run's stream buffer and displays normalized
text chunks. A new stream clears a stale buffer, and chunks that cannot be
serialized retain their text with a serialization error marker. For objects
that define both methods, live-buffer serialization uses `to_dict` before
`model_dump`.
