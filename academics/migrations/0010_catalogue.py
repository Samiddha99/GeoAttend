"""
The university's catalogue, and the links from an institute's rows back to it.

**Existing rows are grandfathered, not converted.** Every department an
institute created in an affiliated discipline keeps working and stays editable;
it simply carries `is_legacy` so a screen can say why it behaves differently
from a department adopted from the catalogue beside it.

Converting them was the alternative and it is worse: a university would inherit
a catalogue assembled from whatever its colleges happened to type, typos and
near-duplicates included, and every one of those would then be published back
out to the others. Locking them is worse still — any college with attendance
running against a department would be stranded until somebody re-adopted it,
mid-term.

So nothing here rewrites a single existing row's behaviour. It adds three empty
tables, three nullable links, and one flag whose only job is to explain.
"""
from django.db import migrations, models
import django.db.models.deletion

from core.enums import status_field


def mark_legacy(apps, schema_editor):
    """
    Flag the departments the new rules would not allow to be created now.

    Only ones in a discipline an affiliating university holds — an autonomous
    department is the institute's by right, then and now, and marking it would
    be saying something untrue about it.
    """
    Department = apps.get_model("academics", "Department")
    InstituteAffiliation = apps.get_model("accounts", "InstituteAffiliation")

    affiliated = {(a.institute_id, a.discipline)
                  for a in InstituteAffiliation.objects.exclude(university=None)}
    legacy = [d.pk for d in Department.objects.exclude(discipline="")
              if (d.institute_id, d.discipline) in affiliated]
    if legacy:
        Department.objects.filter(pk__in=legacy).update(is_legacy=True)


def unmark(apps, schema_editor):
    apps.get_model("academics", "Department").objects.filter(
        is_legacy=True).update(is_legacy=False)


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0009_status_and_revoked"),
        ("accounts", "0006_user_status_and_revoked"),
    ]

    operations = [
        migrations.CreateModel(
            name="UniversityDepartment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("discipline", models.CharField(db_index=True, max_length=12)),
                ("name", models.CharField(max_length=120)),
                ("code", models.CharField(max_length=20)),
                ("status", status_field()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("university", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="catalogue_departments", to="accounts.university")),
            ],
            options={"ordering": ["discipline", "code"]},
        ),
        migrations.CreateModel(
            name="UniversityBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("label", models.CharField(max_length=12)),
                ("start_year", models.PositiveIntegerField()),
                ("end_year", models.PositiveIntegerField()),
                ("status", status_field()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("department", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="batches", to="academics.universitydepartment")),
            ],
            options={"ordering": ["-start_year", "label"]},
        ),
        migrations.CreateModel(
            name="UniversitySubject",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=20)),
                ("name", models.CharField(max_length=150)),
                ("degree", models.CharField(default="BACHELOR", max_length=10)),
                ("subject_type", models.CharField(default="THEORY", max_length=10)),
                ("semester", models.PositiveSmallIntegerField(default=1)),
                ("credits", models.PositiveSmallIntegerField(default=4)),
                ("status", status_field()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("department", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="subjects", to="academics.universitydepartment")),
            ],
            options={"ordering": ["semester", "code"]},
        ),
        migrations.AddConstraint(
            model_name="universitydepartment",
            constraint=models.UniqueConstraint(
                fields=("university", "discipline", "code"),
                name="uniq_catalogue_dept"),
        ),
        migrations.AddConstraint(
            model_name="universitybatch",
            constraint=models.UniqueConstraint(
                fields=("department", "label"), name="uniq_catalogue_batch"),
        ),
        migrations.AddConstraint(
            model_name="universitysubject",
            constraint=models.UniqueConstraint(
                fields=("department", "code"), name="uniq_catalogue_subject"),
        ),
        migrations.AddField(
            model_name="department", name="source",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="adoptions", to="academics.universitydepartment"),
        ),
        migrations.AddField(
            model_name="department", name="is_legacy",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="batch", name="source",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="adoptions", to="academics.universitybatch"),
        ),
        migrations.AddField(
            model_name="subject", name="source",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="adoptions", to="academics.universitysubject"),
        ),
        migrations.RunPython(mark_legacy, unmark),
    ]
