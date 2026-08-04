"""The ObjectId → JSON boundary.

Every API response is built by core.http.ok()/fail(), and on MongoDB every
primary key is a bson ObjectId, which the stdlib JSON encoder refuses. Without
ApiJSONEncoder each of those endpoints raises TypeError at render time.
"""
import json
import uuid
from datetime import datetime
from decimal import Decimal

from django.test import SimpleTestCase

from core.http import ApiJSONEncoder, fail, ok

try:
    from bson import ObjectId
except ImportError:                                   # pragma: no cover
    ObjectId = None


class ObjectIdEncodingTests(SimpleTestCase):
    def setUp(self):
        if ObjectId is None:
            self.skipTest("bson is not installed")

    def test_ok_encodes_an_objectid(self):
        oid = ObjectId()
        body = json.loads(ok({"id": oid}).content)
        self.assertEqual(body["data"]["id"], str(oid))

    def test_nested_and_listed_objectids(self):
        rows = [{"id": ObjectId(), "name": "DSA"}, {"id": ObjectId(), "name": "DBMS"}]
        body = json.loads(ok({"rows": rows}).content)
        self.assertEqual([r["id"] for r in body["data"]["rows"]],
                         [str(r["id"]) for r in rows])

    def test_fail_encodes_too(self):
        oid = ObjectId()
        body = json.loads(fail("nope", errors={"batch": oid}).content)
        self.assertEqual(body["errors"]["batch"], str(oid))

    def test_the_hex_string_round_trips_back_to_the_same_id(self):
        """The browser posts the string back, so it must resolve to the same row."""
        from django_mongodb_backend.fields import ObjectIdField

        oid = ObjectId()
        sent = json.loads(ok({"id": oid}).content)["data"]["id"]
        self.assertEqual(ObjectIdField().to_python(sent), oid)

    def test_django_types_still_work(self):
        """ApiJSONEncoder extends DjangoJSONEncoder — it must not lose its base."""
        payload = json.dumps(
            {"when": datetime(2026, 8, 1, 2, 36), "pct": Decimal("75.5"),
             "ref": uuid.UUID("12345678-1234-5678-1234-567812345678")},
            cls=ApiJSONEncoder,
        )
        self.assertEqual(json.loads(payload), {
            "when": "2026-08-01T02:36:00",
            "pct": "75.5",
            "ref": "12345678-1234-5678-1234-567812345678",
        })

    def test_unknown_types_still_raise(self):
        """Silently stringifying everything would hide real bugs."""
        with self.assertRaises(TypeError):
            json.dumps({"x": object()}, cls=ApiJSONEncoder)


class ObjectIdInputTests(SimpleTestCase):
    """The browser→Python boundary: hex text meeting code that wanted an int."""

    def setUp(self):
        if ObjectId is None:
            self.skipTest("bson is not installed")

    def test_clean_object_id_accepts_real_ids(self):
        from core.utils import clean_object_id

        oid = ObjectId()
        self.assertEqual(clean_object_id(str(oid)), str(oid))
        self.assertEqual(clean_object_id(oid), str(oid))          # ObjectId in, str out
        self.assertEqual(clean_object_id(str(oid).upper()), str(oid).upper())

    def test_clean_object_id_rejects_everything_else(self):
        from core.utils import clean_object_id

        for junk in ["", "  ", "abc", "12", "all", None, 0, 42,
                     "6a6cf0c46252538ba980884",      # 23 chars
                     "6a6cf0c46252538ba9808843x",    # 25 chars
                     "6a6cf0c46252538ba980884g",     # non-hex
                     "../../etc/passwd"]:
            with self.subTest(junk=junk):
                self.assertIsNone(clean_object_id(junk))

    def test_clean_object_ids_filters_and_preserves_order(self):
        from core.utils import clean_object_ids

        a, b = str(ObjectId()), str(ObjectId())
        self.assertEqual(clean_object_ids([a, "junk", b, "", None]), [a, b])
        self.assertEqual(clean_object_ids(None), [])

    def test_a_hex_id_survives_the_dashboard_filters(self):
        """
        The regression that mattered most: int() used to raise here, the
        ValueError was swallowed, and the filter silently became "no filter"
        so every report quietly showed unfiltered data.
        """
        from django.test import RequestFactory

        from dashboard.filters import ReportFilters

        oid = str(ObjectId())
        request = RequestFactory().get(
            "/", {"batch": oid, "subject": oid, "department": oid,
                  "teacher": oid, "student": oid, "semester": "4"})
        f = ReportFilters.from_request(request)
        for name in ("batch", "subject", "department", "teacher", "student"):
            self.assertEqual(getattr(f, name), oid, f"{name} filter was dropped")
        self.assertEqual(f.semester, 4)          # a genuine integer, still int

    def test_junk_filters_are_ignored_not_fatal(self):
        from django.test import RequestFactory

        from dashboard.filters import ReportFilters

        request = RequestFactory().get("/", {"batch": "wat", "semester": "nope"})
        f = ReportFilters.from_request(request)
        self.assertIsNone(f.batch)
        self.assertIsNone(f.semester)

    def test_all_and_blank_still_mean_no_filter(self):
        from django.test import RequestFactory

        from dashboard.filters import ReportFilters

        request = RequestFactory().get("/", {"batch": "all", "subject": ""})
        f = ReportFilters.from_request(request)
        self.assertIsNone(f.batch)
        self.assertIsNone(f.subject)


class ObjectIdUrlTests(SimpleTestCase):
    """The <oid:...> path converter."""

    def setUp(self):
        if ObjectId is None:
            self.skipTest("bson is not installed")
        from core.converters import ObjectIdConverter

        self.conv = ObjectIdConverter()

    def test_regex_matches_only_24_hex(self):
        import re

        pattern = re.compile(f"^{self.conv.regex}$")
        self.assertTrue(pattern.match(str(ObjectId())))
        self.assertTrue(pattern.match(str(ObjectId()).upper()))
        for junk in ["123", "abc", "6a6cf0c46252538ba980884",
                     "6a6cf0c46252538ba9808843x", "6a6cf0c46252538ba980884g"]:
            with self.subTest(junk=junk):
                self.assertIsNone(pattern.match(junk))

    def test_to_url_accepts_a_real_objectid(self):
        """reverse(..., args=[obj.id]) passes an ObjectId, not a string."""
        oid = ObjectId()
        self.assertEqual(self.conv.to_url(oid), str(oid))
        self.assertEqual(self.conv.to_url(str(oid)), str(oid))
