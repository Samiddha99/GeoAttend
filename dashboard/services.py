"""
All analytics live here.  Views stay thin: parse filters → call a service →
return JSON.  Percentages are always computed as

        present marks  /  classes conducted  × 100

where "classes conducted" is the number of non-cancelled sessions for that
subject + batch inside the selected date range.
"""
import datetime as dt
from collections import defaultdict

from django.conf import settings
from django.db.models import Avg, Count, F
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, Subject
from academics.selectors import departments_for, students_qs_for
from attendance.models import AbsenceReason, AttendanceRecord, AttendanceSession
from attendance.services import planned_cover_for
from core.utils import pct

PRESENT = ["PRESENT", "MANUAL"]


# --------------------------------------------------------------------------- #
#  Scoping
# --------------------------------------------------------------------------- #
def scoped_sessions(user, f=None):
    # `batch__is_active` keeps archived cohorts out of every statistic derived
    # from attendance — see academics.selectors for the rule.
    qs = AttendanceSession.objects.exclude(
        status=AttendanceSession.Status.CANCELLED
    ).filter(batch__is_active=True)
    if user.is_head:
        qs = qs.filter(subject__department__institute=user.institute)
    elif user.is_hod:
        qs = qs.filter(subject__department__in=departments_for(user))
    elif user.is_teacher:
        qs = qs.filter(teacher=user)
    elif user.is_student or user.is_guardian:
        from accounts.guardians import acting_profile

        profile = acting_profile(user)
        if profile is None or not profile.batch.is_active:
            return qs.none()
        qs = qs.filter(batch=profile.batch, subject__enrollments__student=profile,
                       subject__enrollments__is_active=True).distinct()
    else:
        return qs.none()

    # Clearing the default ordering is essential: with DISTINCT + Meta.ordering
    # SQLite/Postgres would drag `created_at` into the SELECT list and silently
    # break every GROUP BY below.
    qs = qs.order_by()
    if f is None:
        return qs
    qs = qs.filter(session_date__gte=f.start, session_date__lte=f.end)
    if f.department:
        qs = qs.filter(subject__department_id=f.department)
    if f.batch:
        qs = qs.filter(batch_id=f.batch)
    if f.subject:
        qs = qs.filter(subject_id=f.subject)
    if f.subject_type:
        qs = qs.filter(subject__subject_type=f.subject_type)
    if f.degree:
        qs = qs.filter(subject__degree=f.degree)
    if f.teacher:
        qs = qs.filter(teacher_id=f.teacher)
    if f.semester:
        qs = qs.filter(subject__semester=f.semester)
    return qs.order_by()


def scoped_students(user, f=None):
    qs = students_qs_for(user).select_related("user", "batch", "department")
    if f is None:
        return qs
    if f.department:
        qs = qs.filter(department_id=f.department)
    if f.batch:
        qs = qs.filter(batch_id=f.batch)
    if f.subject:
        qs = qs.filter(enrollments__subject_id=f.subject, enrollments__is_active=True)
    if f.subject_type:
        # Same reading as the semester filter below: students taking at least
        # one subject of that type, since a student has no type of their own.
        qs = qs.filter(enrollments__subject__subject_type=f.subject_type,
                       enrollments__is_active=True)
    if f.degree:
        # And the same again for degree — a student is not "in" a degree here,
        # their subjects are. Keeping the three filters identical in shape is
        # what stops them disagreeing about what a filtered list means.
        qs = qs.filter(enrollments__subject__degree=f.degree,
                       enrollments__is_active=True)
    if f.semester:
        # A student is not "in" a semester — their subjects are. So this means
        # "students taking at least one subject of that semester", which is what
        # keeps the student list consistent with the session-side filter.
        qs = qs.filter(enrollments__subject__semester=f.semester,
                       enrollments__is_active=True)
    if f.student:
        qs = qs.filter(pk=f.student)
    return qs.distinct()


