"""
Give the catalogue's subject the same Degree and SubjectType lists the
institute's subject has.

They were plain CharFields with string defaults, which meant `get_degree_display()`
returned the raw code and a form built from the model produced a free-text box
where every other screen has a dropdown. The lists now live in `core.enums` —
`academics.catalogue` is imported by `academics.models`, so defining them in the
latter and importing them in the former would be circular, and copying them
would let the two drift apart, which is the drift the codes exist to prevent.

Data-safe: the stored values were already "BACHELOR" and "THEORY".
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("academics",
         "0011_alter_universitybatch_id_alter_universitybatch_label_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="universitysubject",
            name="degree",
            field=models.CharField(
                choices=[("DIPLOMA", "Diploma"), ("BACHELOR", "Bachelor"),
                         ("MASTERS", "Masters")],
                default="BACHELOR", max_length=12),
        ),
        migrations.AlterField(
            model_name="universitysubject",
            name="subject_type",
            field=models.CharField(
                choices=[("THEORY", "Theory"), ("PRACTICAL", "Practical"),
                         ("OTHER", "Other")],
                default="THEORY", max_length=12),
        ),
    ]
