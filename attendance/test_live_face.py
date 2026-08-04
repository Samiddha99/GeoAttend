"""
Live face verification: the rules, without a socket or a model.

The consumer is a pump — it moves frames one way and verdicts the other. Every
decision it appears to make actually lives in `live.py` or in the matcher, and
that is what is tested here: what a ticket authorises, what happens when two
frames match at once, and the one thing that must never be true — that a client
can talk its way to a present mark.
"""
import datetime as dt
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from accounts.models import Institute, User
from attendance import live


class AntiSpoofGateTests(SimpleTestCase):
    """
    Whether face marking may run at all without a liveness model.

    The default is to refuse, and that is the important part. A photograph of
    the student satisfies detection, embedding and the match threshold — every
    other test in the pipeline. Running without liveness and saying nothing
    would leave staff believing a check had happened that never had.
    """

    def _conf(self, **over):
        from django.conf import settings
        return {**settings.FACE, **over}

    def test_required_but_missing_blocks_marking(self):
        with override_settings(FACE=self._conf(
                ANTISPOOF_REQUIRED=True, ANTISPOOF_MODEL="")):
            self.assertFalse(live.antispoof_ready())

    def test_a_path_pointing_at_nothing_is_not_ready(self):
        """
        Regression. This only checked that a path was *configured*, so a
        setting pointing at a file that did not exist sailed through and blew
        up per frame inside the matcher — where the student saw "could not read
        that frame" forever and the log filled with tracebacks.
        """
        with override_settings(FACE=self._conf(
                ANTISPOOF_REQUIRED=True,
                ANTISPOOF_MODEL="/code/models/does-not-exist.onnx")):
            self.assertFalse(live.antispoof_ready())

    def test_a_file_that_exists_is_ready(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".onnx") as handle:
            with override_settings(FACE=self._conf(
                    ANTISPOOF_REQUIRED=True, ANTISPOOF_MODEL=handle.name)):
                self.assertTrue(live.antispoof_ready())

    def test_it_can_be_switched_off_deliberately(self):
        """Explicitly, in settings — never by a missing file going unnoticed."""
        with override_settings(FACE=self._conf(
                ANTISPOOF_REQUIRED=False, ANTISPOOF_MODEL="")):
            self.assertTrue(live.antispoof_ready())


class MatchDecisionTests(SimpleTestCase):
    """The verdict `match_frame` reaches, with the model itself stubbed out."""

    class _Face:
        def __init__(self, embedding, score=0.9, height=200):
            self.normed_embedding = embedding
            self.det_score = score
            self.bbox = (0, 0, height, height)

    def _run(self, faces, embeddings, liveness=1.0, **conf):
        from django.conf import settings

        from accounts import face as engine

        merged = {**settings.FACE, **conf}
        with override_settings(FACE=merged), \
                patch.object(engine, "decode_image", return_value="IMAGE"), \
                patch.object(engine, "get_live_app",
                             return_value=type("A", (), {"get": lambda s, i: faces})()), \
                patch.object(engine, "liveness_score", return_value=liveness):
            return engine.match_frame(b"frame", embeddings)

    def test_an_empty_frame_asks_for_another(self):
        """Not an error: the student blinked or looked away. Send another."""
        self.assertEqual(self._run([], [[1, 0]])["state"], "no_face")

    def test_two_faces_are_refused(self):
        self.assertEqual(self._run([self._Face([1, 0]), self._Face([0, 1])],
                                   [[1, 0]])["state"], "many_faces")

    def test_a_face_that_is_too_small_is_told_to_come_closer(self):
        verdict = self._run([self._Face([1, 0], height=40)], [[1, 0]])
        self.assertEqual(verdict["state"], "too_far")

    def test_the_student_matches_their_own_vector(self):
        verdict = self._run([self._Face([1.0, 0.0])], [[1.0, 0.0]])
        self.assertEqual(verdict["state"], "matched")
        self.assertAlmostEqual(verdict["score"], 1.0)

    def test_it_matches_against_the_best_of_the_three_angles(self):
        """A student looking slightly left should match the left enrolment."""
        verdict = self._run([self._Face([0.0, 1.0])],
                            [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]])
        self.assertEqual(verdict["state"], "matched")

    def test_somebody_else_does_not_match(self):
        verdict = self._run([self._Face([0.0, 1.0])], [[1.0, 0.0]])
        self.assertEqual(verdict["state"], "no_match")
        self.assertLess(verdict["score"], 0.42)

    def test_a_photograph_is_refused_before_the_match_is_even_considered(self):
        """
        The order matters. A held-up photo of the right student matches
        perfectly, so liveness has to be decided first or it decides nothing.
        """
        verdict = self._run([self._Face([1.0, 0.0])], [[1.0, 0.0]], liveness=0.1)
        self.assertEqual(verdict["state"], "not_live")
        self.assertNotIn("score", {k: v for k, v in verdict.items() if k == "match"})

    def test_the_threshold_comes_from_settings(self):
        near = [0.8, 0.6]
        self.assertEqual(
            self._run([self._Face(near)], [[1.0, 0.0]], MATCH_MIN=0.9)["state"],
            "no_match")
        self.assertEqual(
            self._run([self._Face(near)], [[1.0, 0.0]], MATCH_MIN=0.5)["state"],
            "matched")