# --------------------------------------------------------------------------- #
#  Building blocks
# --------------------------------------------------------------------------- #
def session_counts(sessions):
    """{(subject_id, batch_id): number of classes conducted}"""
    out = {}
    for row in sessions.order_by().values("subject_id", "batch_id").annotate(n=Count("id")):
        out[(row["subject_id"], row["batch_id"])] = row["n"]
    return out


def present_counts(sessions):
    """{(student_id, subject_id): times present}"""
    rows = (
        AttendanceRecord.objects.filter(session__in=sessions, status__in=PRESENT)
        .order_by()
        .values("student_id", "session__subject_id")
        .annotate(n=Count("id"))
    )
    return {(r["student_id"], r["session__subject_id"]): r["n"] for r in rows}


def manual_counts(sessions):
    """
    {(student_id, subject_id): times a teacher marked them present}

    A strict subset of `present_counts` — MANUAL is one of the two statuses
    that count as present, never a third category. So a row's manual figure is
    always ≤ its attended figure, and subtracting gives the marks the student
    made themselves.

    Kept as a separate query rather than folded into `present_counts` because
    most callers want the total and only some want the split; one extra grouped
    count is cheaper than making every caller carry a second dictionary it does
    not read.
    """
    rows = (
        AttendanceRecord.objects
        .filter(session__in=sessions, status=AttendanceRecord.Status.MANUAL)
        .order_by()
        .values("student_id", "session__subject_id")
        .annotate(n=Count("id"))
    )
    return {(r["student_id"], r["session__subject_id"]): r["n"] for r in rows}


def enrollment_pairs(students, sessions=None, subject_filter=None):
    """[(student_id, subject_id)] for the students in scope."""
    qs = Enrollment.objects.filter(student__in=students, is_active=True)
    if subject_filter:
        qs = qs.filter(subject_id=subject_filter)
    return list(qs.values_list("student_id", "subject_id"))


# --------------------------------------------------------------------------- #
#  1. Headline KPIs
# --------------------------------------------------------------------------- #
def kpi_summary(user, f):
    sessions = scoped_sessions(user, f)
    students = scoped_students(user, f)
    s_counts = session_counts(sessions)
    p_counts = present_counts(sessions)
    m_counts = manual_counts(sessions)

    student_ids = set(students.values_list("id", flat=True))
    student_batch = dict(students.values_list("id", "batch_id"))

    total_slots = total_present = total_manual = 0
    per_student = defaultdict(lambda: [0, 0])  # id -> [present, slots]
    for student_id, subject_id in enrollment_pairs(students, subject_filter=f.subject):
        if student_id not in student_ids:
            continue
        classes = s_counts.get((subject_id, student_batch.get(student_id)), 0)
        if not classes:
            continue
        present = p_counts.get((student_id, subject_id), 0)
        total_slots += classes
        total_present += present
        total_manual += m_counts.get((student_id, subject_id), 0)
        per_student[student_id][0] += present
        per_student[student_id][1] += classes

    below = sum(1 for p, t in per_student.values() if t and pct(p, t) < 75)
    today = timezone.localdate()
    return {
        "range": f.label,
        "classes_conducted": sessions.count(),
        "students": len(student_ids),
        "subjects": sessions.values("subject_id").distinct().count(),
        "overall_percentage": pct(total_present, total_slots),
        "present_marks": total_present,
        # How many of those present marks a teacher entered by hand, and what
        # share of the present marks that is. The denominator is present marks,
        # not possible marks: the question being answered is "how much of this
        # attendance was self-marked", not "what fraction of the class".
        "manual_marks": total_manual,
        "manual_share": pct(total_manual, total_present),
        "possible_marks": total_slots,
        "classes_today": sessions.filter(session_date=today).count(),
        "open_now": AttendanceSession.objects.filter(
            id__in=sessions.values("id"), status=AttendanceSession.Status.OPEN,
            expires_at__gt=timezone.now(),
        ).count(),
        "below_threshold": below,
        "avg_class_strength": round(
            sessions.aggregate(a=Avg("expected_count"))["a"] or 0, 1
        ),
    }


