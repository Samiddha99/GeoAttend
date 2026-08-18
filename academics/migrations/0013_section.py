"""
Sections — a subdivision of one batch — and the student's link to one.

**Scoped to a batch, not to a department.** Section A of 2022-26 and section A
of 2023-27 are different groups of people who share a letter. One row meaning
both would let the first cohort to graduate take the other's students with it.

**The student's link is nullable and `SET_NULL`.** Every student who predates
this has no section, and requiring one would make those rows unsaveable — the
same reasoning as `class_roll`. Deleting a section must not delete the people
in it; they become unsectioned, which is a state every screen already renders
because it is what every existing student looks like.

No backfill. There is no honest way to guess which section anybody was in, and
inventing one would be worse than leaving it empty: an import or the edit form
fills it in with something a person actually knows.
"""
import django.db.models.deletion
import django_mongodb_backend.fields
from django.db import migrations, models

import core.enums


class Migration(migrations.Migration):

    dependencies = [("academics", "0012_catalogue_subject_choices")]

    operations = [
        migrations.CreateModel(
            name="Section",
            fields=[
                # The project's primary key type — see any other model here.
                ("id", django_mongodb_backend.fields.ObjectIdAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name="ID")),
                ("name", models.CharField(help_text='e.g. "A"', max_length=20)),
                ("is_active", models.BooleanField(default=True)),
                ("status", core.enums.status_field()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("batch", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="sections", to="academics.batch")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddConstraint(
            model_name="section",
            constraint=models.UniqueConstraint(
                fields=("batch", "name"), name="uniq_section_per_batch"),
        ),
        migrations.AddField(
            model_name="studentprofile",
            name="section",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="students", to="academics.section"),
        ),
    ]
