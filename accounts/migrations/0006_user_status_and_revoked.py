"""
The people half of the status/revoked split.

Its twin is academics/0009, which also does the backfill for both — a single
`RunPython` can see every model, and splitting the data migration across two
apps would leave a window where half the rows had been reconstructed.
"""
from django.db import migrations

from core.enums import revoked_field, status_field


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_user_archived_with_discipline"),
    ]

    operations = [
        migrations.AddField(model_name="user", name="status",
                            field=status_field()),
        migrations.AddField(model_name="user", name="is_revoked",
                            field=revoked_field()),
    ]
