"""
Build a complete demo database: generate synthetic data as JSON, then load it.

    manage.py seed_demo --reset            # wipe tenant data, then seed
    manage.py seed_demo --generate-only    # write the JSON, touch nothing
    manage.py seed_demo --from-file x.json # load a fixture you edited
    manage.py seed_demo --institutes 8 --students 40 --weeks 12

**Two stages, deliberately.** The generator (`academics/demodata.py`) writes
JSON and knows nothing about the database; this command loads JSON and invents
nothing. When a seed comes out wrong you can tell which half was at fault by
reading the file, and a fixture that reproduces a specific bug can be kept and
replayed.

**What `--reset` deletes, and what it does not.** It removes every tenant row —
universities, institutes and everything hanging off them. It leaves superusers
alone: wiping the account you are logged in as, in the middle of setting up a
demo, is not a helpful surprise. Nothing else is spared, so this is a
development command and refuses to run with DEBUG off unless you say
`--i-mean-it`.

The password for every generated account is the same and printed at the end.
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

DEFAULT_PASSWORD = "Passw0rd!23"
DEFAULT_OUTPUT = "demo_seed.json"


class Command(BaseCommand):
    help = "Generate and load a complete synthetic demo dataset."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Delete all tenant data first.")
        parser.add_argument("--generate-only", action="store_true",
                            help="Write the JSON and stop.")
        parser.add_argument("--from-file", help="Load this JSON instead of generating.")
        parser.add_argument("--output", default=DEFAULT_OUTPUT,
                            help=f"Where to write the JSON (default {DEFAULT_OUTPUT}).")
        parser.add_argument("--seed", type=int, default=20260811)
        parser.add_argument("--institutes", type=int, default=4)
        parser.add_argument("--students", type=int, default=18,
                            help="Students per batch.")
        parser.add_argument("--weeks", type=int, default=6,
                            help="Weeks of attendance history.")
        parser.add_argument("--password", default=DEFAULT_PASSWORD)
        parser.add_argument("--i-mean-it", action="store_true",
                            help="Required to reset with DEBUG=False.")

    # ------------------------------------------------------------------ entry
    def handle(self, *args, **options):
        path = Path(options["output"])

        # Checked first, before any work. A guard that only fires after the
        # generator has run is a guard that lets you think the reset happened.
        if options["reset"] and not settings.DEBUG and not options["i_mean_it"]:
            raise CommandError(
                "Refusing to reset with DEBUG=False. Pass --i-mean-it if this "
                "really is a throwaway database.")
        if options["reset"] and options["generate_only"]:
            raise CommandError(
                "--reset and --generate-only contradict each other: nothing "
                "would be deleted and nothing would be loaded. Drop one.")

        if options["from_file"]:
            data = json.loads(Path(options["from_file"]).read_text("utf-8"))
            self.stdout.write(f"Loaded fixture from {options['from_file']}")
        else:
            from academics.demodata import Generator

            data = Generator(seed=options["seed"],
                             institutes=options["institutes"],
                             students_per_batch=options["students"],
                             weeks=options["weeks"]).build()
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))
            for name, n in sorted(data["meta"]["counts"].items()):
                self.stdout.write(f"    {n:>6}  {name}")

        if options["generate_only"]:
            self.stdout.write("--generate-only: nothing was written to the database.")
            return

        if options["reset"]:
            self._reset(options["i_mean_it"])

        self._load(data, options["password"])

    # ------------------------------------------------------------------ reset
    def _reset(self, confirmed):
        """
        Delete every tenant row.

        Ordered children-first rather than relying on cascades: several FKs here
        are `PROTECT` (an affiliation protects its university), and MongoDB has
        no transactional DDL to fall back on if a delete half-runs.

        The DEBUG guard is in `handle`, not here — it has to fire before the
        generator runs, or the command does several seconds of work and only
        then admits it was never going to reset anything.
        """
        from academics.models import (
            Batch, Department, Enrollment, ImportJob, StudentProfile, Subject,
            TeacherAssignment,
        )
        from accounts.models import (
            ActivityLog, EmailOTP, FaceEnrolment, FaceSample, Institute,
            InstituteAffiliation, Invitation, PhoneOTP, University,
            UniversityDiscipline, User,
        )
        from attendance.models import (
            AbsenceAttachment, AbsenceReason, AttendanceRecord,
            AttendanceSession, FaceVerifyTicket, ManualMarkRequest, MarkAttempt,
            PlannedAbsence, PlannedAbsenceDecision,
        )
        from feedback.models import (
            FeedbackForm, FeedbackRecipient, FeedbackResponse,
        )
        from notifications.models import (
            AlertCampaign, AlertDelivery, WhatsAppTemplate,
        )

        order = [
            FeedbackResponse, FeedbackRecipient, FeedbackForm,
            AlertDelivery, AlertCampaign, WhatsAppTemplate,
            AbsenceAttachment, PlannedAbsenceDecision, PlannedAbsence,
            AbsenceReason, ManualMarkRequest, FaceVerifyTicket, MarkAttempt,
            AttendanceRecord, AttendanceSession,
            ImportJob, Enrollment, TeacherAssignment, StudentProfile,
            Subject, Batch,
            FaceSample, FaceEnrolment, Invitation, EmailOTP, PhoneOTP,
            ActivityLog,
        ]
        for model in order:
            deleted = model.objects.all().delete()
            self._say_deleted(model, deleted)

        # A department points at its HoD and the HoD points back; clear one side
        # before deleting either, or the delete trips over its own constraint.
        Department.objects.update(hod=None)
        self._say_deleted(User, User.objects.filter(is_superuser=False).delete())
        self._say_deleted(Department, Department.objects.all().delete())
        self._say_deleted(InstituteAffiliation,
                          InstituteAffiliation.objects.all().delete())
        self._say_deleted(Institute, Institute.objects.all().delete())
        self._say_deleted(UniversityDiscipline,
                          UniversityDiscipline.objects.all().delete())
        self._say_deleted(University, University.objects.all().delete())

        self.stdout.write(self.style.WARNING(
            "Reset complete. Superusers were left alone."))

    def _say_deleted(self, model, result):
        total = result[0] if isinstance(result, tuple) else 0
        if total:
            self.stdout.write(f"    - {total:>6}  {model.__name__}")

    # ------------------------------------------------------------------- load
    @transaction.atomic
    def _load(self, data, password):
        """
        Walk the fixture in dependency order, keeping a map from its string
        keys to the rows created for them.
        """
        from academics.models import (
            Batch, Department, Enrollment, StudentProfile, Subject,
            TeacherAssignment,
        )
        from accounts.models import (
            Institute, InstituteAffiliation, University, UniversityDiscipline,
            User,
        )
        from attendance.models import (
            AbsenceReason, AttendanceRecord, AttendanceSession, PlannedAbsence,
            PlannedAbsenceDecision,
        )

        now = timezone.now()
        ref = {}

        for row in data["universities"]:
            university = University.objects.create(
                name=row["name"], short_name=row["short_name"], code=row["code"],
                email=row["email"], grants_affiliation=row["grants_affiliation"],
                claimed_at=now)
            ref[row["key"]] = university
            for discipline in row["disciplines"]:
                UniversityDiscipline.objects.create(
                    university=university, discipline=discipline)
            admin = row["admin"]
            User.objects.create_user(
                email=admin["email"], password=password,
                full_name=admin["full_name"], phone=admin["phone"],
                role=User.Role.UNIVERSITY, university=university,
                email_verified=True, registration_completed=True)

        for row in data["institutes"]:
            ref[row["key"]] = Institute.objects.create(
                name=row["name"], code=row["code"], email=row["email"],
                phone=row["phone"], website=row["website"],
                address=row["address"], state=row["state"],
                district=row["district"], status=row["status"],
                rejection_reason=row["rejection_reason"],
                decided_at=now if row["status"] != "PENDING" else None)

        for row in data["affiliations"]:
            InstituteAffiliation.objects.create(
                institute=ref[row["institute"]], discipline=row["discipline"],
                university=ref[row["university"]] if row["university"] else None)

        # **Nothing here writes `is_revoked`.** The affiliations are loaded
        # first, and each model derives the flag on save from the disciplines
        # its institute holds — see core/enums.py and
        # academics.curriculum.sync_revoked. A seeder that wrote the flag
        # itself would be a second opinion about the same fact, and the two
        # would disagree the first time either changed.
        for row in data["departments"]:
            ref[row["key"]] = Department.objects.create(
                institute=ref[row["institute"]], name=row["name"],
                code=row["code"], discipline=row["discipline"],
                status=row["status"],
                is_active=row["status"] != "ARCHIVED")

        for row in data["batches"]:
            ref[row["key"]] = Batch.objects.create(
                department=ref[row["department"]], label=row["label"],
                start_year=row["start_year"], end_year=row["end_year"],
                status=row["status"],
                is_active=row["status"] != "ARCHIVED")

        for row in data["subjects"]:
            ref[row["key"]] = Subject.objects.create(
                department=ref[row["department"]], code=row["code"],
                name=row["name"], subject_type=row["subject_type"],
                degree=row["degree"], semester=row["semester"],
                credits=row["credits"], status=row["status"],
                is_active=row["status"] != "ARCHIVED",
                owner_university=(ref[row["owner_university"]]
                                  if row.get("owner_university") else None))

        heads = []
        for row in data["users"]:
            user = User.objects.create_user(
                email=row["email"], password=password,
                full_name=row["full_name"], phone=row["phone"],
                role=row["role"], institute=ref[row["institute"]],
                department=ref[row["department"]] if row["department"] else None,
                # `User.save` derives status from these two, so passing both
                # keeps one writer rather than two — see accounts.models.User.
                is_active=row["status"] != "ARCHIVED", email_verified=True,
                registration_completed=row["registration_completed"])
            ref[row["key"]] = user
            if row.get("heads_department"):
                heads.append((row["heads_department"], user))
        for department_key, user in heads:
            department = ref[department_key]
            department.hod = user
            department.save(update_fields=["hod"])

        for row in data["students"]:
            ref[row["key"]] = StudentProfile.objects.create(
                user=ref[row["user"]], department=ref[row["department"]],
                batch=ref[row["batch"]], class_roll=row["class_roll"],
                exam_roll=row["exam_roll"], mobile=row["mobile"],
                guardian_name=row["guardian_name"],
                guardian_mobile=row["guardian_mobile"],
                guardian_email=row["guardian_email"],
                status=row["status"],
                is_active=row["status"] != "ARCHIVED")

        Enrollment.objects.bulk_create([
            Enrollment(student=ref[r["student"]], subject=ref[r["subject"]])
            for r in data["enrolments"]])
        TeacherAssignment.objects.bulk_create([
            TeacherAssignment(teacher=ref[r["teacher"]], subject=ref[r["subject"]],
                              batch=ref[r["batch"]])
            for r in data["assignments"]])

        from datetime import date as _date, timedelta

        for row in data["sessions"]:
            session_date = _date.fromisoformat(row["session_date"])
            session = AttendanceSession.objects.create(
                teacher=ref[row["teacher"]], subject=ref[row["subject"]],
                batch=ref[row["batch"]], session_date=session_date,
                latitude=row["latitude"], longitude=row["longitude"],
                radius_m=row["radius_m"], status=row["status"],
                expected_count=row["expected_count"], note=row["note"],
                # A closed session in the past: the window is behind us, which
                # is what every historical statistic assumes.
                expires_at=now - timedelta(days=1),
                closed_at=now - timedelta(days=1))
            ref[row["key"]] = session

        AttendanceRecord.objects.bulk_create([
            AttendanceRecord(
                session=ref[r["session"]], student=ref[r["student"]],
                status=r["status"],
                marked_by=ref[r["marked_by"]] if r.get("marked_by") else None)
            for r in data["records"]])

        for row in data["absence_reasons"]:
            session = ref[row["session"]]
            reviewer = session.teacher if row["status"] != "PENDING" else None
            AbsenceReason.objects.create(
                session=session, student=ref[row["student"]],
                reason=row["reason"], status=row["status"],
                reviewed_by=reviewer,
                reviewed_at=now if reviewer else None,
                review_remark=row["review_remark"])

        for row in data["planned_absences"]:
            planned = PlannedAbsence.objects.create(
                student=ref[row["student"]],
                from_date=_date.fromisoformat(row["from_date"]),
                to_date=_date.fromisoformat(row["to_date"]),
                reason=row["reason"], all_subjects=row["all_subjects"],
                cancelled_at=now if row.get("cancelled") else None)
            for decision in row.get("decisions", []):
                subject = ref[decision["subject"]]
                PlannedAbsenceDecision.objects.create(
                    planned=planned, subject=subject,
                    status=decision["status"],
                    reviewed_at=now if decision["status"] != "PENDING" else None,
                    review_remark=decision["review_remark"])

        self._report(data, password)

    # ----------------------------------------------------------------- report
    def _report(self, data, password):
        from accounts.models import User

        self.stdout.write(self.style.SUCCESS("\nLoaded."))
        for name, n in sorted(data["meta"]["counts"].items()):
            self.stdout.write(f"    {n:>6}  {name}")

        self.stdout.write("\nSign in with any of these — password below:")
        for row in data["universities"][:2]:
            self.stdout.write(f"    university  {row['admin']['email']}")
        for row in data["users"]:
            if row["role"] == "HEAD":
                self.stdout.write(f"    head        {row['email']}")
        first_hod = next((r for r in data["users"] if r["role"] == "HOD"), None)
        first_teacher = next((r for r in data["users"]
                              if r["role"] == "TEACHER"
                              and r["registration_completed"]), None)
        first_student = next((r for r in data["users"]
                              if r["role"] == "STUDENT"
                              and r["registration_completed"]), None)
        for label, row in (("hod", first_hod), ("teacher", first_teacher),
                           ("student", first_student)):
            if row:
                self.stdout.write(f"    {label:<11} {row['email']}")

        self.stdout.write(self.style.WARNING(
            f"\n  Every account above shares the password '{password}'.\n"
            f"  {User.objects.count()} accounts in total. This is demo data — "
            "never load it anywhere real."))
