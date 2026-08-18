"""
What each account is offered in the sidebar.

Worth testing rather than eyeballing, because a nav entry is the only thing
that tells somebody a screen exists — and because "the university's Subjects"
and "the colleges' Subjects" were two links a click apart with the same word on
them, which is the kind of thing that reads fine to whoever wrote it.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)


class NavTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)
        self.admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)
        self.institute = Institute.objects.create(
            name="Acme", code="ACME", email="o@a.edu", status="APPROVED")
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.university)
        self.head = User.objects.create_user(
            email="head@a.edu", password="Str0ngPass!23", role=User.Role.HEAD,
            institute=self.institute, registration_completed=True)

    def nav(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("dashboard:home")).content.decode()

    def links(self, user):
        import re

        return set(re.findall(r'href="([^"]+)"', self.nav(user)))

    # The university.
    def test_the_university_is_not_offered_the_colleges_subjects_or_batches(self):
        links = self.links(self.admin)
        self.assertNotIn(reverse("academics:subjects"), links)
        self.assertNotIn(reverse("academics:batches"), links)

    def test_the_university_is_offered_its_own_catalogue(self):
        links = self.links(self.admin)
        for name in ("catalogue_departments", "catalogue_batches",
                     "catalogue_subjects"):
            self.assertIn(reverse(f"academics:{name}"), links, name)

    def test_the_remaining_institutes_data_links_are_still_there(self):
        """
        Teachers and Students stay: there is no catalogue equivalent of a
        person, so those two are the only place a university sees them.
        """
        links = self.links(self.admin)
        self.assertIn(reverse("academics:teachers"), links)
        self.assertIn(reverse("academics:students"), links)

    def test_the_section_heading_is_kept_because_it_still_has_rows(self):
        self.assertIn("Institutes' data", self.nav(self.admin))

    def test_the_pages_are_still_routable_just_unlisted(self):
        """
        Unlisting is not the same decision as closing the route, and this test
        exists so that if somebody later wants the second, they change it
        deliberately rather than discovering it here.
        """
        self.client.force_login(self.admin)
        for name in ("academics:subjects", "academics:batches"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200,
                             name)

    # The institute, unaffected.
    def test_the_institute_head_still_gets_subjects_and_batches(self):
        links = self.links(self.head)
        self.assertIn(reverse("academics:subjects"), links)
        self.assertIn(reverse("academics:batches"), links)

    def test_the_institute_head_is_not_offered_the_catalogue(self):
        links = self.links(self.head)
        self.assertNotIn(reverse("academics:catalogue_departments"), links)
        self.assertNotIn(reverse("academics:catalogue_subjects"), links)
