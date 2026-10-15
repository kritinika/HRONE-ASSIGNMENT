"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Rules R1-R10 are defined in openapi.yaml; what is stored is described in DATA_MODEL.md.
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

# tz_aware=True: PyMongo returns aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
GRACE_SECONDS = 10 * 60  # R2
MON_FRI = [2, 3, 4, 5, 6]  # MongoDB $dayOfWeek: 1 = Sunday
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def ensure_indexes():
    """Idempotent: create_index is a no-op when the index already exists."""
    logs, emps = db.attendance_logs, db.employees
    # one record per (employee, day): the guarantee behind race-safe punch-in; serves per-employee queries
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    # date-first: serves the sorted list, date ranges, and the month/range matches of the analytics
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    emps.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    emps.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    emps.create_index([("joined_on", ASCENDING)], name="joined_on")


# --------------------------------------------------------------------------- #
# Helpers: time conversion
# --------------------------------------------------------------------------- #
def aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC (that is how MongoDB stores it)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (aware(dt) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def now_ms_precision() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)  # BSON dates hold milliseconds


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def shift_dt(day: Date, hhmm: str, add_days: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST) + timedelta(days=add_days)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts before shift_end belong to the previous day."""
    local = aware(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(emp):
        h, m = map(int, emp["shift_end"].split(":"))
        if local.time() < time(h, m):
            day -= timedelta(days=1)
    return day.isoformat()


def round_half_up(value, places: int) -> float:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_derived(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, day: str) -> dict:
    """R2-R5. Used by punch-in, punch-out and regularize so the rules live in one place."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    d = Date.fromisoformat(day)
    punch_in = aware(punch_in).replace(microsecond=0)

    # R2: strictly more than 10:00 after shift_start; minutes counted from shift_start
    late_seconds = (punch_in - shift_dt(d, emp["shift_start"])).total_seconds()
    if late_seconds > GRACE_SECONDS:
        out["late_minutes"] = int(late_seconds // 60)

    if punch_out is not None:
        punch_out = aware(punch_out).replace(microsecond=0)
        seconds = int((punch_out - punch_in).total_seconds())
        out["work_hours"] = round_half_up(Decimal(seconds) / Decimal(3600), 2)  # R4
        out["half_day"] = Decimal(str(out["work_hours"])) < Decimal("4.50")  # R5 uses the rounded value
        end = shift_dt(d, emp["shift_end"], 1 if is_overnight(emp) else 0)  # R3
        overtime = int((punch_out - end).total_seconds() // 60)
        out["overtime_minutes"] = overtime if overtime >= 30 else 0
    return out


# --------------------------------------------------------------------------- #
# Helpers: serialization
# --------------------------------------------------------------------------- #
def api_value(v):
    return to_ms(v) if isinstance(v, datetime) else v


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def serialize_attendance(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        changes = {k: {"from": api_value(c.get("from")), "to": api_value(c.get("to"))} for k, c in h["changes"].items()}
        history.append({"at": to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_RE)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: Date

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        # omitted means "unchanged"; an explicit null is ambiguous, so reject it
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_ms_precision()
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    pin = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(pin, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": pin,
        "punch_out": None,
        "history": [],
        **compute_derived(body.status, pin, None, emp, day),
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    pout = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    # latest record that started at or before the punch-out (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": pout}},
        sort=[("date", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = aware(rec["punch_in"]).replace(microsecond=0)
    if pout <= pin or pout - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    derived = compute_derived(rec["status"], pin, pout, emp, rec["date"])
    update = {"punch_out": pout, **derived}
    # the punch_out: None filter makes the close atomic - a concurrent second request matches nothing
    res = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(update)
    return serialize_attendance(rec)


def build_list_query(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


def check_date_order(date_from, date_to):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")


LIST_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    check_date_order(date_from, date_to)
    q = build_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(LIST_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


TRACKED = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Date, body: RegularizeIn):
    emp = get_employee(emp_code)
    day = date.isoformat()
    sent = body.model_fields_set

    for _attempt in range(3):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        old = serialize_raw(rec)

        status = body.status or old["status"]
        if status in ("ABSENT", "LEAVE"):
            if "punch_in" in sent or "punch_out" in sent:
                raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")
            pin = pout = None
        else:
            pin = from_ms(body.punch_in) if "punch_in" in sent else old["punch_in"]
            pout = from_ms(body.punch_out) if "punch_out" in sent else old["punch_out"]
            if pin is None:
                raise HTTPException(422, f"{status} requires a punch_in")
            pin = aware(pin).replace(microsecond=0)
            if attendance_date(pin, emp) != day:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if pout is not None:
                pout = aware(pout).replace(microsecond=0)
                if pout <= pin or pout - pin > timedelta(hours=24):
                    raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

        new = {"status": status, "punch_in": pin, "punch_out": pout, **compute_derived(status, pin, pout, emp, day)}
        changes = {k: {"from": old[k], "to": new[k]} for k in TRACKED if old[k] != new[k]}
        if not changes:
            raise HTTPException(422, "request changes nothing")

        entry = {"at": now_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        n = len(rec.get("history") or [])
        history_unchanged = {"history": {"$size": n}} if n else {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
        # optimistic lock: only write if nobody appended to history since we read the record
        res = db.attendance_logs.update_one(
            {"_id": rec["_id"], **history_unchanged},
            {"$set": new, "$push": {"history": entry}},
        )
        if res.matched_count:
            rec.update(new)
            rec["history"] = (rec.get("history") or []) + [entry]
            return serialize_attendance(rec)
    raise HTTPException(409, "record was modified concurrently, please retry")


def serialize_raw(rec: dict) -> dict:
    """The stored values of the tracked fields (with defaults) in storage form (datetimes)."""
    return {
        "status": rec["status"],
        "punch_in": aware(rec["punch_in"]) if rec.get("punch_in") else None,
        "punch_out": aware(rec["punch_out"]) if rec.get("punch_out") else None,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def month_bounds(month: str):
    y, m = int(month[:4]), int(month[5:])
    first = Date(y, m, 1)
    last = Date(y, m, calendar.monthrange(y, m)[1])
    return first, last


def count_working_days(start: Date, end: Date) -> int:
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def present_weight_expr(weekdays_only: bool):
    """1 (or 0.5 for a half day) for presence-status records; optionally only Mon-Fri."""
    cond = [{"$in": ["$status", PRESENCE]}]
    if weekdays_only:
        cond.append({"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, MON_FRI]})
    half = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    return {"$cond": [{"$and": cond}, half, 0]}


def late_flag_expr():
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def month_range_match(first: Date, last: Date) -> dict:
    return {"$gte": first.isoformat(), "$lte": last.isoformat()}


def build_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": month_range_match(first, last)}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_weight_expr(True)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": late_flag_expr()},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    working_days = count_working_days(max(first, Date.fromisoformat(emp["joined_on"])), last)
    rows = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    present = Decimal(str(r.get("present_days", 0)))
    pct = round_half_up(present / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


def build_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    has_hours = {"$and": [{"$in": ["$status", PRESENCE]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    emp_match = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        # per-employee month stats; employees with no logs get an empty array (not dropped)
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": month_range_match(first, last)}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": present_weight_expr(True)},
                    "hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
                    "hours_cnt": {"$sum": {"$cond": [has_hours, 1, 0]}},
                    "late_count": {"$sum": late_flag_expr()},
                    "late_total": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},  # keep zero-log employees
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "hours_sum": {"$sum": {"$ifNull": ["$s.hours_sum", 0]}},
            "hours_cnt": {"$sum": {"$ifNull": ["$s.hours_cnt", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "late_total": {"$sum": {"$ifNull": ["$s.late_total", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(build_summary_pipeline(month, department)):
        avg = None
        if r["hours_cnt"]:
            avg = round_half_up(Decimal(str(r["hours_sum"])) / r["hours_cnt"], 2)
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["late_total"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


LimitQ = Annotated[int, Query(ge=1, le=50)]


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": month_range_match(first, last), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: LimitQ = 10, department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(build_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def check_trend_range(frm: Date, to: Date):
    if to < frm:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (to - frm).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")


def build_trend_pipeline(department: str, frm: Date, to: Date) -> list:
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = [e["joined_on"] for e in emps]
    lo = datetime(frm.year, frm.month, frm.day, tzinfo=UTC)
    hi = datetime(to.year, to.month, to.day, tzinfo=UTC) + timedelta(days=1)  # $densify's upper bound is exclusive
    return [
        {"$match": {"date": {"$gte": frm.isoformat(), "$lte": to.isoformat()}, "emp_code": {"$in": codes}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": present_weight_expr(False)}, "late_count": {"$sum": late_flag_expr()}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        # one row per calendar day, including days with no records
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$addFields": {
            "ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, MON_FRI]},
        }},
        {"$addFields": {"headcount": {"$size": {"$filter": {"input": joined, "cond": {"$lte": ["$$this", "$ds"]}}}}}},
        {"$addFields": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": "$ds", "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "rate": 1, "moving": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, frm: Annotated[Date, Query(alias="from")], to: Date):
    check_trend_range(frm, to)
    items = []
    for r in db.attendance_logs.aggregate(build_trend_pipeline(department, frm, to)):
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": r["late_count"],
            "attendance_rate": None if r["rate"] is None else round_half_up(r["rate"], 4),
            "moving_avg_7d": None if r["moving"] is None else round_half_up(r["moving"], 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[Annotated[str, Query(pattern=MONTH_RE)]] = None,
    department: Optional[str] = None,
    limit: LimitQ = 10,
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    status: Optional[Status] = None,
    frm: Annotated[Optional[Date], Query(alias="from")] = None,
    to: Optional[Date] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    # each branch builds the query exactly as the real endpoint does
    if endpoint == "attendance_list":
        check_date_order(date_from, date_to)
        collection = "attendance_logs"
        cmd = {
            "find": collection,
            "filter": build_list_query(emp_code, date_from, date_to, status),
            "sort": dict(LIST_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", build_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            need(month=month)
            collection, pipeline = "employees", build_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            collection, pipeline = "attendance_logs", build_leaderboard_pipeline(month, limit, department)
        else:
            need(department=department, **{"from": frm, "to": to})
            check_trend_range(frm, to)
            collection, pipeline = "attendance_logs", build_trend_pipeline(department, frm, to)
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
