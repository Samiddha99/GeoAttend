"""
Subject allocation moves from (teacher, subject, batch) to
(teacher, subject, batch, section).

**No backfill, on purpose.** `section = NULL` means *the whole batch* — not
"unknown". That is exactly what every existing allocation already means, so
every existing row is already correct under the new rule, and a college that
never adopts sections never notices this migration happened.

**One plain unique constraint.** The first version of this migration used two
*partial* constraints keyed on `section__isnull`, on the SQL reasoning that
`NULL != NULL` and a composite unique over a nullable column therefore lets
`(t, s, b, NULL)` in twice. MongoDB rejected them outright —
`NotSupportedError: MongoDB does not support the 'isnull' lookup in indexes` —
and it turns out they were never needed here: MongoDB indexes a null or missing
field as a value, so two whole-batch rows for one teacher and subject collide
under an ordinary compound unique index.

**Re-runnable.** The first attempt failed *after* adding the column and
dropping the old index, and a migration that fails is not recorded — so this
runs again over a database that is already half-changed. Adding the column
again is harmless on a schemaless store, but dropping an index that is already
gone raises, so that step is wrapped below.
"""
import django.db.models.deletion
from django.db import migrations, models

OLD_INDEX = "uniq_teacher_subject_batch"


def drop_old_index(apps, schema_editor):
    """
    Drop the pre-section unique index, if it is still there.

    Tolerant of it being gone: the failed first run of this migration dropped
    it, and pymongo raises rather than shrugging when asked to drop an index
    that does not exist. Tolerant of the backend not being MongoDB too, so the
    sqlite test harness passes straight through.
    """
    model = apps.get_model("academics", "TeacherAssignment")
    connection = schema_editor.connection
    database = getattr(connection, "database", None)
    if database is None:                       # not MongoDB — nothing to do
        return
    try:
        database[model._meta.db_table].drop_index(OLD_INDEX)
    except Exception:                                            # noqa: BLE001
        # "index not found" is the expected case on a re-run. Anything else
        # here is also not worth failing a migration over: the index is being
        # removed, and its absence is the desired end state either way.
        pass


def noop(apps, schema_editor):
    """Reversing does not put the old index back; 0016 would have to."""


class Migration(migrations.Migration):

    dependencies = [("academics", "0014_import_job_institute")]

    operations = [
        migrations.AddField(
            model_name="teacherassignment",
            name="section",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="assignments", to="academics.section",
                help_text="Leave empty for the whole batch."),
        ),
        # State and database split: Django's migration state must forget the
        # old constraint, but the actual index drop has to be the forgiving
        # version above.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="teacherassignment",
                    name=OLD_INDEX,
                ),
            ],
            database_operations=[
                migrations.RunPython(drop_old_index, noop),
            ],
        ),
        migrations.AddConstraint(
            model_name="teacherassignment",
            constraint=models.UniqueConstraint(
                fields=("teacher", "subject", "batch", "section"),
                name="uniq_teacher_subject_batch_section"),
        ),
        migrations.AlterModelOptions(
            name="teacherassignment",
            options={"ordering": ["batch__label", "section__name",
                                  "subject__code"]},
        ),
    ]
