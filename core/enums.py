"""
Row lifecycle, shared by every model that has one.

**Two independent facts, two fields.** A row's own lifecycle — is it running,
is it waiting for somebody to accept an invitation, has it been retired — is
*not* the same question as whether the discipline it sits under is still on
its institute's record. They change for different reasons, at different times,
by different people.

They were one derived value for a while, and the bug that exposed the mistake
is worth recording: a revoked department reported **0 students**. Every student
in it was still perfectly active, but "revoked" had overwritten "active" on the
way to the screen, so counting active students found none. The department was
full and the number said empty.

So:

* `status` — ACTIVE / INVITED / ARCHIVED. The row's own state, and the only
  thing a count should look at.
* `is_revoked` — a boolean. Its discipline is no longer held by the institute.
  Display and filtering read it; counting never does.

A revoked row keeps its status. That is the whole point: it lets a screen say
"Revoked" while a count still says 43.
"""
from django.db import models


class RowStatus(models.TextChoices):
    """
    The lifecycle every scoped row shares.

    INVITED only means anything for people — an account that exists because
    somebody was invited and has not finished signing up. It is in the shared
    vocabulary rather than in a separate people-only enum so that one status
    column, one filter and one pill renderer serve every table.
    """

    ACTIVE = "ACTIVE", "Active"
    INVITED = "INVITED", "Invited"
    ARCHIVED = "ARCHIVED", "Archived"


#: What the UI calls the revoked flag. Not a `status` value — a row is revoked
#: *and* active, or revoked *and* archived, and flattening the two loses the
#: second half.
REVOKED_LABEL = "Revoked"
REVOKED_KEY = "REVOKED"

#: What the UI calls the suspension flag — the same arrangement again, for the
#: same reason. A suspended teacher is suspended *and* active; the university
#: barred them, the institute did not archive them, and a screen that showed
#: only one of those facts would send somebody to the wrong office. See
#: accounts/suspension.py.
SUSPENDED_LABEL = "Suspended"
SUSPENDED_KEY = "SUSPENDED"


def status_field(**kwargs):
    """The column, so all six models declare it identically."""
    kwargs.setdefault("max_length", 10)
    kwargs.setdefault("choices", RowStatus.choices)
    kwargs.setdefault("default", RowStatus.ACTIVE)
    kwargs.setdefault("db_index", True)
    return models.CharField(**kwargs)


def revoked_field(**kwargs):
    """
    The flag, likewise.

    Stored rather than computed. It was computed for a while — one query per
    page resolving every department's affiliation — and besides being the
    source of the counting bug it meant no query could filter on it, so
    "show me the revoked students" had to be done in the browser over whatever
    rows happened to have been sent.
    """
    kwargs.setdefault("default", False)
    kwargs.setdefault("db_index", True)
    kwargs.setdefault(
        "help_text",
        "The discipline this sits under is no longer on the institute's "
        "record. Independent of status: a revoked row keeps whatever status "
        "it had.")
    return models.BooleanField(**kwargs)


class SubjectType(models.TextChoices):
    """
    How a subject is taught.

    Stored as a short code rather than a free string so that grouping a
    dropdown and filtering a report agree on what the categories are. Kept
    deliberately coarse — a lab and a lecture behave differently for
    attendance; a seminar and a workshop mostly do not, and both land in
    Other rather than growing the list.

    Lives here rather than in `academics.models` because the university's
    catalogue needs the same list, and `academics.catalogue` is imported *by*
    `academics.models` — putting it there would make the import circular and
    duplicating it would let the two lists drift, which is exactly the drift
    the codes exist to prevent.
    """

    THEORY = "THEORY", "Theory"
    PRACTICAL = "PRACTICAL", "Practical"
    OTHER = "OTHER", "Other"


class Degree(models.TextChoices):
    """
    The programme a subject belongs to.

    Declared shortest-to-longest rather than alphabetically, because that is
    the order the dropdowns and filters read in and the order is load-bearing:
    grouping is done in Python against `Degree.choices`, never by sorting the
    stored codes (which would give BACHELOR, DIPLOMA, MASTERS).
    """

    DIPLOMA = "DIPLOMA", "Diploma"
    BACHELOR = "BACHELOR", "Bachelor"
    MASTERS = "MASTERS", "Masters"
