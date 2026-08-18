"""
Who may change which affiliation, and what delinking actually does.

An `InstituteAffiliation` row is a claim about two parties — "we teach
pharmacy, and *that* university awards it" — and only one of them can make the
second half. Nearly every test here is a variation on that.

The one worth reading twice is `test_delinking_releases_the_shared_curriculum`.
Delinking that left the pushed syllabus locked would have been a plausible
outcome and a bad one: an institute frozen under a university with no remaining
say and no reason to log in.
"""
import json

from django.test import RequestFactory, TestCase

from accounts import university_views, views
from accounts.affiliations import (
    AffiliationError,
    add_autonomous,
    available_disciplines,
    delink,
    remove,
    rows_for,
    set_affiliation,
)
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)


class AffiliationFixture(TestCase):
    def setUp(self):
        self.engg = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        UniversityDiscipline.objects.create(university=self.engg,
                                            discipline=Discipline.ENGG)
        self.health = University.objects.create(
            name="Health University", code="HEALTHU", short_name="HEALTHU",
            email="h@u.edu", grants_affiliation=True)

        self.admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.engg,
            registration_completed=True)

        self.institute = Institute.objects.create(
            name="Acme College", code="ACME", email="office@acme.edu",
            status=Institute.Status.APPROVED)
        # Engineering under us, pharmacy under somebody else.
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.engg)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.PHARMACY,
            university=self.health)
        self.head = User.objects.create_user(
            email="head@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.institute,
            registration_completed=True)

    def _post(self, view, user, pk=None, **data):
        request = RequestFactory().post("/", data)
        request.user = user
        request.session = self.client.session
        request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        return view(request, pk=pk) if pk is not None else view(request)

    def _body(self, response):
        return json.loads(response.content)

    def _university_of(self, discipline):
        return self.institute.affiliations.get(discipline=discipline).university


class ListingTests(AffiliationFixture):
    """Feature 3: the institute can see what it has and who awards it."""

    def test_the_rows_name_the_university_for_each_discipline(self):
        rows = {r["discipline"]: r for r in rows_for(self.institute)}
        self.assertEqual(rows["ENGG"]["university"], "ENGGU")
        self.assertFalse(rows["ENGG"]["autonomous"])
        self.assertEqual(rows["PHARMACY"]["university"], "HEALTHU")

    def test_an_autonomous_discipline_says_so_rather_than_showing_a_blank(self):
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)
        rows = {r["discipline"]: r for r in rows_for(self.institute)}
        self.assertTrue(rows["GENERAL"]["autonomous"])
        self.assertEqual(rows["GENERAL"]["university"], "")

    def test_the_rows_follow_the_declared_order_not_the_stored_codes(self):
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.AGRI,
            university=None)
        order = [r["discipline"] for r in rows_for(self.institute)]
        self.assertEqual(order, ["AGRI", "ENGG", "PHARMACY"])

    def test_the_add_list_offers_only_what_is_missing(self):
        offered = [d["value"] for d in available_disciplines(self.institute)]
        self.assertNotIn("ENGG", offered)
        self.assertNotIn("PHARMACY", offered)
        self.assertIn("GENERAL", offered)

    def test_the_profile_page_shows_the_disciplines_and_who_awards_them(self):
        self.client.force_login(self.head)
        page = self.client.get("/auth/profile/").content.decode()
        self.assertIn("Engineering, Technology &amp; Management", page)
        self.assertIn("ENGGU", page)
        self.assertIn("HEALTHU", page)


