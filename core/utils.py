"""Generic helpers shared across apps."""
import datetime as dt
import hashlib
import math
import re
import secrets

from django.utils import timezone

EARTH_RADIUS_M = 6_371_008.8

# A MongoDB primary key as it arrives from the browser: 24 hex characters.
OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")


def is_object_id(value):
    """True if `value` could be a Mongo primary key."""
    return bool(value is not None and OBJECT_ID_RE.match(str(value)))


def clean_object_id(value):
    """
    An id from a query string or form body, or None if it isn't one.

    ObjectIdAutoField *raises* ValidationError on a malformed id rather than
    simply not matching, so passing unchecked user input straight into a
    queryset turns a typo into a 500. Filtering it out here keeps that at the
    edge. Returning None also reads naturally as "no filter applied".
    """
    return str(value) if is_object_id(value) else None


def clean_object_ids(values):
    """The valid ids from an arbitrary iterable, order preserved."""
    return [str(v) for v in (values or []) if is_object_id(v)]


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two WGS-84 points, in metres."""
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = p2 - p1
    d_lambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def valid_coords(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def random_token(nbytes=32):
    return secrets.token_urlsafe(nbytes)


def numeric_otp(length=6):
    return "".join(secrets.choice("0123456789") for _ in range(length))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


BATCH_RE = re.compile(r"^\s*(\d{4})\s*[-/–]\s*(\d{2,4})\s*$")


def parse_batch_label(label):
    """'2022-26' or '2022-2026' -> (2022, 2026, '2022-26'). Returns None if invalid."""
    m = BATCH_RE.match(str(label or ""))
    if not m:
        return None
    start = int(m.group(1))
    tail = m.group(2)
    end = int(tail) if len(tail) == 4 else int(str(start)[:2] + tail)
    if end <= start or end - start > 10:
        return None
    return start, end, f"{start}-{str(end)[-2:]}"


def default_date_range():
    """Dashboard default: 1 Jan of the current year -> today."""
    today = timezone.localdate()
    return dt.date(today.year, 1, 1), today


def parse_date(value, fallback=None):
    if not value:
        return fallback
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return fallback


def pct(numerator, denominator, digits=2):
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, digits)


def device_fingerprint(request, client_hash=""):
    """
    Cheap, privacy-friendly device signature: a hash of the UA string plus an
    opaque hash the browser computes (canvas/screen/timezone).  Not bullet-proof,
    but it raises the cost of proxy attendance considerably.
    """
    ua = request.META.get("HTTP_USER_AGENT", "")[:400]
    return sha256(f"{ua}|{client_hash}")[:64]
