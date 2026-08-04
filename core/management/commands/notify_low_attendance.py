"""
Email every student whose attendance has slipped below the threshold.

    python manage.py notify_low_attendance --threshold 75 --dry-run
Run it from cron / Task Scheduler once a week.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from academics.models import StudentProfile
from accounts.emails import send_low_attendance_alert
from dashboard.filters import ReportFilters
from dashboard.services import student_detail


class _FakeRequest:
    GET = {}


class Command(BaseCommand):
    help = "Email students who are below the attendance threshold."

    def add_arguments(self, parser):
        parser.add_argument("--threshold", type=float,
                            default=settings.ATTENDANCE["LOW_ATTENDANCE_THRESHOLD"])
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--institute", type=str, help="Institute code (default: all)")

    def handle(self, *args, **opts):
        f = ReportFilters.from_request(_FakeRequest())
        qs = StudentProfile.objects.select_related("user", "department").filter(
            is_active=True, user__is_active=True, user__registration_completed=True,
            batch__is_active=True,      # archived cohorts are never chased
        )
        if opts.get("institute"):
            qs = qs.filter(department__institute__code=opts["institute"])

        sent = 0
        for student in qs:
            data = student_detail(student.user, f, student)
            if not data["overall"]["held"]:
                continue
            if data["overall"]["percentage"] >= opts["threshold"]:
                continue
            rows = [r for r in data["subjects"] if r["held"]]
            self.stdout.write(
                f"{student.email}: {data['overall']['percentage']}% "
                f"({data['overall']['attended']}/{data['overall']['held']})"
            )
            if not opts["dry_run"]:
                send_low_attendance_alert(student.user, rows, opts["threshold"])
                sent += 1
        self.stdout.write(self.style.SUCCESS(
            f"{'Would email' if opts['dry_run'] else 'Emailed'} {sent or 0} student(s)."
        ))
