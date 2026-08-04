"""
Housekeeping for stale invitation links.

Expiry alone never deletes anything — an expired link simply stops working.
This command does the actual clean-up:

  1. Flags PENDING invitations whose expiry has passed as EXPIRED (the app only
     does this lazily, when somebody actually opens a dead link).
  2. Deletes invitations that expired more than --days ago.
  3. Optionally removes the never-activated user accounts left behind, but only
     when they hold no academic data at all.

    python manage.py prune_invitations --days 30 --dry-run
    python manage.py prune_invitations --days 30
    python manage.py prune_invitations --days 90 --include-revoked --purge-users --yes

ACCEPTED invitations are never touched: they are the record of who joined and when.
Safe to run from cron / Windows Task Scheduler.
"""
import datetime as dt
import sys

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from academics.models import Department
from accounts.models import EmailOTP, Invitation, User

KEEP = Invitation.Status.ACCEPTED


class Command(BaseCommand):
    help = "Delete invitation links that expired more than N days ago."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=30,
            help="Delete invitations that expired more than this many days ago (default: 30).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would be removed without touching the database.",
        )
        parser.add_argument(
            "--institute", type=str,
            help="Limit to one institute code, e.g. --institute DEMO.",
        )
        parser.add_argument(
            "--include-revoked", action="store_true",
            help="Also delete invitations that were manually revoked.",
        )
        parser.add_argument(
            "--purge-users", action="store_true",
            help="Also delete the never-activated accounts left behind (guarded).",
        )
        parser.add_argument(
            "--otps", action="store_true",
            help="Also delete EmailOTP rows older than the cutoff.",
        )
        parser.add_argument(
            "--yes", action="store_true",
            help="Skip the confirmation prompt for --purge-users.",
        )

    # ------------------------------------------------------------------ #
    def handle(self, *args, **o):
        days = o["days"]
        if days < 0:
            raise CommandError("--days cannot be negative.")
        dry = o["dry_run"]
        now = timezone.now()
        cutoff = now - dt.timedelta(days=days)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nPruning invitations that expired before {timezone.localtime(cutoff):%d %b %Y, %H:%M}"
            + (f"  ·  institute={o['institute']}" if o.get("institute") else "")
            + ("  ·  DRY RUN" if dry else "")
        ))

        scope = Invitation.objects.all()
        if o.get("institute"):
            scope = scope.filter(institute__code__iexact=o["institute"])
            if not scope.exists():
                raise CommandError(f"No invitations found for institute '{o['institute']}'.")

        # -- 1. flag stale PENDING rows ---------------------------------- #
        stale = scope.filter(status=Invitation.Status.PENDING, expires_at__lt=now)
        stale_count = stale.count()
        if stale_count and not dry:
            stale.update(status=Invitation.Status.EXPIRED)
        self.stdout.write(
            f"  {'would flag' if dry else 'flagged':>12}  {stale_count:>5}  "
            f"pending → expired"
        )

        # -- 2. select the doomed invitations ---------------------------- #
        doomed = scope.filter(expires_at__lt=cutoff).exclude(status=KEEP)
        if not o["include_revoked"]:
            doomed = doomed.exclude(status=Invitation.Status.REVOKED)

        doomed_ids = list(doomed.values_list("id", flat=True))
        emails = sorted(set(doomed.values_list("email", flat=True)))
        by_role = {}
        for role in doomed.values_list("role", flat=True):
            by_role[role] = by_role.get(role, 0) + 1

        if not doomed_ids:
            self.stdout.write(self.style.SUCCESS(
                "\n  Nothing to prune — no invitation has been expired that long.\n"
            ))
            return

        self.stdout.write(
            f"  {'would delete' if dry else 'deleting':>12}  {len(doomed_ids):>5}  invitation(s)  "
            + ", ".join(f"{n}×{r}" for r, n in sorted(by_role.items()))
        )
        if o["verbosity"] >= 2:
            for inv in doomed.order_by("expires_at")[:200]:
                age = (now - inv.expires_at).days
                self.stdout.write(
                    f"        · {inv.email:<38} {inv.role:<8} {inv.status:<8} expired {age}d ago"
                )

        # -- 3. work out which orphan users may go ----------------------- #
        purge, skipped = [], []
        if o["purge_users"]:
            candidates = User.objects.filter(
                email__in=emails, registration_completed=False
            ).exclude(is_superuser=True)
            for user in candidates:
                reason = self._blocking_reason(user, doomed_ids)
                (skipped.append((user, reason)) if reason else purge.append(user))

            self.stdout.write(
                f"  {'would delete' if dry else 'deleting':>12}  {len(purge):>5}  "
                f"never-activated account(s)"
            )
            for user, reason in skipped:
                self.stdout.write(self.style.WARNING(
                    f"        · keeping {user.email} — {reason}"
                ))

        otp_qs = EmailOTP.objects.filter(expires_at__lt=cutoff)
        if o["otps"]:
            self.stdout.write(
                f"  {'would delete' if dry else 'deleting':>12}  {otp_qs.count():>5}  expired OTP code(s)"
            )

        if dry:
            self.stdout.write(self.style.WARNING(
                "\n  Dry run — nothing was written. Re-run without --dry-run to apply.\n"
            ))
            return

        if purge and not o["yes"] and sys.stdin.isatty():
            answer = input(f"\n  Permanently delete {len(purge)} user account(s)? [y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                self.stdout.write(self.style.WARNING("  Aborted — nothing was deleted."))
                return

        # -- 4. apply ----------------------------------------------------- #
        with transaction.atomic():
            # Users first. `Invitation.user` is only populated on acceptance, so a
            # pending row survives its user's deletion and still needs removing below.
            purged = 0
            for user in purge:
                user.delete()
                purged += 1
            removed, _ = Invitation.objects.filter(id__in=doomed_ids).delete()
            otps = otp_qs.delete()[0] if o["otps"] else 0

        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. Removed {removed} invitation row(s)"
            + (f", {purged} user account(s)" if o["purge_users"] else "")
            + (f", {otps} OTP code(s)" if o["otps"] else "")
            + f". Flagged {stale_count} as expired.\n"
        ))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _blocking_reason(user, doomed_ids):
        """
        Return why this account must survive, or None if it is safe to delete.

        Deleting a User cascades into StudentProfile → Enrollment →
        AttendanceRecord, into AttendanceSession.teacher and into
        TeacherAssignment.  We refuse to delete anyone holding any of it.
        """
        profile = getattr(user, "student_profile", None)
        if profile is not None:
            if profile.attendance_records.exists():
                return "has attendance records"
            if profile.enrollments.exists():
                return "still enrolled in subjects — re-invite instead"
        if user.sessions_created.exists():
            return "has conducted classes"
        if user.assignments.exists():
            return "still holds subject allocations — re-invite instead"
        if Department.objects.filter(hod=user).exists():
            return "is listed as a department HoD"
        if Invitation.objects.filter(email=user.email).exclude(id__in=doomed_ids).exists():
            return "has a newer invitation pending"
        return None