# --------------------------------------------------------------------------- #
#  2. Student-wise report
# --------------------------------------------------------------------------- #
def student_report(user, f, limit=3000):
    sessions = scoped_sessions(user, f)
    students = scoped_students(user, f)[:limit]
    student_list = list(students)
    s_counts = session_counts(sessions)
    p_counts = present_counts(sessions)
    m_counts = manual_counts(sessions)

    subject_names = {
        s.id: {"code": s.code, "name": s.name, "subject_type": s.subject_type,
               "degree": s.degree}
        for s in Subject.objects.filter(
            id__in={sid for (sid, _b) in s_counts.keys()}
        )
    }

    by_student = defaultdict(list)
    for student_id, subject_id in enrollment_pairs(student_list, subject_filter=f.subject):
        by_student[student_id].append(subject_id)

    rows = []
    for student in student_list:
        present_total = slots_total = manual_total = 0
        subjects = []
        for subject_id in by_student.get(student.id, []):
            classes = s_counts.get((subject_id, student.batch_id), 0)
            if not classes:
                continue
            present = p_counts.get((student.id, subject_id), 0)
            manual = m_counts.get((student.id, subject_id), 0)
            present_total += present
            slots_total += classes
            manual_total += manual
            meta = subject_names.get(
                subject_id, {"code": "?", "name": "", "subject_type": "", "degree": ""})
            subjects.append({
                "subject_id": subject_id,
                "code": meta["code"],
                "name": meta["name"],
                "subject_type": meta["subject_type"],
                "degree": meta["degree"],
                "held": classes,
                "attended": present,
                "manual": manual,
                "percentage": pct(present, classes),
            })
        subjects.sort(key=lambda x: x["code"])
        rows.append({
            "student_id": student.id,
            "name": student.name,
            "roll": student.class_roll,
            "exam_roll": student.exam_roll,
            "email": student.email,
            "batch": student.batch.label,
            "batch_id": student.batch_id,
            "department": student.department.name,
            "held": slots_total,
            "attended": present_total,
            "manual": manual_total,
            "manual_share": pct(manual_total, present_total),
            "percentage": pct(present_total, slots_total),
            "subjects": subjects,
            "status": "at-risk" if slots_total and pct(present_total, slots_total) < 75 else "ok",
        })
    rows.sort(key=lambda r: r["percentage"])
    return rows


# --------------------------------------------------------------------------- #
#  3. Subject-wise report
# --------------------------------------------------------------------------- #
def subject_report(user, f):
    sessions = scoped_sessions(user, f)
    rows_map = {}
    for s in sessions.select_related("subject", "batch", "subject__department"):
        key = (s.subject_id, s.batch_id)
        row = rows_map.setdefault(key, {
            "subject_id": s.subject_id,
            "code": s.subject.code,
            "name": s.subject.name,
            "subject_type": s.subject.subject_type,
            "degree": s.subject.degree,
            "semester": s.subject.semester,
            "department": s.subject.department.name,
            "batch": s.batch.label,
            "batch_id": s.batch_id,
            "classes": 0,
            "enrolled": 0,
            "present_marks": 0,
            "manual_marks": 0,
            "teachers": set(),
        })
        row["classes"] += 1
        row["enrolled"] = max(row["enrolled"], s.expected_count)
        row["teachers"].add(s.teacher.full_name or s.teacher.email)

    marks = (
        AttendanceRecord.objects.filter(session__in=sessions, status__in=PRESENT)
        .order_by()
        .values("session__subject_id", "session__batch_id")
        .annotate(n=Count("id"), uniq=Count("student_id", distinct=True))
    )
    for m in marks:
        key = (m["session__subject_id"], m["session__batch_id"])
        if key in rows_map:
            rows_map[key]["present_marks"] = m["n"]
            rows_map[key]["students_attended"] = m["uniq"]

    # The same grouping again, narrowed to the marks a teacher entered. A
    # second query rather than a conditional aggregate so it reads the same way
    # as the one above and cannot drift from it.
    manual = (
        AttendanceRecord.objects
        .filter(session__in=sessions, status=AttendanceRecord.Status.MANUAL)
        .order_by()
        .values("session__subject_id", "session__batch_id")
        .annotate(n=Count("id"))
    )
    for m in manual:
        key = (m["session__subject_id"], m["session__batch_id"])
        if key in rows_map:
            rows_map[key]["manual_marks"] = m["n"]

    rows = []
    for row in rows_map.values():
        possible = row["classes"] * row["enrolled"]
        row["students_attended"] = row.get("students_attended", 0)
        row["percentage"] = pct(row["present_marks"], possible)
        row["manual_share"] = pct(row["manual_marks"], row["present_marks"])
        row["avg_present"] = round(row["present_marks"] / row["classes"], 1) if row["classes"] else 0
        row["teachers"] = ", ".join(sorted(row["teachers"]))
        rows.append(row)
    rows.sort(key=lambda r: (-r["classes"], r["code"]))
    return rows


