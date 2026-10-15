# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # a local .env also works
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). Needs MongoDB 6.0+ (`$densify`, `$setWindowFields`).

## Status
All endpoints in the contract are implemented, including the three aggregation pipelines and `/admin/explain/{endpoint}`.
Nothing is known to be missing.

## Interpretations worth knowing
- `moving_avg_7d` averages the unrounded daily rates; the result is rounded half-up to 4 decimals.
- "Range exceeds 92 days" for the trend means more than 92 days counting both ends.
- Overnight shifts: late minutes are counted from `shift_start` on the attendance date, so a punch-in after midnight is very late.
- Records dated before an employee's `joined_on` are not filtered out of the analytics.

-    Walkthrough: what the project is, how it works, how to test it                                                                                                                                         │
│                                                                                                                                                                                                        │
│ No code changes. This plan is the explanation itself (the project is already built in candidate_kit/app/main.py).                                                                                      │
│                                                                                                                                                                                                        │
│ 1. What the project is                                                                                                                                                                                 │
│                                                                                                                                                                                                        │
│ An HR attendance backend (a REST API). Employees punch in/out; HR can correct records (with an audit trail); managers ask analytics questions. Python + FastAPI for the web layer, MongoDB for storage. It is an HROne hiring assignment, graded by an automated test-suite that calls the API with a large hidden dataset, then a live code walkthrough.                                             │
│                                                                                                                                                                                                        │
│ Two MongoDB collections:                                                                                                                                                                               │
│ - employees: emp_code (e.g. EMP0001), name, email, department, shift_start/shift_end (IST "HH:MM"), joined_on, created_at.                                                                             │
│ - attendance_logs: one document per (emp_code, date): status, punch_in, punch_out, work_hours, late_minutes, overtime_minutes, half_day, history[].                                                    │
│                                                                                                                                                                                                        │
│ Key conventions: times in the API are epoch milliseconds; in MongoDB they are BSON datetimes (UTC). Dates are "YYYY-MM-DD" in IST.                                                                     │
│                                                                                                                                                                                                        │
│ 2. What each endpoint does and how it is built                                                                                                                                                         │
│                                                                                                                                                                                                        │
│ - Business rules in one place: compute_derived() applies R2 late (strictly > 10:00 after shift start, minutes counted from shift start), R3 overtime (>= 30 min), R4 work hours (Decimal, half-up), R5 half day (rounded hours < 4.5). Punch-in, punch-out and PATCH all reuse it. attendance_date() applies R1 (IST date; overnight shifts belong to the previous day).                                    │
│ - POST /employees: validated by pydantic; the unique index on emp_code makes duplicates a 409 even under parallel requests.                                                                            │
│ - GET /employees: filter by department, sorted by emp_code, paginated.                                                                                                                                 │
│ - POST /attendance/punch-in: insert directly; the unique (emp_code, date) index means exactly one of N parallel requests succeeds, the rest get 409 (no check-then-insert).                            │
│ - POST /attendance/punch-out: finds the latest record with punch_in <= punched_at, then closes it with update_one({_id, punch_out: None}); a second simultaneous call matches nothing and gets 409.    │
│ - GET /attendance: indexed find + sort(date desc, emp_code asc) + skip/limit; nothing loaded into memory.                                                                                              │
│ - PATCH /attendance/{emp_code}/{date}: recomputes everything from the final times, records only the fields that changed in one history entry, and uses an optimistic lock (update only if history length is unchanged, retry, then 409) so concurrent edits never lose history.                                                                                                                        │
│ - Analytics (all MongoDB aggregation pipelines):                                                                                                                                                       │
│   - monthly: $match the employee's month, $group into present days (Mon-Fri, half day 0.5), leave, late, overtime; working days from joined_on.                                                        │
│   - department summary: starts from employees, $lookups logs, $unwind with preserveNullAndEmptyArrays so zero-log employees still count in headcount; average hours is over records.                   │
│   - late leaderboard: group late minutes per employee, $lookup the employee, $setWindowFields with $rank (ties share rank, next skipped), then filter rank <= limit.                                   │
│   - trend: group by day, $densify to one row per calendar day, compute headcount/rate, $setWindowFields $avg over the previous 6 rows for the 7-day moving average.                                    │
│ - GET /admin/explain/{endpoint}: builds the same query/pipeline as the real endpoint and returns MongoDB's executionStats explain, which shows IXSCAN.                                                 │
│ - Indexes (created at startup): logs (emp_code, date) unique and (date desc, emp_code); employees emp_code unique, (department, emp_code), joined_on.                                                  │
│                                                                                                                                                                                                        │
│ 3. How to test everything (in detail)                                                                                                                                                                  │
│                                                                                                                                                                                                        │
│ Start: brew services start mongodb-community; python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt; uvicorn app.main:app --port 8000. Open http://localhost:8000/docs (Swagger UI lets you click through every endpoint). Use a fresh DB: export MONGO_DB=test1.                                                                                                             │
│                                                                                                                                                                                                        │
│ A. Health: curl localhost:8000/health -> {"status":"ok"}. Stop mongod -> 503.                                                                                                                          │
│                                                                                                                                                                                                        │
│ B. Employees: create EMP0001 (201, created_at is an integer, no _id); create again (409); emp_code:"X1" (422); same shift_start/shift_end (422); list with department, page, page_size and check total follows the filter.                                                                                                                                                                                    │
│                                                                                                                                                                                                        │
│ C. Punch-in rules (use punched_at in ms; e.g. python3 -c "from datetime import *;print(int(datetime(2026,7,6,9,40,1,tzinfo=timezone(timedelta(hours=5,minutes=30))).timestamp()*1000))"): shift 09:30: 09:40:00 -> late 0; 09:40:01 -> 10; 10:15:59 -> 45. Second punch-in same date -> 409. Seconds-looking value, float, string -> 422. status ABSENT -> 422. Unknown employee -> 404.                      │
│                                                                                                                                                                                                        │
│ D. Punch-out: 30 min after shift end -> overtime 30 (29 -> 0); 3.5 h -> half_day true; 16218 s after punch-in -> work_hours 4.51 (half-up), half_day false; punch-out again -> 409; before punch-in -> 422; > 24 h -> 422.                                                                                                                                                                                    │
│                                                                                                                                                                                                        │
│ E. Overnight shift (22:00-06:00): punch in 21:55 on 7/8, out 06:40 on 7/9 -> date 2026-07-08, overtime 40, work_hours 8.75.                                                                            │
│                                                                                                                                                                                                        │
│ F. PATCH: change punch_in -> late/work_hours recomputed, one history entry listing only changed fields; repeat same body -> 422; status ABSENT with a punch time -> 422; punch_in on another date -> 422; unknown date -> 404; LEAVE clears punch times.                                                                                                                                                    │
│                                                                                                                                                                                                        │
│ G. Concurrency (bash): for i in $(seq 10); do curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8000/attendance/punch-in -H 'content-type: application/json' -d '{"emp_code":"EMP0001","punched_at":1783312500000}' & done; wait -> one 201, nine 409. Same idea for create-employee, punch-out and PATCH (history length must equal the number of 200s).              │
│                                                                                                                                                                                                        │
│ H. Analytics with awkward data: seed a few documents by hand (mongosh/pymongo) covering: an employee with no logs, a mid-month joiner, a Saturday record, a half day, LEAVE, ON_DUTY, an orphan log with an unknown emp_code, three employees tied on late minutes. Check: headcount counts zero-log employees but not future joiners; weekend records add to late/overtime but not present_days; ties rank 1,2,2,4 and limit returns all tied rows; orphan logs ignored; trend returns exactly one row per day with attendance_rate null on weekends and shorter windows at the start.                       │
│                                                                                                                                                                                                        │
│ I. Speed + explain: generate ~100k logs (500 employees x 200 days), then hit every analytics endpoint (expect milliseconds) and GET /admin/explain/{attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend} with the same query params as the endpoint; search the JSON for "IXSCAN" (present) and "COLLSCAN" (absent). Missing required param -> 422.                                                                                                                                                    │
│                                                                                                                                                                                                        │
│ Already done: the above was run during development and passed (see previous message for results). An automated pytest file could be added if wanted, but is not part of the submission.                │
│                                                                                                                                                                                                        │
│ 4. Before submitting                                                                                                                                                                                   │
│                                                                                                                                                                                                        │
│ Rewrite DECISIONS.md in your own words; read through main.py so you can explain it live; push to a public repo (.gitignore already excludes kit files and the problem statement); submit the link on the form before 11:59 PM IST today.
