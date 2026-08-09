"""
Guardian sign-in: a new role, the number that identifies the account, and the
WhatsApp one-time codes.

Additive throughout. The GUARDIAN role is a new choice on an existing column,
so no stored value changes; `guardian_mobile` is nullable and unique, which on
an empty column means every existing row holds NULL and the unique index has
nothing to collide on.

Note the field is NULL rather than "" for non-guardians. A unique column can
hold any number of NULLs but only one empty string, so a default of "" would
make the second staff account unsaveable.
"""
import django.utils.timezone
import django_mongodb_backend.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_user_face_enrolled_faceenrolment_facesample"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("HEAD", "Head of Institute"), ("HOD", "Head of Department"),
                         ("TEACHER", "Teacher"), ("STUDENT", "Student"),
                         ("GUARDIAN", "Guardian")],
                db_index=True, max_length=10),
        ),
        # Invitation.role reuses User.Role.choices, so its stored choices
        # change too. Nothing is ever invited as a guardian — the account is
        # derived from the student record — but the field has to agree with the
        # model or every later autodetect run reports a phantom change.
        migrations.AlterField(
            model_name="invitation",
            name="role",
            field=models.CharField(
                choices=[("HEAD", "Head of Institute"), ("HOD", "Head of Department"),
                         ("TEACHER", "Teacher"), ("STUDENT", "Student"),
                         ("GUARDIAN", "Guardian")],
                max_length=10),
        ),
        migrations.AddField(
            model_name="user",
            name="guardian_mobile",
            field=models.CharField(
                blank=True, db_index=True,
                help_text="Set only on guardian accounts. The number is the login.",
                max_length=20, null=True, unique=True),
        ),
        migrations.CreateModel(
            name="PhoneOTP",
            fields=[
                ("id", django_mongodb_backend.fields.ObjectIdAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name="ID")),
                ("mobile", models.CharField(db_index=True, max_length=20)),
                ("purpose", models.CharField(
                    choices=[("GLOGIN", "Guardian sign-in")],
                    default="GLOGIN", max_length=10)),
                ("code_hash", models.CharField(max_length=64)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("sends", models.PositiveSmallIntegerField(default=1)),
                ("is_used", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_sent_at", models.DateTimeField(
                    default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="phoneotp",
            index=models.Index(fields=["mobile", "purpose", "is_used"],
                               name="accounts_ph_mobile_17001f_idx"),
        ),
    ]