# --------------------------------------------------------------------------- #
#  4. Daily trend
# --------------------------------------------------------------------------- #
def daily_trend(user, f):
    sessions = scoped_sessions(user, f)
    per_day = defaultdict(lambda: {"classes": 0, "expected": 0, "present": 0})
    for row in sessions.order_by().values("session_date", "expected_count").annotate(n=Count("id")):
        bucket = per_day[row["session_date"]]
        bucket["classes"] += row["n"]
        bucket["expected"] += row["expected_count"] * row["n"]
    marks = (
        AttendanceRecord.objects.filter(session__in=sessions, status__in=PRESENT)
        .order_by()
        .annotate(day=F("session__session_date"))
        .values("day")
        .annotate(n=Count("id"))
    )
    for m in marks:
        per_day[m["day"]]["present"] += m["n"]

    labels, values, classes, present = [], [], [], []
    for day in sorted(per_day):
        d = per_day[day]
        labels.append(day.strftime("%d %b"))
        values.append(pct(d["present"], d["expected"]))
        classes.append(d["classes"])
        present.append(d["present"])
    return {"labels": labels, "percentage": values, "classes": classes, "present": present}


def student_daily_trend(user, f, student):
    """Day-by-day 'did this student attend' series (optionally for one subject)."""
    sessions = scoped_sessions(user, f).filter(batch=student.batch)
    subject_ids = list(
        Enrollment.objects.filter(student=student, is_active=True).values_list("subject_id", flat=True)
    )
    sessions = sessions.filter(subject_id__in=subject_ids)
    if f.subject:
        sessions = sessions.filter(subject_id=f.subject)

    held = defaultdict(int)
    for row in sessions.order_by().values("session_date").annotate(n=Count("id")):
        held[row["session_date"]] = row["n"]
    attended = defaultdict(int)
    for row in (
        AttendanceRecord.objects.filter(session__in=sessions, student=student, status__in=PRESENT)
        .order_by()
        .values("session__session_date")
        .annotate(n=Count("id"))
    ):
        attended[row["session__session_date"]] = row["n"]

    labels, values, cumulative = [], [], []
    run_h = run_a = 0
    for day in sorted(held):
        run_h += held[day]
        run_a += attended.get(day, 0)
        labels.append(day.strftime("%d %b"))
        values.append(pct(attended.get(day, 0), held[day]))
        cumulative.append(pct(run_a, run_h))
    return {"labels": labels, "daily": values, "cumulative": cumulative}


