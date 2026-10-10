# HROne Employee Attendance & Analytics API

## How to run

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Start MongoDB 6.0+ locally (or configure `MONGO_URI` / `MONGO_DB` in a `.env` file).
3. Start the application:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   
## Notes
- **Indexes**: All required indexes are created idempotently at application startup to ensure optimal performance. 
- **Time Zones**: Handled strictly via conversion to IST for calendar dates/shifts, and stored exactly as BSON Date / Epoch ms everywhere else as per R1.
- **Analytics**: All analytics endpoints run exclusively as MongoDB aggregation pipelines and are capable of efficiently processing >100,000 logs.
- **Missing Log Handing**: The department summary correctly processes headcount inclusion of employees without logs by starting the aggregation on the `employees` collection and leveraging `$lookup`.
