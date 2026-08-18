"""
Which affiliation governs a department.

Blank for every existing row, and deliberately not backfilled. `is_read_only`
treats an unset discipline as "nobody affiliates this", so departments that
existed before this field keep behaving exactly as they did — an institute
that could edit its own subjects yesterday still can today. Guessing a
discipline from a department code would lock some of them out on a hunch, and
the person who knows the answer is the one looking at the Departments screen.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0006_shared_curriculum"),
    ]

    operations = [
        migrations.AddField(
            model_name="department",
            name="discipline",
            field=models.CharField(
                blank=True, db_index=True, max_length=12,
                choices=[
                    ("AGRI", "Agriculture, Veterinary & Allied Sciences"),
                    ("DIPLOMA", "Diploma (Polytechnic & ITI)"),
                    ("ENGG", "Engineering, Technology & Management"),
                    ("GENERAL", "General Courses (Arts, Science, Commerce)"),
                    ("MEDICAL", "Medical, Health Sciences, Ayush, Nursing & Paramedical"),
                    ("PHARMACY", "Pharmacy"),
                ],
                help_text="Which affiliation governs this department. Leave "
                          "blank if it is not tied to one.",
            ),
        ),
    ]
