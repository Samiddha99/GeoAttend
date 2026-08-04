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
    department: str | None = None
    batch: str | None = None
    subject: str | None = None
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

        return cls(
            start=start, end=end,
            department=oid("department"), batch=oid("batch"), subject=oid("subject"),
            teacher=oid("teacher"), student=oid("student"), semester=num("semester"),
        )

    def as_dict(self):
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "department": self.department, "batch": self.batch, "subject": self.subject,
            "teacher": self.teacher, "student": self.student, "semester": self.semester,
        }

    @property
    def label(self):
        return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"
