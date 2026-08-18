"""
A university's suspension of a teacher: a flag, a reason, a date and the body
that imposed it.

**Four fields rather than a new `status` value.** `status` is the institute's
fact about the row, `is_revoked` is the discipline's, and this is the
university's. Folding any of them together is the mistake recorded at the top
of core/enums.py — a suspension written over `status` would leave nothing to
restore when it was lifted, and no screen could tell "the institute archived
them" from "the university barred them".

No backfill: `is_suspended` defaults to False, which is true of every existing
row, and the three companion fields are meaningless until it is set.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("accounts", "0006_user_status_and_revoked")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="is_suspended",
            field=models.BooleanField(
                default=False, db_index=True,
                help_text="Barred by the affiliating university. Independent "
                          "of status: a suspended teacher keeps whatever "
                          "status they had."),
        ),
        migrations.AddField(
            model_name="user",
            name="suspension_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="user",
            name="suspended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="suspended_by",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="suspended_teachers",
                to="accounts.university"),
        ),
    ]
