# day_of_week — "Day of week"

- **Source:** none. Derived purely from `ctx.run_date` (the America/New_York date of the run), never `ctx.now`, so a run near midnight UTC still gets the New York weekday.
- **Auth / rate limits / cadence:** n/a. No HTTP, so no fixtures or live test.
- **value:** full weekday name, e.g. `"Monday"`. A holiday never replaces it.
- **data:** `weekday` ("Monday"), `iso_weekday` (1=Mon … 7=Sun), `is_weekend` (Sat/Sun), `date` (ISO), `holiday` (observed US federal holiday name, or `null`).
- **raw:** empty.
- **Status:** always `ok`; this input can't go stale or fail.

## Holiday rules
Hard-coded in the module (no dependency):
- Fixed dates (New Year's, Juneteenth, Independence Day, Veterans Day, Christmas) use the federal observed rule: Saturday moves to Friday, Sunday moves to Monday. So July 4 2026 (a Saturday) is flagged on Fri July 3, and Jan 1 2028 (a Saturday) is flagged on Fri Dec 31 2027.
- Nth-weekday: MLK (3rd Mon Jan), Washington's Birthday (3rd Mon Feb), Memorial (last Mon May), Labor (1st Mon Sep), Columbus (2nd Mon Oct), Thanksgiving (4th Thu Nov).
- Left out: Inauguration Day, which only applies around DC every four years. If Congress adds a holiday, update `_FIXED` / `_NTH`.