class InstituteAddsItsOwnTests(AffiliationFixture):
    """Feature 4."""

    def test_an_institute_may_record_a_new_discipline_as_autonomous(self):
        result = add_autonomous(institute=self.institute,
                                disciplines=["GENERAL"], actor=self.head)
        self.assertEqual(result["added"], ["General Courses (Arts, Science, Commerce)"])
        self.assertIsNone(self._university_of("GENERAL"))

    def test_an_affiliated_institute_may_still_do_this(self):
        """
        Affiliation is per discipline. An institute under a university for
        engineering may open an autonomous general-courses wing and needs
        nobody's permission to say so.
        """
        response = self._post(views.api_add_disciplines, self.head,
                              disciplines=["GENERAL"])
        self.assertTrue(self._body(response)["success"], response.content)
        self.assertIsNone(self._university_of("GENERAL"))

    def test_re_adding_an_affiliated_discipline_does_not_drop_its_university(self):
        """
        The rule inverted: if this overwrote, an affiliated institute could
        free itself by re-adding the same discipline as autonomous.
        """
        result = add_autonomous(institute=self.institute,
                                disciplines=["ENGG"], actor=self.head)
        self.assertEqual(result["added"], [])
        self.assertEqual(self._university_of("ENGG"), self.engg)

    def test_there_is_no_way_to_name_a_university_from_this_endpoint(self):
        """Posting one is ignored rather than honoured."""
        self._post(views.api_add_disciplines, self.head,
                   disciplines=["GENERAL"], university=str(self.engg.pk))
        self.assertIsNone(self._university_of("GENERAL"))

    def test_a_teacher_cannot_add_disciplines(self):
        teacher = User.objects.create_user(
            email="t@acme.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.institute)
        response = self._post(views.api_add_disciplines, teacher,
                              disciplines=["GENERAL"])
        self.assertEqual(response.status_code, 403)

    def test_an_unknown_discipline_code_is_refused(self):
        with self.assertRaises(AffiliationError):
            add_autonomous(institute=self.institute,
                           disciplines=["NONSENSE"], actor=self.head)


class UniversityChangesThemTests(AffiliationFixture):
    """Features 5 and 6."""

    def test_a_university_can_affiliate_a_new_discipline_to_itself(self):
        result = set_affiliation(institute=self.institute,
                                 disciplines=["GENERAL"],
                                 university=self.engg, actor=self.admin)
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(self._university_of("GENERAL"), self.engg)

    def test_a_university_can_take_over_an_autonomous_discipline(self):
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)
        set_affiliation(institute=self.institute, disciplines=["GENERAL"],
                        university=self.engg, actor=self.admin)
        self.assertEqual(self._university_of("GENERAL"), self.engg)

    def test_delinking_makes_the_institute_autonomous_and_keeps_the_row(self):
        """
        Feature 6. The row stays with a null university — the institute has not
        stopped teaching engineering, it has stopped answering to us for it.
        """
        result = delink(institute=self.institute, disciplines=["ENGG"],
                        university=self.engg, actor=self.admin)
        self.assertEqual(len(result["delinked"]), 1)
        self.assertTrue(
            self.institute.affiliations.filter(discipline="ENGG").exists())
        self.assertIsNone(self._university_of("ENGG"))

    def test_a_university_cannot_touch_another_universitys_discipline(self):
        for action in (set_affiliation, delink, remove):
            with self.subTest(action=action.__name__):
                with self.assertRaises(AffiliationError) as caught:
                    action(institute=self.institute, disciplines=["PHARMACY"],
                           university=self.engg, actor=self.admin)
                self.assertIn("HEALTHU", str(caught.exception))
        self.assertEqual(self._university_of("PHARMACY"), self.health)

    def test_removing_drops_the_row_entirely(self):
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.GENERAL,
            university=None)
        remove(institute=self.institute, disciplines=["GENERAL"],
               university=self.engg, actor=self.admin)
        self.assertFalse(
            self.institute.affiliations.filter(discipline="GENERAL").exists())

    def test_removing_everything_is_refused(self):
        """
        An institute teaching nothing is not a state any screen knows how to
        render, and the person almost certainly meant delink.
        """
        self.institute.affiliations.filter(discipline="PHARMACY").delete()
        with self.assertRaises(AffiliationError) as caught:
            remove(institute=self.institute, disciplines=["ENGG"],
                   university=self.engg, actor=self.admin)
        self.assertIn("Delink instead", str(caught.exception))


