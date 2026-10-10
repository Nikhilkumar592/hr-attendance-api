import os
import decimal
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def startup_event():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", ASCENDING)])
    db.attendance_logs.create_index("date")

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)

def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def truncate_dt(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)

def compute_calendar_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    ist_dt = punch_in_dt.astimezone(IST)
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    
    is_overnight = (h_end, m_end) <= (h_start, m_start)
    if is_overnight:
        if (ist_dt.hour, ist_dt.minute, ist_dt.second) < (h_end, m_end, 0):
            return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()

def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date_str: str) -> int:
    ist_dt = punch_in_dt.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    
    start_dt = datetime.fromisoformat(attendance_date_str).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    
    diff_seconds = (ist_dt - start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0

def compute_overtime(punch_out_dt: datetime, shift_end: str, shift_start: str, attendance_date_str: str) -> int:
    ist_dt = punch_out_dt.astimezone(IST)
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    
    is_overnight = (h_end, m_end) <= (h_start, m_start)
    end_date = date.fromisoformat(attendance_date_str)
    if is_overnight:
        end_date = end_date + timedelta(days=1)
        
    end_dt = datetime(end_date.year, end_date.month, end_date.day, h_end, m_end, 0, tzinfo=IST)
    diff_seconds = (ist_dt - end_dt).total_seconds()
    if diff_seconds >= 1800:
        return int(diff_seconds // 60)
    return 0

def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    diff_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    val = decimal.Decimal(str(diff_seconds)) / decimal.Decimal('3600')
    val = val.quantize(decimal.Decimal('0.01'), rounding=decimal.ROUND_HALF_UP)
    return float(val)

def compute_half_day(work_hours: float) -> bool:
    return work_hours < 4.50

def mongo_round_half_up(expr, decimals=2):
    multiplier = 10 ** decimals
    return { "$divide": [ { "$floor": { "$add": [ { "$multiply": [expr, multiplier] }, 0.5 ] } }, multiplier ] }

def format_record(doc):
    if not doc:
        return None
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_ms(doc.get("punch_out"))
    
    if "history" in doc:
        for h in doc["history"]:
            h["at"] = dt_to_ms(h.get("at"))
            changes = h.get("changes", {})
            for k in ["punch_in", "punch_out"]:
                if k in changes:
                    changes[k]["from"] = dt_to_ms(changes[k]["from"])
                    changes[k]["to"] = dt_to_ms(changes[k]["to"])
    else:
        doc["history"] = []
    
    doc["late_minutes"] = doc.get("late_minutes", 0)
    doc["overtime_minutes"] = doc.get("overtime_minutes", 0)
    doc["half_day"] = doc.get("half_day", False)
    return doc


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database down")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    try:
        doc = body.model_dump()
        doc["created_at"] = truncate_dt(datetime.now(timezone.utc))
        db.employees.insert_one(doc)
        
        doc.pop("_id", None)
        doc["created_at"] = dt_to_ms(doc["created_at"])
        return doc
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for d in items:
        if "created_at" in d:
            d["created_at"] = dt_to_ms(d["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid status for punch-in")
        
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = ms_to_dt(body.punched_at) if body.punched_at else truncate_dt(datetime.now(timezone.utc))
    d = compute_calendar_date(ts, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
        
    return format_record(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = ms_to_dt(body.punched_at) if body.punched_at else truncate_dt(datetime.now(timezone.utc))
    
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)]
    )
    
    if not record:
        raise HTTPException(404, "No punch-in found")
        
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")
        
    orig_in = record["punch_in"]
    if orig_in.tzinfo is None:
        orig_in = orig_in.replace(tzinfo=timezone.utc)
        
    diff_seconds = (ts - orig_in).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 86400:
        raise HTTPException(422, "Invalid punch out time")
        
    work_hours = compute_work_hours(orig_in, ts)
    overtime_minutes = compute_overtime(ts, emp["shift_end"], emp["shift_start"], record["date"])
    half_day = compute_half_day(work_hours)
    
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day
        }},
        return_document=True
    )
    
    if not res:
        raise HTTPException(409, "Record already punched out concurrently")
        
    return format_record(res)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from > date_to")
        
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size))
    
    return {"items": [format_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Record not found")
        
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(422, "No changes provided")
        
    new_status = body.status if body.status is not None else record["status"]
    
    orig_in = record.get("punch_in")
    orig_out = record.get("punch_out")
    if orig_in and orig_in.tzinfo is None: orig_in = orig_in.replace(tzinfo=timezone.utc)
    if orig_out and orig_out.tzinfo is None: orig_out = orig_out.replace(tzinfo=timezone.utc)

    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else orig_in
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else orig_out

    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch times for ABSENT/LEAVE")
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(422, "Presence status requires punch_in")
            
        d_in = compute_calendar_date(new_in, emp["shift_start"], emp["shift_end"])
        if d_in != date_str:
            raise HTTPException(422, "punch_in must stay on the same attendance date")
            
        if new_out:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_out - new_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24h")

    if new_status in ["ABSENT", "LEAVE"]:
        new_late = 0
        new_overtime = 0
        new_work = None
        new_half = False
    else:
        new_late = compute_late_minutes(new_in, emp["shift_start"], date_str)
        if new_out:
            new_work = compute_work_hours(new_in, new_out)
            new_overtime = compute_overtime(new_out, emp["shift_end"], emp["shift_start"], date_str)
            new_half = compute_half_day(new_work)
        else:
            new_work = None
            new_overtime = 0
            new_half = False
            
    changes = {}
    def check_diff(field, orig_val, new_val):
        if orig_val != new_val:
            changes[field] = {"from": orig_val, "to": new_val}
            
    check_diff("status", record["status"], new_status)
    check_diff("punch_in", orig_in, new_in)
    check_diff("punch_out", orig_out, new_out)
    check_diff("work_hours", record.get("work_hours"), new_work)
    check_diff("late_minutes", record.get("late_minutes", 0), new_late)
    check_diff("overtime_minutes", record.get("overtime_minutes", 0), new_overtime)
    check_diff("half_day", record.get("half_day", False), new_half)

    if not changes:
        raise HTTPException(422, "No fields actually changed")
        
    history_entry = {
        "at": truncate_dt(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_in,
            "punch_out": new_out,
            "work_hours": new_work,
            "late_minutes": new_late,
            "overtime_minutes": new_overtime,
            "half_day": new_half
        },
        "$push": {
            "history": history_entry
        }
    }
    
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        update_doc,
        return_document=True
    )
    return format_record(res)


# --------------------------------------------------------------------------- #
# Aggregation Pipeline Builders
# --------------------------------------------------------------------------- #
def get_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    start_date = f"{month}-01"
    y, m = map(int, month.split("-"))
    next_month = f"{y+1:04d}-01-01" if m == 12 else f"{y:04d}-{m+1:02d}-01"
    
    return [
        { "$match": { "emp_code": emp_code } },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": { "ec": "$emp_code" },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": { "$eq": ["$emp_code", "$$ec"] },
                            "date": { "$gte": start_date, "$lt": next_month }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$addFields": {
                "month_start": { "$dateFromString": { "dateString": start_date, "format": "%Y-%m-%d" } },
                "month_end": { "$dateFromString": { "dateString": next_month, "format": "%Y-%m-%d" } },
                "join_date": { "$dateFromString": { "dateString": "$joined_on", "format": "%Y-%m-%d" } }
            }
        },
        {
            "$addFields": {
                "days_in_month": { "$divide": [ { "$subtract": [ "$month_end", "$month_start" ] }, 86400000 ] }
            }
        },
        {
            "$addFields": {
                "working_days_array": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": { "$range": [0, "$days_in_month"] },
                                "as": "d",
                                "in": { "$add": [ "$month_start", { "$multiply": ["$$d", 86400000] } ] }
                            }
                        },
                        "as": "day",
                        "cond": {
                            "$and": [
                                { "$gte": ["$$day", "$join_date"] },
                                { "$ne": [{ "$dayOfWeek": "$$day" }, 1] },
                                { "$ne": [{ "$dayOfWeek": "$$day" }, 7] }
                            ]
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "working_days": { "$size": "$working_days_array" },
                "present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                { "$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]] },
                                                { "$ne": [{ "$dayOfWeek": { "$dateFromString": { "dateString": "$$this.date", "format": "%Y-%m-%d" } } }, 1] },
                                                { "$ne": [{ "$dayOfWeek": { "$dateFromString": { "dateString": "$$this.date", "format": "%Y-%m-%d" } } }, 7] }
                                            ]
                                        },
                                        { "$cond": [{ "$eq": ["$$this.half_day", True] }, 0.5, 1] },
                                        0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": { "input": "$logs", "as": "l", "cond": { "$eq": ["$$l.status", "LEAVE"] } }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": { "input": "$logs", "as": "l", "cond": { "$gt": [{ "$ifNull": ["$$l.late_minutes", 0] }, 0] } }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$logs.overtime_minutes"
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": { "$literal": month },
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        { "$eq": ["$working_days", 0] },
                        None,
                        mongo_round_half_up({ "$multiply": [ { "$divide": ["$present_days", "$working_days"] }, 100 ] }, 2)
                    ]
                }
            }
        }
    ]

