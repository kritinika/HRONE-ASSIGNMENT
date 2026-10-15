# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace check used floored minutes (`> 10`), so 09:40:01 gave 0 instead of 10. Used the punch-in's own (UTC) time, not IST; no overnight rule; sub-seconds not dropped. | Shift 09:30, punch 09:40:01 -> expected 10, got 0. Punch 15:05 UTC for a 10:35 IST punch gives nonsense. | Rewrote as part of `compute_derived`: seconds-based comparison `> 600`, floor of minutes, measured from IST shift start on the attendance date. |
| 2 | `compute_work_hours` | `round()` on a float is banker's / float rounding, not half-up. | 16218 s = 4.505 h must be 4.51. | `Decimal(seconds)/3600` quantized with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | No 30-minute minimum; naive date mixed with an aware time (TypeError); overnight shift end not moved to the next day. | Punch-out 19:00 on an 18:30 shift gave 30, 18:40 gave 10 (should be 0). | Same function: floor minutes, count only if >= 30, shift end +1 day when overnight. |
| 4 | `EmployeeIn` | No validation at all: emp_code format, email, `HH:MM`, `joined_on` real date, lengths, `shift_start != shift_end`. | POST `{"emp_code": "x", "joined_on": "nope"}` returns 201. | Pydantic field patterns/limits and a model validator; `joined_on` is a `date`. |
| 5 | `create_employee` | Check-then-insert race; `created_at` naive local time returned as a datetime, not epoch ms. | Parallel creates both return 201 (or one 500). | Unique index on `emp_code`, catch `DuplicateKeyError` -> 409; UTC time, returned as epoch ms. |
| 6 | `list_employees` | `skip = page*page_size` skips page 1; `total` ignores the filter; no sort; no page bounds. | `page=1` returns the second page; `total` wrong with `department`. | `(page-1)*page_size`, filtered count, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 7 | `punch_in` | Unknown employee -> `None["shift_start"]` 500. `fromtimestamp` uses server-local time zone, so the date is not the IST date; no overnight rule; check-then-insert race; ABSENT/LEAVE accepted; returns `id` and a raw datetime; `punched_at` not range-checked. | Unknown emp_code; server in UTC at 02:00 IST; two parallel requests; status `ABSENT`. | 404 check; `from_ms` (UTC, whole seconds); `attendance_date` (IST + overnight); unique `(emp_code,date)` index + `DuplicateKeyError` -> 409; Literal status; bounded `StrictInt` timestamp; epoch-ms response. |
| 8 | `list_attendance` | Loads the whole collection and sorts in Python, only by date; leaks `id`/datetimes; `date_from > date_to` not rejected; no bounds on paging; no index. | ~100k records: slow, memory heavy; ties on date come back in arbitrary order. | Mongo `find` + `sort(date desc, emp_code asc)` + skip/limit, index `(date, emp_code)`, `count_documents`, 422 on bad range, `serialize_attendance`. |
| 9 | `/health` | Always returns ok, even when MongoDB is down. | Stop mongod and call it. | Pings MongoDB; 503 on failure. |
| 10 | Client setup | Naive datetimes (no `tz_aware`); no indexes created at startup; no server selection timeout (requests hang when DB is down); missing `history`/`half_day` not defaulted. | Subtracting a naive and an aware datetime raises; legacy docs crash serialization. | `tz_aware=True`, `ensure_indexes()` in lifespan, 5 s timeout, defaults in the serializer. |

## Looked at and fine
- `load_dotenv()` does not override real environment variables, which is what the contract wants.
- Plain `def` routes with PyMongo run in FastAPI's thread pool; PyMongo's client is thread-safe, so this is fine.
- Removing `_id` from responses (`doc.pop("_id")`) was right in spirit; I replaced it with explicit serializers so nothing internal can leak.
