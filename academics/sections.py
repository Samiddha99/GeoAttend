"""
Sections: naming them, finding them, and keeping a student in one of their own.

Three call sites need the same three answers — the importer, the student edit
endpoint and the filters on the Students page — so they live here rather than
three times over.

**Names are normalised, not free.** "a", "A " and "A" are one section. The
alternative is a filter dropdown with three entries that all look identical,
which is exactly the failure a record was chosen over a text field to avoid.
Normalising on the way in is the only place it can be done once.

**A student's section must belong to their own batch.** Section A of 2022-26 and
section A of 2023-27 are different groups of people. Nothing in the database can
express that rule — it spans two tables through a third — so `assert_in_batch`
is called by every path that writes a section, and moving a student between
batches clears a section that no longer applies rather than leaving a silent
mismatch.
"""
import re

from .models import Section, StudentProfile


class SectionError(Exception):
    """A refusal with a message meant to be shown to the person."""


def normalise(name):
    """
    Upper-cased, inner runs of space collapsed, trimmed.

    Upper because sections are letters or short codes and nobody means "a" and
    "A" differently. If a college ever wants "Alpha" spelled that way, this is
    the one line to reconsider.
    """
    return re.sub(r"\s+", " ", (name or "").strip()).upper()


def resolve(batch, name, *, create=False):
    """
    The section of `batch` called `name`, optionally creating it.

    Returns `(section, created)`. `section` is None for a blank name, which is
    how "no section" travels through the importer and the edit form without a
    special case at each call site.

    `created` is returned rather than left for the caller to work out. The
    importer needs it to report which sections a file brought into existence,
    and the obvious alternative — counting rows before and after — is two extra
    queries per line of a two-thousand-line spreadsheet.

    `create` is the importer's choice, not a default: a spreadsheet is the
    source of truth for a new intake, but a typo on the edit form should be
    refused rather than quietly becoming a fourth section.
    """
    name = normalise(name)
    if not name:
        return None, False
    if batch is None:
        raise SectionError("Choose a batch before assigning a section.")

    existing = Section.objects.filter(batch=batch, name=name).first()
    if existing is not None:
        return existing, False
    if not create:
        raise SectionError(
            f"{batch.label} has no section {name}. Check the spelling, or "
            f"import the roster — an import creates sections it does not "
            f"recognise.")
    # `get_or_create` rather than `create`: two rows of one file naming the
    # same new section arrive here twice, and the second must find the first
    # rather than trip the uniqueness constraint and fail the whole import.
    section, made = Section.objects.get_or_create(batch=batch, name=name)
    return section, made


def assert_in_batch(section, batch):
    """
    Raise unless this section belongs to this batch.

    The rule no constraint can hold. Called by every write path, because the
    one that forgets is the one that puts a 2022-26 student into 2023-27's
    section A and makes both rosters wrong.
    """
    if section is None:
        return
    if batch is None or section.batch_id != batch.pk:
        raise SectionError(
            f"Section {section.name} belongs to {section.batch.label}, not to "
            f"{batch.label if batch else 'this batch'}.")


def for_batches(batches):
    """
    Sections of these batches, for a filter dropdown.

    Live ones only: a filter offering a retired section would return an empty
    table and look like a bug. The *table* still shows a retired section's name
    against a student who is in one, which is the same split the department
    filters use — see academics/views.students_page.
    """
    ids = [b.pk if hasattr(b, "pk") else b for b in batches]
    if not ids:
        return Section.objects.none()
    return Section.objects.filter(
        batch_id__in=ids, is_active=True).select_related("batch")


def for_user(user):
    """Every section this account may see, ordered for a dropdown."""
    from .selectors import batches_for

    # Materialised rather than passed as a queryset: `batch__in=<qs>` is a
    # correlated subquery and django_mongodb_backend cannot express one. sqlite
    # runs it happily, which is how this shape reaches production unnoticed —
    # see the long note in accounts/affiliations.py.
    batch_ids = list(batches_for(user).values_list("id", flat=True))
    return for_batches(batch_ids).order_by("batch__label", "name")


def apply_to(profile, section, *, batch=None):
    """
    Put a student in a section, checking it is one of their own.

    `batch` defaults to the profile's, but the caller passes the *new* batch
    when both are changing at once — otherwise the check runs against the batch
    the student is leaving and lets a mismatch through.
    """
    batch = batch or profile.batch
    assert_in_batch(section, batch)
    profile.section = section
    return profile


def clear_if_foreign(profile):
    """
    Drop a section that no longer belongs to the student's batch.

    Called after a batch move. Leaving it would be worse than clearing it: an
    unsectioned student is an ordinary state every screen already renders,
    while a student listed under another cohort's section is a quiet error in
    two rosters at once.
    """
    if profile.section_id and profile.section.batch_id != profile.batch_id:
        profile.section = None
        return True
    return False


def counts_for(sections):
    """
    How many live students each section holds, as `{section_id: n}`.

    One grouped query rather than a count per row. Several filtered `Count`
    annotations over different relations fan out on MongoDB and inflate every
    figure — a bug this project has had twice.
    """
    from django.db.models import Count

    ids = [s.pk if hasattr(s, "pk") else s for s in sections]
    if not ids:
        return {}
    return dict(
        StudentProfile.objects.filter(section_id__in=ids, is_active=True)
        .values_list("section_id").annotate(n=Count("id"))
        .values_list("section_id", "n"))
