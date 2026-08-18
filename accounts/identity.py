"""
Who may change an institute's *identity* — its name, its official email, and
the login its head signs in with.

Distinct from the curriculum rule in `academics/curriculum.py`, which is about
rows a university wrote. This is about the institute itself, and it answers a
different question: an affiliated institute's name is the name its degrees are
awarded under, so it is the affiliating university's to correct, not the
institute's to change quietly. An autonomous institute answers to nobody and
keeps both.

**Why the login email is in here at all.** Renaming an institute and moving its
head's login are usually the same job — a college is renamed, the domain
changes, and one of the two happening without the other leaves an account
nobody can reach. They are one permission because they are one act.

**Where this stops.** A university may move a head's login address; it may not
set that head's password or sign in as them. `accounts.views` enforces that
separately, and deliberately: correcting an address is reversible, and an
account takeover is not.
"""


def is_autonomous(institute):
    """
    True when no university awards this institute's degrees.

    An institute with two disciplines, one affiliated and one autonomous, is
    *not* autonomous: there is still a university whose name is on the
    certificates, and it is the one that should be correcting the record.
    """
    if institute is None:
        return False
    affiliations = institute.affiliations.all()
    if not affiliations:
        # No disciplines recorded at all. Treated as autonomous rather than
        # locked: an institute with nothing on file has nobody to ask, and
        # locking it would leave it unable to fix its own name forever.
        return True
    return all(a.university_id is None for a in affiliations)


def affiliating_universities(institute):
    """The bodies that would have to make the change instead. Possibly empty."""
    from .models import University

    if institute is None:
        return University.objects.none()
    return University.objects.filter(
        affiliated_institutes__institute=institute).distinct()


def may_edit_identity(user, institute):
    """
    May this account change the institute's name, official email or head login?

    True for a university that reaches the institute, and for the head of an
    *autonomous* institute. False for the head of an affiliated one — that is
    the requirement — and false for everyone else, including HoDs and
    teachers, for whom this was never a question.
    """
    from .scoping import institutes_for

    if not getattr(user, "is_authenticated", False) or institute is None:
        return False
    if getattr(user, "is_university", False):
        return institutes_for(user, focused=False).filter(
            pk=institute.pk).exists()
    if getattr(user, "is_head", False) and user.institute_id == institute.pk:
        return is_autonomous(institute)
    return False


def may_edit_own_name(user):
    """
    May this account change its *own* full name from the profile page?

    False for a teacher. Their name is not a display preference — it was
    checked against their PAN when the institute added them
    (`accounts/pan.py`), and it is the name a college's records, a university's
    suspension notice and any future certificate are all written against. A
    teacher who could edit it could quietly make the verified identity and the
    account disagree, and nothing downstream would notice.

    True for everyone else. A head, a HoD, a student or a guardian typed their
    own name in the first place and nothing was verified against it, so
    correcting a spelling is theirs to do.

    Not a permission the institute loses: a head or HoD still edits a teacher's
    name from the Teachers page, where the change is made by somebody
    accountable for it and is logged.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    return not getattr(user, "is_teacher", False)


def own_name_lock_reason(user):
    """
    Why a teacher's own name is read-only, or None.

    A locked field with no explanation reads as a bug — and this one has a real
    answer that also tells the person where to go, which is more useful than
    the lock itself.
    """
    if may_edit_own_name(user):
        return None
    return ("Your name was verified against your PAN when your institute added "
            "you, so it is not editable here. Ask your head of department to "
            "correct it if it is wrong.")


def identity_lock_reason(user, institute):
    """
    Why these fields are read-only, phrased for the person looking at them, or
    None when they are not.

    A locked field with no explanation reads as a bug, and the support question
    that follows ("why can't I fix our name?") costs more than the sentence.
    """
    if may_edit_identity(user, institute) or institute is None:
        return None
    if getattr(user, "is_head", False) and user.institute_id == institute.pk:
        names = ", ".join(u.short_name or u.name
                          for u in affiliating_universities(institute))
        return ("Your name and official email are held by your affiliating "
                + (f"university ({names})" if names else "university")
                + ", because they appear on the degrees awarded here. Ask them "
                  "to amend the record and it will update on this page.")
    return "You do not have permission to change these details."
