"""Parse the query-string filters every report shares."""
import datetime as dt
from dataclasses import dataclass, field

from core.utils import clean_object_id, default_date_range, parse_date


@dataclass
class ReportFilters:
    # The five id filters are Mongo primary keys — 24-char hex strings, not
    # integers. `semester` is the only genuine number here.
    start: dt.date
    end: dt.date
    # Only a university ever sets this — everyone else already has exactly one
    # institute, so it is a no-op for them.
    institute: str | None = None
    department: str | None = None
    batch: str | None = None
    subject: str | None = None
    subject_type: str | None = None
    degree: str | None = None
    # The department's discipline, so a report can be narrowed to one wing of
    # a college without naming each of its departments.
    discipline: str | None = None
    teacher: str | None = None
    student: str | None = None
    semester: int | None = None
    extras: dict = field(default_factory=dict)

    @classmethod
    def from_request(cls, request):
        d_start, d_end = default_date_range()
        start = parse_date(request.GET.get("start"), d_start)
        end = parse_date(request.GET.get("end"), d_end)
        if start > end:
            start, end = end, start

        def num(key):
            """A real integer filter, e.g. semester."""
            raw = request.GET.get(key)
            try:
                return int(raw) if raw not in (None, "", "all") else None
            except ValueError:
                return None

        def oid(key):
            """
            An id filter.

            This used to run through num(), which meant int("6a6cf0…") raised
            ValueError, got swallowed, and returned None — so the filter was
            silently ignored and every report quietly showed unfiltered data.
            Passing the hex through keeps the filter working; clean_object_id
            still discards junk so a bad query string cannot 500 the page.
            """
            raw = request.GET.get(key)
            if raw in (None, "", "all"):
                return None
            return clean_object_id(raw)

        def choice(key, allowed):
            """
            A filter whose values are a fixed vocabulary.

            Anything unrecognised becomes None — an unfiltered report — rather
            than a filter that matches nothing. A typo in a bookmarked URL
            should not produce an empty page that looks like real data.
            """
            raw = (request.GET.get(key) or "").strip().upper()
            return raw if raw in allowed else None

        from academics.models import Degree, SubjectType
        from accounts.models import Discipline

        return cls(
            start=start, end=end,
            institute=oid("institute"),
            department=oid("department"), batch=oid("batch"), subject=oid("subject"),
            subject_type=choice("subject_type", set(SubjectType.values)),
            degree=choice("degree", set(Degree.values)),
            discipline=choice("discipline", set(Discipline.values)),
            teacher=oid("teacher"), student=oid("student"), semester=num("semester"),
        )

    def as_dict(self):
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "institute": self.institute,
            "department": self.department, "batch": self.batch, "subject": self.subject,
            "subject_type": self.subject_type,
            "degree": self.degree,
            "discipline": self.discipline,
            "teacher": self.teacher, "student": self.student, "semester": self.semester,
        }

    @property
    def label(self):
        return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"