class UniversityEndpointTests(AffiliationFixture):
    def test_the_endpoint_delinks_and_reports_the_new_state(self):
        response = self._post(
            university_views.api_institute_disciplines, self.admin,
            pk=self.institute.pk, action="delink", disciplines=["ENGG"])
        body = self._body(response)
        self.assertTrue(body["success"], response.content)
        self.assertIn("autonomous", body["message"])
        rows = {a["discipline"]: a for a in body["data"]["affiliations"]}
        self.assertTrue(rows["ENGG"]["autonomous"])

    def test_the_endpoint_refuses_another_universitys_discipline(self):
        response = self._post(
            university_views.api_institute_disciplines, self.admin,
            pk=self.institute.pk, action="delink", disciplines=["PHARMACY"])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._university_of("PHARMACY"), self.health)

    def test_an_unknown_action_is_refused_rather_than_guessed_at(self):
        response = self._post(
            university_views.api_institute_disciplines, self.admin,
            pk=self.institute.pk, action="destroy", disciplines=["ENGG"])
        self.assertFalse(self._body(response)["success"])

    def test_a_university_may_record_an_autonomous_wing_without_claiming_it(self):
        response = self._post(
            university_views.api_institute_disciplines, self.admin,
            pk=self.institute.pk, action="autonomous", disciplines=["GENERAL"])
        self.assertTrue(self._body(response)["success"], response.content)
        self.assertIsNone(self._university_of("GENERAL"))

    def test_the_row_carries_which_university_not_only_its_name(self):
        """The editor needs the id to tell "ours" from "theirs"."""
        response = self._post(
            university_views.api_institute_disciplines, self.admin,
            pk=self.institute.pk, action="affiliate", disciplines=["GENERAL"])
        rows = {a["discipline"]: a
                for a in self._body(response)["data"]["affiliations"]}
        self.assertEqual(rows["ENGG"]["university_id"], str(self.engg.pk))
        self.assertEqual(rows["PHARMACY"]["university_id"], str(self.health.pk))


class DelinkReleasesCurriculumTests(AffiliationFixture):
    """
    The consequence that would have been easy to miss.

    A subject pushed under an affiliation is stamped with the university that
    wrote it. If read-only keyed on that stamp alone, delinking would leave the
    institute's syllabus frozen for good — locked to a university with no
    remaining say and no reason to log in.
    """

    def setUp(self):
        super().setUp()
        from academics.models import Department, Subject

        from academics.models import UniversityDepartment

        entry = UniversityDepartment.objects.create(
            university=self.engg, discipline=Discipline.ENGG,
            name="CSE Department", code="CSE")
        # Adopted from the catalogue — the link is what makes its rows the
        # university's now. See academics/catalogue.py.
        self.department = Department.objects.create(
            institute=self.institute, code="CSE", name="CSE Department",
            discipline=Discipline.ENGG, source=entry)
        from academics.models import UniversitySubject

        published = UniversitySubject.objects.create(
            department=self.department.source, code="DSA",
            name="Data Structures", semester=3, credits=4)
        self.subject = Subject.objects.create(
            department=self.department, code="DSA", name="Data Structures",
            semester=3, credits=4, source=published)

    def test_the_syllabus_is_read_only_while_the_affiliation_stands(self):
        from academics.curriculum import is_read_only

        self.assertTrue(is_read_only(self.subject, self.head))

    def test_delinking_releases_the_shared_curriculum(self):
        from academics.curriculum import is_read_only

        delink(institute=self.institute, disciplines=["ENGG"],
               university=self.engg, actor=self.admin)
        # `release` clears the link with a bulk update, so the instance held
        # here is stale until it is re-read.
        self.subject.refresh_from_db()
        self.assertFalse(is_read_only(self.subject, self.head))

    def test_the_department_is_marked_legacy_so_the_change_is_explicable(self):
        """
        Changed with the catalogue. There is no stamp to survive now — the link
        *was* the claim, so releasing means clearing it. `is_legacy` is what
        remains, and it is there so a screen can say why this department is
        suddenly editable when its neighbours are not.
        """
        delink(institute=self.institute, disciplines=["ENGG"],
               university=self.engg, actor=self.admin)
        self.department.refresh_from_db()
        self.assertIsNone(self.department.source_id)
        self.assertTrue(self.department.is_legacy)

    def test_the_university_keeps_its_own_access_while_it_still_affiliates(self):
        """
        Delinking engineering leaves pharmacy under Health, not under us — so
        the subject is released even though *an* affiliation remains.
        """
        from academics.curriculum import is_read_only

        delink(institute=self.institute, disciplines=["ENGG"],
               university=self.engg, actor=self.admin)
        self.assertTrue(
            self.institute.affiliations.filter(university=self.health).exists())
        self.subject.refresh_from_db()
        self.assertFalse(is_read_only(self.subject, self.head))