# --------------------------------------------------------------------------- #
#  5. Comparisons
# --------------------------------------------------------------------------- #
def _group_percentage(sessions, group_field, label_map):
    rows = defaultdict(lambda: {"classes": 0, "expected": 0, "present": 0,
                                "manual": 0})
    for s in sessions.order_by().values(group_field, "expected_count").annotate(n=Count("id")):
        key = s[group_field]
        rows[key]["classes"] += s["n"]
        rows[key]["expected"] += s["expected_count"] * s["n"]
    for m in (
        AttendanceRecord.objects.filter(session__in=sessions, status__in=PRESENT)
        .order_by()
        .values(f"session__{group_field}")
        .annotate(n=Count("id"))
    ):
        rows[m[f"session__{group_field}"]]["present"] += m["n"]
    # The same grouping narrowed to hand-entered marks, so a batch or
    # department rollup can say how much of its attendance was typed in.
    for m in (
        AttendanceRecord.objects
        .filter(session__in=sessions, status=AttendanceRecord.Status.MANUAL)
        .order_by()
        .values(f"session__{group_field}")
        .annotate(n=Count("id"))
    ):
        rows[m[f"session__{group_field}"]]["manual"] += m["n"]
    out = []
    for key, v in rows.items():
        out.append({
            "id": key,
            "label": label_map.get(key, str(key)),
            "classes": v["classes"],
            "percentage": pct(v["present"], v["expected"]),
            "present": v["present"],
            "manual": v["manual"],
            "manual_share": pct(v["manual"], v["present"]),
        })
    out.sort(key=lambda r: -r["percentage"])
    return out


def batch_comparison(user, f):
    sessions = scoped_sessions(user, f)
    labels = {b.id: b.label for b in Batch.objects.all()}
    return _group_percentage(sessions, "batch_id", labels)


def department_comparison(user, f):
    sessions = scoped_sessions(user, f)
    labels = {d.id: d.name for d in Department.objects.all()}
    rows = defaultdict(lambda: {"classes": 0, "expected": 0, "present": 0,
                                "manual": 0})
    for s in sessions.order_by().values(
        "subject__department_id", "expected_count"
    ).annotate(n=Count("id")):
        key = s["subject__department_id"]
        rows[key]["classes"] += s["n"]
        rows[key]["expected"] += s["expected_count"] * s["n"]
    for m in (
        AttendanceRecord.objects.filter(session__in=sessions, status__in=PRESENT)
        .order_by()
        .values("session__subject__department_id")
        .annotate(n=Count("id"))
    ):
        rows[m["session__subject__department_id"]]["present"] += m["n"]
    for m in (
        AttendanceRecord.objects
        .filter(session__in=sessions, status=AttendanceRecord.Status.MANUAL)
        .order_by()
        .values("session__subject__department_id")
        .annotate(n=Count("id"))
    ):
        rows[m["session__subject__department_id"]]["manual"] += m["n"]
    out = [{
        "id": k, "label": labels.get(k, "—"), "classes": v["classes"],
        "percentage": pct(v["present"], v["expected"]), "present": v["present"],
        "manual": v["manual"], "manual_share": pct(v["manual"], v["present"]),
    } for k, v in rows.items()]
    out.sort(key=lambda r: -r["percentage"])
    return out


def teacher_activity(user, f):
    sessions = scoped_sessions(user, f).select_related("teacher")
    rows = defaultdict(lambda: {"classes": 0, "expected": 0, "present": 0,
                                "manual": 0, "name": ""})
    for s in sessions:
        r = rows[s.teacher_id]
        r["name"] = s.teacher.full_name or s.teacher.email
        r["classes"] += 1
        r["expected"] += s.expected_count
    for m in (
        AttendanceRecord.objects.filter(session__in=sessions, status__in=PRESENT)
        .order_by().values("session__teacher_id").annotate(n=Count("id"))
    ):
        rows[m["session__teacher_id"]]["present"] += m["n"]
    for m in (
        AttendanceRecord.objects
        .filter(session__in=sessions, status=AttendanceRecord.Status.MANUAL)
        .order_by().values("session__teacher_id").annotate(n=Count("id"))
    ):
        rows[m["session__teacher_id"]]["manual"] += m["n"]
    out = [{
        "id": k, "label": v["name"], "classes": v["classes"],
        "percentage": pct(v["present"], v["expected"]),
        "present": v["present"], "manual": v["manual"],
        "manual_share": pct(v["manual"], v["present"]),
    } for k, v in rows.items()]
    out.sort(key=lambda r: -r["classes"])
    return out


