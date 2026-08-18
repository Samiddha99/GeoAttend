"""
Who an email actually goes to.

Every organisation in this system has two addresses and they are not the same
thing:

* the **official** address — `University.email`, `Institute.email` — which is
  the public contact printed on a letterhead. It is typed once at registration,
  nobody signs in with it, and for a seeded university it is
  `<code>@unclaimed.invalid`: a reserved domain that can never receive mail.
* the **login** address — `User.email` — which somebody chose because they
  read it, and which they type every time they sign in.

Notifications go to the login address. The rule matters most exactly where it
is easiest to get wrong: the "an institute is waiting for your approval" mail
was addressed to `University.email`, so for all 112 seeded universities it was
posted to `.invalid` and silently discarded. The queue was on screen, but the
message telling anyone to look at it never arrived.

The official address is kept as a last resort rather than dropped. An institute
invited but not yet accepted has no active login, and an email to a real
letterhead address beats no email at all — but that is the fallback, never the
first choice.
"""
import logging

log = logging.getLogger("geoattend")


def _logins(queryset):
    """Verified-or-not, active login addresses, in a stable order."""
    return [
        email for email in queryset.filter(is_active=True)
        .exclude(email="").order_by("date_joined").values_list("email", flat=True)
        if email
    ]


def university_recipients(university, fallback=True):
    """
    Every login that can act for this university.

    All of them, not just the first. A university with two administrators has
    two people who might be the one at their desk today, and an approval queue
    that only ever pings whoever registered first is a queue that stalls the
    moment that person leaves.
    """
    from .models import User

    addresses = _logins(university.users.filter(role=User.Role.UNIVERSITY))
    if addresses:
        return addresses
    if fallback and _deliverable(university.email):
        return [university.email]
    log.warning("no login address for university %s — nothing was emailed",
                university.name)
    return []


def institute_recipients(institute, fallback=True):
    """
    The institute's head, by login address.

    Heads only. HODs and teachers are not told that their institute's
    registration was approved or rejected — that is the head's business, and
    a rejection reason in particular is not staff-wide news.
    """
    from .models import User

    addresses = _logins(institute.users.filter(role=User.Role.HEAD))
    if addresses:
        return addresses
    if fallback and _deliverable(institute.email):
        return [institute.email]
    log.warning("no login address for institute %s — nothing was emailed",
                institute.name)
    return []


def _deliverable(address):
    """
    Whether an official address is worth trying at all.

    `.invalid` is reserved by RFC 2606 precisely so that it can never resolve;
    `seed_universities` uses it to mark a row nobody has claimed. Handing one to
    the mail provider spends a send and earns a bounce, so they are dropped here
    rather than at the provider.
    """
    address = (address or "").strip()
    return bool(address) and not address.lower().endswith(
        (".invalid", "@unclaimed.invalid"))
