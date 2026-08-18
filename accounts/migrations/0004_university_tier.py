"""
The university tier: a second kind of tenant above Institute.

Additive throughout. Two things are worth stating because they are easy to
misread later:

**Existing institutes stay approved.** `Institute.status` defaults to APPROVED
rather than PENDING. Every institute that already exists got in before there
was anyone to ask, and defaulting to PENDING would lock every current head out
of their own account the moment this migration ran.

**No affiliation rows are created.** An institute with no `InstituteAffiliation`
rows has declared no disciplines, which is exactly the state of every existing
row. It is not the same as autonomous — autonomous is a row with a NULL
university — and the difference matters, because "autonomous in engineering"
is a claim and "we never asked" is not.
"""
import django.db.models.deletion
import django_mongodb_backend.fields
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_guardian_login"),
    ]

    operations = [
        migrations.CreateModel(
            name="University",
            fields=[
                ("id", django_mongodb_backend.fields.ObjectIdAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name="ID")),
                ("name", models.CharField(max_length=200, unique=True)),
                ("short_name", models.CharField(blank=True, max_length=40)),
                ("code", models.SlugField(max_length=30, unique=True)),
                ("email", models.EmailField(
                    help_text="Official university email", max_length=254,
                    unique=True)),
                ("phone", models.CharField(blank=True, max_length=20)),
                ("website", models.URLField(blank=True)),
                ("address", models.TextField(blank=True)),
                ("state", models.CharField(blank=True, max_length=60)),
                ("district", models.CharField(blank=True, max_length=80)),
                ("logo", models.ImageField(blank=True, null=True,
                                           upload_to="university/")),
                ("grants_affiliation", models.BooleanField(
                    default=True,
                    help_text="Institutes may name this body as their affiliating "
                              "university. Turn off for a university that only "
                              "takes the institutes it invites.")),
                ("is_active", models.BooleanField(default=True)),
                ("is_seeded", models.BooleanField(default=False)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name_plural": "Universities", "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="UniversityDiscipline",
            fields=[
                ("id", django_mongodb_backend.fields.ObjectIdAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name="ID")),
                ("discipline", models.CharField(
                    choices=[("AGRI", "Agriculture, Veterinary & Allied Sciences"),
                             ("DIPLOMA", "Diploma (Polytechnic & ITI)"),
                             ("ENGG", "Engineering, Technology & Management"),
                             ("GENERAL", "General Courses (Arts, Science, Commerce)"),
                             ("MEDICAL", "Medical, Health Sciences, Ayush, Nursing & Paramedical"),
                             ("PHARMACY", "Pharmacy")],
                    db_index=True, max_length=12)),
                ("university", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="disciplines", to="accounts.university")),
            ],
            options={"ordering": ["discipline"]},
        ),
        migrations.AddConstraint(
            model_name="universitydiscipline",
            constraint=models.UniqueConstraint(
                fields=("university", "discipline"),
                name="uniq_university_discipline"),
        ),
        # --- institute gains a place, a status and an inviter ---------------
        migrations.AddField(
            model_name="institute",
            name="state",
            field=models.CharField(blank=True, db_index=True, max_length=60),
        ),
        migrations.AddField(
            model_name="institute",
            name="district",
            field=models.CharField(blank=True, db_index=True, max_length=80),
        ),
        migrations.AddField(
            model_name="institute",
            name="status",
            field=models.CharField(
                choices=[("PENDING", "Awaiting approval"),
                         ("APPROVED", "Approved"), ("REJECTED", "Rejected")],
                db_index=True, default="APPROVED", max_length=10),
        ),
        migrations.AddField(
            model_name="institute",
            name="rejection_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="institute",
            name="decided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="institute",
            name="decided_by",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="institute_decisions", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="institute",
            name="invited_by",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="invited_institutes", to="accounts.university"),
        ),
        migrations.CreateModel(
            name="InstituteAffiliation",
            fields=[
                ("id", django_mongodb_backend.fields.ObjectIdAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name="ID")),
                ("discipline", models.CharField(
                    choices=[("AGRI", "Agriculture, Veterinary & Allied Sciences"),
                             ("DIPLOMA", "Diploma (Polytechnic & ITI)"),
                             ("ENGG", "Engineering, Technology & Management"),
                             ("GENERAL", "General Courses (Arts, Science, Commerce)"),
                             ("MEDICAL", "Medical, Health Sciences, Ayush, Nursing & Paramedical"),
                             ("PHARMACY", "Pharmacy")],
                    db_index=True, max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("institute", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="affiliations", to="accounts.institute")),
                ("university", models.ForeignKey(
                    blank=True, null=True,
                    help_text="Blank means autonomous for this discipline.",
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="affiliated_institutes", to="accounts.university")),
            ],
            options={"ordering": ["discipline"]},
        ),
        migrations.AddConstraint(
            model_name="instituteaffiliation",
            constraint=models.UniqueConstraint(
                fields=("institute", "discipline"),
                name="uniq_institute_discipline"),
        ),
        # --- the new role, and the link from a user to their university -----
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("HEAD", "Head of Institute"), ("HOD", "Head of Department"),
                         ("TEACHER", "Teacher"), ("STUDENT", "Student"),
                         ("GUARDIAN", "Guardian"), ("UNIVERSITY", "University")],
                db_index=True, max_length=12),
        ),
        migrations.AlterField(
            model_name="invitation",
            name="role",
            field=models.CharField(
                choices=[("HEAD", "Head of Institute"), ("HOD", "Head of Department"),
                         ("TEACHER", "Teacher"), ("STUDENT", "Student"),
                         ("GUARDIAN", "Guardian"), ("UNIVERSITY", "University")],
                max_length=12),
        ),
        migrations.AddField(
            model_name="user",
            name="university",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="users", to="accounts.university"),
        ),
    ]
