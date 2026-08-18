"""
The demo seeder: does it generate what it claims, and does it load?

The point of a demo dataset is the awkward states — a rejected institute with a
reason, a revoked department, an absence rejected with a remark. Those are the
ones nobody sets up by hand, so they are the ones that go untested until a
screen renders wrong in front of somebody. Each is asserted here.

The generator is tested without a database at all, which is the whole reason it
is a separate module: it is a pure function from a seed to a dict.
"""
import io
import json

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from academics.demodata import Generator
from academics.models import Batch, Department, StudentProfile, Subject
from accounts.models import Institute, InstituteAffiliation, University, User
from attendance.models import (
    AbsenceReason,
    AttendanceRecord,
    AttendanceSession,
    PlannedAbsence,
    PlannedAbsenceDecision,
)


def small(**kwargs):
    options = {"seed": 4242, "institutes": 4, "students_per_batch": 3, "weeks": 2}
    options.update(kwargs)
    return Generator(**options).build()


class GeneratorTests(TestCase):
    """No database involved — the generator is a pure function."""

    def setUp(self):
        self.data = small()

    def test_the_same_seed_gives_byte_identical_output(self):
        """
        A demo database that differs run to run is one you cannot describe to
        anyone else, or reproduce a bug in.
        """
        self.assertEqual(json.dumps(small(), sort_keys=True),
                         json.dumps(small(), sort_keys=True))

    def test_a_different_seed_gives_different_output(self):
        self.assertNotEqual(json.dumps(small(), sort_keys=True),
                            json.dumps(small(seed=99), sort_keys=True))

    def test_every_login_is_unique(self):
        """
        One duplicate anywhere and the load dies half way through, leaving a
        database that is neither empty nor seeded.
        """
        emails = [u["email"] for u in self.data["users"]]
        emails += [u["admin"]["email"] for u in self.data["universities"]]
        self.assertEqual(len(emails), len(set(emails)))

    def test_every_reference_points_at_something_that_exists(self):
        """
        The JSON has to load into an *empty* database, so every key it mentions
        must be defined in the same file.
        """
        keys = {row["key"] for group in ("universities", "institutes",
                                         "departments", "batches", "subjects",
                                         "users", "students", "sessions")
                for row in self.data[group]}
        for group, fields in (
                ("departments", ["institute"]),
                ("batches", ["department"]),
                ("subjects", ["department"]),
                ("students", ["user", "department", "batch"]),
                ("enrolments", ["student", "subject"]),
                ("assignments", ["teacher", "subject", "batch"]),
                ("sessions", ["teacher", "subject", "batch"]),
                ("records", ["session", "student"]),
                ("absence_reasons", ["session", "student"]),
        ):
            for row in self.data[group]:
                for field in fields:
                    with self.subTest(group=group, field=field):
                        self.assertIn(row[field], keys)

    def test_no_absence_reason_is_recorded_twice_for_one_student(self):
        """
        A unique constraint the first version tripped: `build_absences` runs per
        institute and was picking from every institute's records each time.
        """
        pairs = [(r["session"], r["student"])
                 for r in self.data["absence_reasons"]]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_no_department_name_repeats_inside_one_institute(self):
        """Another constraint the first version tripped, for the same reason."""
        seen = set()
        for row in self.data["departments"]:
            key = (row["institute"], row["name"])
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_every_department_discipline_is_deliberate(self):
        """
        A department's discipline is either one the institute holds, or the one
        deliberately left unaffiliated to demonstrate `revoked`. Anything else
        is an accident — and three accidental revoked departments is exactly
        what the first version produced.
        """
        held = {}
        for row in self.data["affiliations"]:
            held.setdefault(row["institute"], set()).add(row["discipline"])
        stray = [d for d in self.data["departments"]
                 if d["discipline"] not in held.get(d["institute"], set())]
        self.assertEqual(len(stray), 1, stray)
        self.assertEqual(stray[0]["discipline"], "AGRI")

    def test_at_least_two_institutes_are_approved(self):
        """
        One would leave every cross-institute screen with nothing to compare,
        which is what those screens are for.
        """
        approved = [i for i in self.data["institutes"]
                    if i["status"] == "APPROVED"]
        self.assertGreaterEqual(len(approved), 2)

    def test_the_awkward_states_are_all_present(self):
        statuses = {i["status"] for i in self.data["institutes"]}
        self.assertEqual(statuses, {"APPROVED", "PENDING", "REJECTED"})
        rejected = [i for i in self.data["institutes"]
                    if i["status"] == "REJECTED"]
        self.assertTrue(all(i["rejection_reason"] for i in rejected))

        self.assertTrue(any(a["university"] for a in self.data["affiliations"]))
        self.assertTrue(any(a["university"] is None
                            for a in self.data["affiliations"]))
        self.assertTrue(any(d["status"] == "ARCHIVED"
                            for d in self.data["departments"]))
        self.assertTrue(any(b["status"] == "ARCHIVED"
                            for b in self.data["batches"]))
        self.assertTrue(any(s.get("owner_university")
                            for s in self.data["subjects"]))
        self.assertTrue(any(not u["registration_completed"]
                            for u in self.data["users"]))

        self.assertEqual(
            {r["status"] for r in self.data["records"]},
            {"PRESENT", "ABSENT", "MANUAL"})
        self.assertEqual(
            {r["status"] for r in self.data["absence_reasons"]},
            {"APPROVED", "REJECTED", "PENDING"})
        self.assertTrue(all(r["review_remark"] for r in self.data["absence_reasons"]
                            if r["status"] == "REJECTED"))
        self.assertTrue(any(p["cancelled"] for p in self.data["planned_absences"]))

    def test_every_status_value_appears_on_people_and_on_rows(self):
        """
        A status filter with an empty position is a filter nobody can check.
        """
        self.assertEqual({u["status"] for u in self.data["users"]},
                         {"ACTIVE", "INVITED", "ARCHIVED"})
        self.assertEqual({s["status"] for s in self.data["students"]},
                         {"ACTIVE", "INVITED", "ARCHIVED"})
        self.assertEqual({s["status"] for s in self.data["subjects"]},
                         {"ACTIVE", "ARCHIVED"})

    def test_a_student_profile_agrees_with_its_account(self):
        """
        Otherwise the status column and the status filter disagree on screen —
        one reads the profile, the other the user.
        """
        by_key = {u["key"]: u for u in self.data["users"]}
        for student in self.data["students"]:
            with self.subTest(student=student["key"]):
                self.assertEqual(student["status"],
                                 by_key[student["user"]]["status"])

    def test_the_fixture_never_writes_the_revoked_flag(self):
        """
        Revocation is derived from the affiliations on save. A seeder that also
        wrote it would be a second opinion about one fact, and the two would
        disagree the first time either changed.
        """
        for group in ("departments", "subjects", "batches", "students", "users"):
            for row in self.data[group]:
                self.assertNotIn("is_revoked", row, group)

    def test_only_approved_institutes_have_attendance(self):
        """
        A college nobody has approved has not started teaching, so showing it a
        term of history would be a state the app cannot reach.

        Walked through the fixture's own maps rather than by picking the key
        apart — the first attempt parsed "dept-inst-1-CSE" by index and was
        asserting nonsense that happened to pass on some rows.
        """
        approved = {i["key"] for i in self.data["institutes"]
                    if i["status"] == "APPROVED"}
        institute_of_department = {d["key"]: d["institute"]
                                   for d in self.data["departments"]}
        department_of_subject = {s["key"]: s["department"]
                                 for s in self.data["subjects"]}
        self.assertTrue(self.data["sessions"])
        for session in self.data["sessions"]:
            department = department_of_subject[session["subject"]]
            self.assertIn(institute_of_department[department], approved)


