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