def hour_distribution(user, f):
    sessions = scoped_sessions(user, f)
    buckets = defaultdict(int)
    for created in sessions.values_list("created_at", flat=True):
        buckets[timezone.localtime(created).hour] += 1
    labels = [f"{h:02d}:00" for h in range(7, 21)]
    return {"labels": labels, "values": [buckets.get(h, 0) for h in range(7, 21)]}


def attendance_distribution(user, f):
    """How many students fall in each attendance band."""
    rows = student_report(user, f)
    bands = [("<40%", 0, 40), ("40–55%", 40, 55), ("55–70%", 55, 70),
             ("70–85%", 70, 85), ("85–100%", 85, 100.01)]
    counts = []
    for _label, lo, hi in bands:
        counts.append(sum(1 for r in rows if r["held"] and lo <= r["percentage"] < hi))
    return {"labels": [b[0] for b in bands], "values": counts}


def low_attendance(user, f, threshold=75):
    return [r for r in student_report(user, f) if r["held"] and r["percentage"] < threshold]


# --------------------------------------------------------------------------- #
#  6. Single-student deep dive (also powers the student's own dashboard)
# --------------------------------------------------------------------------- #
def _attachment_meta(parent):
    """
    Evidence on an absence request, as metadata only.

    Deliberately no file URL: downloads go through a view that re-checks who is
    asking, so the id is all the client needs and a copied link is worthless to
    anyone else.
    """
    if parent is None:
        return []
    return [{
        "id": a.id,
        "name": a.original_name,
        "size": a.size_label,
        "is_image": a.is_image,
    } for a in parent.attachments.all()]