def get_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    start_date = f"{month}-01"
    y, m = map(int, month.split("-"))
    next_month = f"{y+1:04d}-01-01" if m == 12 else f"{y:04d}-{m+1:02d}-01"
    
    match_stage = { "joined_on": { "$lt": next_month } }
    if department:
        match_stage["department"] = department
        
    return [
        { "$match": match_stage },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": { "ec": "$emp_code" },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": { "$eq": ["$emp_code", "$$ec"] },
                            "date": { "$gte": start_date, "$lt": next_month }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        { "$unwind": { "path": "$logs", "preserveNullAndEmptyArrays": True } },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": { "$addToSet": "$emp_code" },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    { "$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]] },
                                    { "$ne": [{ "$dayOfWeek": { "$dateFromString": { "dateString": "$logs.date", "format": "%Y-%m-%d" } } }, 1] },
                                    { "$ne": [{ "$dayOfWeek": { "$dateFromString": { "dateString": "$logs.date", "format": "%Y-%m-%d" } } }, 7] }
                                ]
                            },
                            { "$cond": [{ "$eq": ["$logs.half_day", True] }, 0.5, 1] },
                            0
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    { "$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]] },
                                    { "$ne": ["$logs.work_hours", None] }
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": { "$cond": [ { "$gt": [{ "$ifNull": ["$logs.late_minutes", 0] }, 0] }, 1, 0 ] }
                },
                "total_late_minutes": {
                    "$sum": { "$ifNull": ["$logs.late_minutes", 0] }
                },
                "leave_count": {
                    "$sum": { "$cond": [ { "$eq": ["$logs.status", "LEAVE"] }, 1, 0 ] }
                },
                "on_duty_count": {
                    "$sum": { "$cond": [ { "$eq": ["$logs.status", "ON_DUTY"] }, 1, 0 ] }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": { "$size": "$emp_codes" },
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        { "$gt": [{ "$size": "$valid_work_hours" }, 0] },
                        mongo_round_half_up({ "$avg": "$valid_work_hours" }, 2),
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        { "$sort": { "department": 1 } }
    ]

