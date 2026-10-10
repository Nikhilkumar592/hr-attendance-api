# REVIEW.md

## Defects Found in Starter Code

1. **Race conditions (Check-then-act)**
   - *Where*: `POST /employees` and `POST /attendance/punch-in`.
   - *What is wrong*: It checks `find_one()` before inserting. Two concurrent requests can bypass the check and create duplicates.
   - *Symptom*: Concurrent identical punch-ins would result in two records for the same day.
   - *Fix*: Created unique indexes on `emp_code` and `(emp_code, date)`. Caught `pymongo.errors.DuplicateKeyError` to return 409.

2. **Timezone and Truncation (R1)**
   - *Where*: `punch_in` and `create_employee`.
   - *What is wrong*: Uses `datetime.now()` resulting in naive local datetimes, which MongoDB stores as UTC incorrectly. Dates are not truncated to whole seconds. The calendar date is computed by calling `ts.date().isoformat()` on a naive datetime, breaking the overnight shift rule.
   - *Symptom*: Dates would shift depending on the server's local timezone. Overnight shifts would land on the wrong calendar day.
   - *Fix*: Force all datetimes to be timezone-aware (UTC internally), truncate milliseconds, and properly compute the IST calendar date by evaluating if the time is before `shift_end` on an overnight shift.

3. **Late Minutes Calculation (R2)**
   - *Where*: `compute_late_minutes`.
   - *What is wrong*: `minutes = int(seconds / 60)`. If seconds = 601 (10 mins 1 sec), `minutes` = 10. `10 > 10` is False, returning 0.
   - *Symptom*: Punching in at 09:40:01 for a 09:30 shift would yield 0 late minutes instead of 10.
   - *Fix*: Check `total_seconds() > 600`.

4. **Performance & Pagination (R10)**
   - *Where*: `GET /attendance` and `GET /employees`.
   - *What is wrong*: `/attendance` loads the entire collection into memory (`list(...)`) before slicing. `/employees` computes `total` without applying the filter `q`.
   - *Symptom*: Out-of-memory errors on 100k records. Wrong total count for employees.
   - *Fix*: Use `count_documents(q)` and `.skip().limit()` backed by proper indexes.

5. **Rounding rule (R4)**
   - *Where*: `compute_work_hours`.
   - *What is wrong*: Uses Python's built-in `round()`, which performs banker's rounding, not half-up.
   - *Symptom*: 4.125 would round to 4.12 instead of 4.13.
   - *Fix*: Use `decimal.Decimal` with `ROUND_HALF_UP`.
