# Song feature fixture provenance

- `reccobeats.json`: real response captured during planning from
  `GET https://api.reccobeats.com/v1/audio-features?ids=00aqkszH1FdUiJJWvX6iEl`.

Tests mutate copies of this payload to exercise missing fields, partial batches and API errors.