def get_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> List[Dict[str, Any]]:
    start_date = f"{month}-01"
    y, m = map(int, month.split("-"))
    next_month = f"{y+1:04d}-01-01" if m == 12 else f"{y:04d}-{m+1:02d}-01"
    
    pipeline = [
        {
            "$match": {
                "date": { "$gte": start_date, "$lt": next_month },
                "late_minutes": { "$gt": 0 }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": { "$sum": "$late_minutes" },
                "late_count": { "$sum": 1 }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        { "$unwind": "$emp" }
    ]
    
    if department:
        pipeline.append({ "$match": { "emp.department": department } })
        
    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": { "total_late_minutes": -1 },
                "output": {
                    "rank": { "$rank": {} }
                }
            }
        },
        { "$match": { "rank": { "$lte": limit } } },
        { "$sort": { "rank": 1, "emp_code": 1 } }
    ])
    return pipeline

def get_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    return [
        {
            "$documents": [ { "start": from_date, "end": to_date } ]
        },
        {
            "$addFields": {
                "start_d": { "$dateFromString": { "dateString": "$start", "format": "%Y-%m-%d" } },
                "end_d": { "$dateFromString": { "dateString": "$end", "format": "%Y-%m-%d" } }
            }
        },
        {
            "$addFields": {
                "days": { "$divide": [ { "$subtract": [ "$end_d", "$start_d" ] }, 86400000 ] }
            }
        },
        {
            "$project": {
                "dates": {
                    "$map": {
                        "input": { "$range": [ 0, { "$add": [ "$days", 1 ] } ] },
                        "as": "d",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": { "$add": [ "$start_d", { "$multiply": [ "$$d", 86400000 ] } ] }
                            }
                        }
                    }
                }
            }
        },
        { "$unwind": "$dates" },
        { "$project": { "date": "$dates", "_id": 0 } },
        {
            "$lookup": {
                "from": "employees",
                "let": { "cur_date": "$date" },
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": { "$lte": ["$joined_on", "$$cur_date"] }
                        }
                    }
                ],
                "as": "emps"
            }
        },
        {
            "$addFields": {
                "headcount": { "$size": "$emps" },
                "is_working_day": {
                    "$let": {
                        "vars": {
                            "dow": { "$dayOfWeek": { "$dateFromString": { "dateString": "$date", "format": "%Y-%m-%d" } } }
                        },
                        "in": { "$and": [ { "$ne": ["$$dow", 1] }, { "$ne": ["$$dow", 7] } ] }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": { "cur_date": "$date", "emp_codes": "$emps.emp_code" },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    { "$eq": ["$date", "$$cur_date"] },
                                    { "$in": ["$emp_code", "$$emp_codes"] }
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        { "$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]] },
                                        { "$cond": [{ "$eq": ["$$this.half_day", True] }, 0.5, 1] },
                                        0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": { "input": "$logs", "as": "l", "cond": { "$gt": [{ "$ifNull": ["$$l.late_minutes", 0] }, 0] } }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        { "$and": [ { "$eq": ["$is_working_day", True] }, { "$gt": ["$headcount", 0] } ] },
                        mongo_round_half_up({ "$divide": ["$present_count", "$headcount"] }, 4),
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": { "date": 1 },
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": { "documents": [-6, 0] }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        { "$ne": ["$moving_avg_7d_raw", None] },
                        mongo_round_half_up("$moving_avg_7d_raw", 4),
                        None
                    ]
                }
            }
        }
    ]

# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    if not db.employees.find_one({"emp_code": emp_code}):
        raise HTTPException(404, "Employee not found")
        
    pipeline = get_employee_monthly_pipeline(emp_code, month)
    res = list(db.employees.aggregate(pipeline))
    if not res:
        raise HTTPException(404, "Employee not found")
    return res[0]

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    pipeline = get_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    pipeline = get_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")
        
    d1 = date.fromisoformat(from_date)
    d2 = date.fromisoformat(to_date)
    if d2 < d1:
        raise HTTPException(422, "to < from")
    if (d2 - d1).days > 92:
        raise HTTPException(422, "Range exceeds 92 days")
        
    pipeline = get_department_trend_pipeline(department, from_date, to_date)
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        skip = (page - 1) * page_size
        
        exp = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size
            },
            "verbosity": "executionStats"
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
        
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422)
        pipeline = get_employee_monthly_pipeline(emp_code, month)
        exp = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
        
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422)
        pipeline = get_department_summary_pipeline(month, department)
        exp = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
        
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422)
        pipeline = get_late_leaderboard_pipeline(month, limit, department)
        exp = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422)
        pipeline = get_department_trend_pipeline(department, from_date, to_date)
        exp = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
        
    else:
        raise HTTPException(422, "Invalid endpoint name")