class TicketRuleTests(TestCase):
    """What a ticket authorises, and for how long."""

    def setUp(self):
        from academics.models import Batch, Department, Enrollment, StudentProfile, Subject
        from attendance.models import AttendanceSession

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="DS")
        self.other = Subject.objects.create(department=self.cse, code="DBMS", name="DB")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)
        self.teacher = User.objects.create_user(
            email="t@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.cse, registration_completed=True)
        self.student_user = User.objects.create_user(
            email="s@i.edu", password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, department=self.cse,
            registration_completed=True, face_enrolled=True)
        self.student = StudentProfile.objects.create(
            user=self.student_user, department=self.cse, batch=self.batch, class_roll="01")
        Enrollment.objects.create(student=self.student, subject=self.dsa)

        def session(subject):
            return AttendanceSession.objects.create(
                teacher=self.teacher, subject=subject, batch=self.batch,
                latitude=22.5, longitude=88.3, expected_count=1,
                expires_at=timezone.now() + dt.timedelta(minutes=5))

        self.session = session(self.dsa)
        self.other_session = session(self.other)

    def _ticket(self, session=None, **over):
        from attendance.models import FaceVerifyTicket

        fields = {
            "session": session or self.session, "student": self.student,
            "latitude": 22.5, "longitude": 88.3, "distance_m": 4.0,
            "expires_at": timezone.now() + dt.timedelta(minutes=3),
        }
        fields.update(over)
        return FaceVerifyTicket.objects.create(**fields)

    def _load(self, ticket, session=None):
        return live.load_attempt(
            user=self.student_user,
            session_token=(session or self.session).token,
            ticket_token=ticket.token)

    def _with_face(self):
        from accounts.models import FaceEnrolment, FaceSample

        enrolment = FaceEnrolment.objects.create(user=self.student_user)
        FaceSample.objects.create(enrolment=enrolment, pose="FRONT",
                                  embedding=[1.0, 0.0])
        return enrolment

    @override_settings()
    def test_a_fresh_ticket_loads_the_stored_vectors(self):
        from django.conf import settings

        self._with_face()
        with override_settings(FACE={**settings.FACE, "ANTISPOOF_REQUIRED": False}):
            result = self._load(self._ticket())
        self.assertNotIn("error", result)
        self.assertEqual(result["embeddings"], [[1.0, 0.0]])

    def test_an_unknown_ticket_is_refused(self):
        result = live.load_attempt(user=self.student_user,
                                   session_token=self.session.token,
                                   ticket_token="not-a-real-token")
        self.assertIn("not authorised", result["error"])

    def test_a_ticket_cannot_be_replayed_against_another_class(self):
        """Otherwise one geo check would become a season ticket."""
        result = self._load(self._ticket(), session=self.other_session)
        self.assertIn("different class", result["error"])

    def test_a_spent_ticket_is_refused(self):
        ticket = self._ticket(used_at=timezone.now())
        self.assertIn("already been used", self._load(ticket)["error"])

    def test_an_expired_ticket_is_refused(self):
        ticket = self._ticket(expires_at=timezone.now() - dt.timedelta(seconds=1))
        self.assertIn("timed out", self._load(ticket)["error"])

    def test_a_student_with_no_face_on_file_is_told_so(self):
        from django.conf import settings

        with override_settings(FACE={**settings.FACE, "ANTISPOOF_REQUIRED": False}):
            self.assertIn("No face is on file", self._load(self._ticket())["error"])

    def test_marking_is_blocked_when_liveness_is_unconfigured(self):
        from django.conf import settings

        self._with_face()
        with override_settings(FACE={**settings.FACE,
                                     "ANTISPOOF_REQUIRED": True,
                                     "ANTISPOOF_MODEL": ""}):
            self.assertIn("not fully configured", self._load(self._ticket())["error"])

    # ----------------------------------------------------------- completion
    def test_completing_spends_the_ticket_and_writes_the_record(self):
        from attendance.models import AttendanceRecord

        ticket = self._ticket()
        result = live.complete_mark(ticket=ticket, score=0.8, liveness=0.9)
        self.assertTrue(result["ok"])
        ticket.refresh_from_db()
        self.assertTrue(ticket.is_spent)
        record = AttendanceRecord.objects.get(session=self.session, student=self.student)
        # The numbers come from the ticket, not from anything the socket said —
        # a client that could restate its own distance would make the geo-fence
        # decorative.
        self.assertEqual(record.distance_m, 4.0)
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)

    def test_two_frames_matching_at_once_produce_one_record(self):
        from attendance.models import AttendanceRecord

        ticket = self._ticket()
        first = live.complete_mark(ticket=ticket, score=0.8)
        second = live.complete_mark(ticket=ticket, score=0.9)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(
            AttendanceRecord.objects.filter(session=self.session).count(), 1)

    # ------------------------------------------------------------- fallback
    def test_giving_up_asks_the_teacher_and_marks_nothing(self):
        from attendance.models import AttendanceRecord, ManualMarkRequest

        ticket = self._ticket()
        live.request_manual_mark(ticket=ticket, reason="Too dark",
                                 attempts=30, best_score=0.31)
        request = ManualMarkRequest.objects.get(session=self.session,
                                                student=self.student)
        self.assertEqual(request.status, ManualMarkRequest.Status.PENDING)
        self.assertEqual(request.attempts, 30)
        self.assertFalse(AttendanceRecord.objects.filter(session=self.session).exists())

    def test_asking_twice_updates_rather_than_duplicating(self):
        from attendance.models import ManualMarkRequest

        ticket = self._ticket()
        live.request_manual_mark(ticket=ticket, reason="x", attempts=5, best_score=0.2)
        live.request_manual_mark(ticket=ticket, reason="x", attempts=40, best_score=0.35)
        requests = ManualMarkRequest.objects.filter(session=self.session)
        self.assertEqual(requests.count(), 1)
        self.assertEqual(requests.first().attempts, 40)

    def test_the_teacher_approving_marks_the_student_present(self):
        from attendance.models import AttendanceRecord, ManualMarkRequest

        live.request_manual_mark(ticket=self._ticket(), reason="x")
        request = ManualMarkRequest.objects.get(session=self.session)
        live.decide_manual_mark(request_obj=request, teacher=self.teacher, approve=True)
        request.refresh_from_db()
        self.assertEqual(request.status, ManualMarkRequest.Status.APPROVED)
        record = AttendanceRecord.objects.get(session=self.session, student=self.student)
        self.assertEqual(record.status, AttendanceRecord.Status.MANUAL)

    def test_the_teacher_refusing_marks_nothing(self):
        from attendance.models import AttendanceRecord, ManualMarkRequest

        live.request_manual_mark(ticket=self._ticket(), reason="x")
        request = ManualMarkRequest.objects.get(session=self.session)
        live.decide_manual_mark(request_obj=request, teacher=self.teacher, approve=False)
        request.refresh_from_db()
        self.assertEqual(request.status, ManualMarkRequest.Status.REJECTED)
        self.assertFalse(AttendanceRecord.objects.filter(session=self.session).exists())
