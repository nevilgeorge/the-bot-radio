# ReccoBeats fixtures

`audio_features.json` copies the captured provider response from
`tests/fixtures/song_features/reccobeats.json` (see its provenance README).

`recommendation.json` is a real response captured 2026-10-29 from read-only
`GET /v1/track/recommendation?seeds=00aqkszH1FdUiJJWvX6iEl&size=1`.
The bulky `availableCountries` field was removed; other returned metadata is unchanged.

The live API rejected explicit `negativeSeeds=` on that date with status 400 and
`getRecommendation.negativeSeeds: size must be between 1 and 5`. The service now uses
an empty list, which httpx omits from the query, matching the successful capture request.
No negative seeds are exposed in the application interface.

Official parameter schema: https://reccobeats.com/docs/apis/get-recommendation
Seven normalized targets are 0–1; key -1–11, mode 0–1, loudness -60–2,
tempo 0–250, popularity 0–100, and featureWeight is a float 1–5.
Malformed-response tests deliberately mutate these baseline envelopes.
