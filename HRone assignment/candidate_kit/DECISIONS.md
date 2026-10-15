# DECISIONS.md

1. **Indexes.** On `attendance_logs`: a unique `(emp_code, date)` index, which makes punch-in race-safe and serves per-employee queries and the monthly summary; and `(date desc, emp_code)`, which serves the sorted list, date ranges, the leaderboard and the trend. On `employees`: unique `emp_code`, `(department, emp_code)` for the filtered list, and `joined_on` for headcount. I rejected a `status` index: it has only five values, so it would barely narrow anything.
2. **Punch-in race.** Both requests build the record and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept exactly one; the other gets `DuplicateKeyError`, which I turn into 409. There is no read before the write.
3. **Ties.** The pipeline ranks with `$rank`, so ties share a rank and the next one is skipped. `limit` filters on `rank <= limit` after ranking, so tied employees at the cutoff are all returned, ordered by `emp_code`.
4. **Headcount.** The department summary starts from `employees`, not from the logs. It joins logs with `$lookup` and keeps employees with no logs using `$unwind` with `preserveNullAndEmptyArrays`, so they still add 1 to headcount.
5. **At 100x.** I would pre-aggregate daily and monthly totals per employee, so analytics stop scanning raw logs.