class LoadTests(TestCase):
    """The other half: does the JSON go into a database."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", "--institutes", "4", "--students", "3",
                     "--weeks", "2", "--seed", "4242",
                     "--output", "/tmp/test_demo_seed.json",
                     stdout=io.StringIO(), stderr=io.StringIO())

    def test_it_creates_rows_in_every_table_it_claims_to(self):
        for model in (University, Institute, InstituteAffiliation, Department,
                      Batch, Subject, StudentProfile, AttendanceSession,
                      AttendanceRecord, AbsenceReason, PlannedAbsence,
                      PlannedAbsenceDecision):
            with self.subTest(model=model.__name__):
                self.assertGreater(model.objects.count(), 0)

    def test_every_department_has_a_head_of_department(self):
        self.assertFalse(Department.objects.filter(hod=None).exists())

    def test_the_loaded_states_match_what_was_generated(self):
        self.assertEqual(
            set(Institute.objects.values_list("status", flat=True)),
            {"APPROVED", "PENDING", "REJECTED"})
        self.assertEqual(
            set(AttendanceRecord.objects.values_list("status", flat=True)),
            {"PRESENT", "ABSENT", "MANUAL"})
        self.assertEqual(
            set(AbsenceReason.objects.values_list("status", flat=True)),
            {"APPROVED", "REJECTED", "PENDING"})

    def test_a_reviewed_absence_has_a_reviewer_and_a_pending_one_does_not(self):
        self.assertFalse(
            AbsenceReason.objects.exclude(status="PENDING")
            .filter(reviewed_by=None).exists())
        self.assertFalse(
            AbsenceReason.objects.filter(status="PENDING")
            .exclude(reviewed_by=None).exists())

    def test_a_revoked_department_is_seeded_full_of_active_students(self):
        """
        The case that exposed the whole status/revocation split, on tap.

        A department nobody affiliates, holding students who are perfectly
        active. Before the split this reported 0 students; now the demo has a
        standing example of it reporting the truth.
        """
        from core.enums import RowStatus

        revoked = Department.objects.filter(is_revoked=True)
        self.assertTrue(revoked.exists())
        department = revoked.first()
        self.assertEqual(department.status, RowStatus.ACTIVE)
        students = StudentProfile.objects.filter(department=department)
        self.assertGreater(students.count(), 0)
        self.assertGreater(
            students.filter(status=RowStatus.ACTIVE).count(), 0)
        # And every one of them carries the flag, inherited on save.
        self.assertTrue(all(s.is_revoked for s in students))

    def test_the_loaded_rows_carry_both_fields_independently(self):
        from core.enums import RowStatus

        self.assertTrue(
            StudentProfile.objects.filter(
                is_revoked=True, status=RowStatus.ACTIVE).exists())
        self.assertTrue(
            StudentProfile.objects.filter(
                is_revoked=False, status=RowStatus.ACTIVE).exists())
        self.assertTrue(
            User.objects.filter(status=RowStatus.INVITED).exists())
        self.assertTrue(
            User.objects.filter(status=RowStatus.ARCHIVED).exists())

    def test_exactly_one_department_reads_as_revoked(self):
        self.assertEqual(Department.objects.filter(is_revoked=True).count(), 1)
        self.assertGreaterEqual(
            Department.objects.filter(status="ARCHIVED").count(), 1)

    def test_manual_marks_exist_so_the_dashboards_are_not_all_zero(self):
        """
        Every "how much of this was typed in" figure reads MANUAL. A demo
        without any shows those as zero, which looks like a broken feature.
        """
        self.assertGreater(
            AttendanceRecord.objects.filter(status="MANUAL").count(), 0)

    def test_every_generated_account_signs_in_with_the_shared_password(self):
        user = User.objects.filter(role=User.Role.HEAD).first()
        self.assertIsNotNone(user)
        self.assertTrue(user.check_password("Passw0rd!23"))

    @override_settings(DEBUG=True)
    def test_running_it_again_replaces_rather_than_duplicates(self):
        # DEBUG=True because the guard refuses a reset without it, and the test
        # harness runs with DEBUG off — which is the guard working, not a
        # problem to route around in the command.
        before = Institute.objects.count()
        call_command("seed_demo", "--reset", "--institutes", "4",
                     "--students", "3", "--weeks", "2", "--seed", "4242",
                     "--output", "/tmp/test_demo_seed.json",
                     stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(Institute.objects.count(), before)

    @override_settings(DEBUG=True)
    def test_reset_keeps_superusers(self):
        """Wiping the account you are signed in as is not a helpful surprise."""
        User.objects.create_superuser(email="root@x.com", password="Str0ngPass!23")
        call_command("seed_demo", "--reset", "--institutes", "4",
                     "--students", "3", "--weeks", "2",
                     "--output", "/tmp/test_demo_seed.json",
                     stdout=io.StringIO(), stderr=io.StringIO())
        self.assertTrue(User.objects.filter(email="root@x.com").exists())


class GuardTests(TestCase):
    def test_generate_only_writes_nothing_to_the_database(self):
        call_command("seed_demo", "--generate-only", "--institutes", "4",
                     "--output", "/tmp/test_demo_only.json",
                     stdout=io.StringIO())
        self.assertEqual(Institute.objects.count(), 0)

    def test_reset_and_generate_only_together_are_refused(self):
        """They contradict: nothing deleted, nothing loaded."""
        with self.assertRaises(CommandError):
            call_command("seed_demo", "--reset", "--generate-only",
                         stdout=io.StringIO())

    @override_settings(DEBUG=False)
    def test_reset_is_refused_in_production_unless_forced(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo", "--reset", stdout=io.StringIO())

    def test_a_fixture_can_be_loaded_from_a_file(self):
        """The reason the JSON exists: an edited fixture replays a bug."""
        path = "/tmp/test_demo_fixture.json"
        call_command("seed_demo", "--generate-only", "--institutes", "4",
                     "--students", "2", "--weeks", "1", "--output", path,
                     stdout=io.StringIO())
        call_command("seed_demo", "--from-file", path,
                     stdout=io.StringIO(), stderr=io.StringIO())
        self.assertGreater(Institute.objects.count(), 0)
