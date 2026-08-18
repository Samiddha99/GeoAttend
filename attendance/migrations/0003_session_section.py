"""
A session records which section it was for.

**Recorded, not derived.** Sections get renamed, retired and reorganised, and a
session is a historical fact about who was in a room on a Tuesday. Working the
audience out again from today's allocations would answer a different question
every term.

Null means the whole batch — the same meaning it carries on a TeacherAssignment,
and what every session taken before sections existed already means. So no
backfill: every existing row is already right.

`SET_NULL` rather than `CASCADE`: deleting a section must not delete a term of
attendance. A session that widens to "the whole batch" is a vaguer record than
it was, but a truthful one.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0002_faceverifyticket_manualmarkrequest_and_more"),
        ("academics", "0015_allocation_section"),
    ]

    operations = [
        migrations.AddField(
            model_name="attendancesession",
            name="section",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sessions", to="academics.section"),
        ),
    ]