def student_detail(user, f, student):
    sessions = scoped_sessions(user, f).filter(batch=student.batch)
    subject_ids = list(
        Enrollment.objects.filter(student=student, is_active=True).values_list("subject_id", flat=True)
    )
    sessions = sessions.filter(subject_id__in=subject_ids)
    s_counts = session_counts(sessions)
    p_counts = present_counts(sessions)
    m_counts = manual_counts(sessions)
    subjects = {s.id: s for s in Subject.objects.filter(id__in=subject_ids)}

    per_subject, held_total, att_total, man_total = [], 0, 0, 0
    for subject_id in subject_ids:
        classes = s_counts.get((subject_id, student.batch_id), 0)
        attended = p_counts.get((student.id, subject_id), 0)
        manual = m_counts.get((student.id, subject_id), 0)
        subj = subjects.get(subject_id)
        if subj is None:
            continue
        held_total += classes
        att_total += attended
        man_total += manual
        per_subject.append({
            "subject_id": subject_id,
            "code": subj.code,
            "name": subj.name,
            "subject_type": subj.subject_type,
            "degree": subj.degree,
            "held": classes,
            "attended": attended,
            "manual": manual,
            "missed": max(classes - attended, 0),
            "percentage": pct(attended, classes),
        })
    per_subject.sort(key=lambda r: r["percentage"])

    recent = []
    marked = {
        r.session_id: r
        # marked_by is joined because a MANUAL row now names who entered it.
        for r in AttendanceRecord.objects.filter(
            session__in=sessions, student=student).select_related("marked_by")
    }
    # One query for the whole history rather than one per absent row.
    reasons = {
        r.session_id: r
        for r in AbsenceReason.objects.filter(
            session__in=sessions, student=student
        ).select_related("reviewed_by").prefetch_related("attachments")
    }
    today = timezone.localdate()
    window_days = int(settings.ATTENDANCE.get("ABSENCE_REASON_DAYS", 3) or 0)

    session_list = list(
        sessions.select_related("subject", "teacher")
        .order_by("-session_date", "-created_at")[:60])
    # A planned absence filed in advance covers any class it spans, so the
    # student never has to explain the same absence twice.
    cover = planned_cover_for(student, session_list)

    for s in session_list:
        rec = marked.get(s.id)
        reason = reasons.get(s.id)
        planned, decision = cover.get(s.id, (None, None))
        absent = rec is None
        # The student's own view uses this to decide whether to offer the
        # "give a reason" button; the server re-checks it on submit, so this is
        # only about what the UI shows.
        # Nothing to explain if a planned absence already covers this class.
        can_explain = bool(
            absent and reason is None and decision is None and window_days > 0
            and today <= s.session_date + dt.timedelta(days=window_days)
            # Never for a guardian: explaining an absence is the student's
            # account of their own day. Cleared here rather than in the
            # template because this flag is what turns the red Absent mark
            # into a clickable "add a reason" control on three screens.
            and not getattr(user, "is_guardian", False))
        recent.append({
            "session_id": s.id,
            "date": s.session_date.strftime("%d %b %Y"),
            "time": timezone.localtime(s.created_at).strftime("%H:%M"),
            "subject": s.subject.code,
            "subject_name": s.subject.name,
            "subject_type": s.subject.subject_type,
            "degree": s.subject.degree,
            "teacher": s.teacher.full_name or s.teacher.email,
            # The record's own status, not a flattened "PRESENT". A mark a
            # teacher entered by hand reads MANUAL, and collapsing the two here
            # was hiding exactly the distinction this screen now has to show.
            "status": rec.status if rec else "ABSENT",
            "marked_by": (rec.marked_by.full_name or rec.marked_by.email)
                         if rec and rec.marked_by_id else "",
            "marked_at": timezone.localtime(rec.marked_at).strftime("%H:%M:%S") if rec else "",
            "distance": round(rec.distance_m, 1) if rec and rec.distance_m is not None else None,
            "can_explain": can_explain,
            # A per-class reason wins if one exists; otherwise fall back to the
            # planned absence covering the date. Both render identically, with
            # `reason_planned` marking which it was.
            "reason": (reason.reason if reason
                       else planned.reason if planned else ""),
            "reason_status": (reason.status if reason
                              else decision.status if decision else ""),
            "reason_status_label": (
                reason.get_status_display() if reason
                else decision.get_status_display() if decision else ""),
            "reason_remark": (reason.review_remark if reason
                              else decision.review_remark if decision else ""),
            "reason_reviewed_by": (
                (reason.reviewed_by.full_name or reason.reviewed_by.email)
                if reason and reason.reviewed_by
                else (decision.reviewed_by.full_name or decision.reviewed_by.email)
                if decision and decision.reviewed_by else ""),
            # Evidence follows whichever request supplied the reason, so a
            # class covered by a planned absence shows that request's files
            # rather than an empty list.
            "reason_attachments": _attachment_meta(reason or planned),
            "reason_planned": bool(decision and not reason),
            "reason_planned_range": (
                f"{planned.from_date:%d %b} – {planned.to_date:%d %b %Y}"
                if planned and not reason else ""),
        })

    return {
        "student": {
            "id": student.id, "name": student.name, "roll": student.class_roll,
            "exam_roll": student.exam_roll,
            "email": student.email, "batch": student.batch.label,
            "department": student.department.name,
        },
        "overall": {
            "held": held_total, "attended": att_total,
            "manual": man_total,
            "manual_share": pct(man_total, att_total),
            "missed": max(held_total - att_total, 0),
            "percentage": pct(att_total, held_total),
        },
        "subjects": per_subject,
        "trend": student_daily_trend(user, f, student),
        "recent": recent,
    }
