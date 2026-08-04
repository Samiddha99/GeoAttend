"""Housekeeping: flip expired-but-still-OPEN sessions to CLOSED."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from attendance.models import AttendanceSession


class Command(BaseCommand):
    help = "Close attendance sessions whose expiry time has passed."

    def handle(self, *args, **opts):
        now = timezone.now()
        n = AttendanceSession.objects.filter(
            status=AttendanceSession.Status.OPEN, expires_at__lte=now
        ).update(status=AttendanceSession.Status.CLOSED, closed_at=now)
        self.stdout.write(self.style.SUCCESS(f"Closed {n} expired session(s)."))
