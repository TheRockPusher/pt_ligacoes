from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0016_parliament_full_history")]

    operations = [
        migrations.AddIndex(
            model_name="evententitysummary",
            index=models.Index(fields=["dataset"], name="event_entity_dataset_idx"),
        ),
        migrations.AddIndex(
            model_name="eventpairsummary",
            index=models.Index(fields=["dataset"], name="event_pair_dataset_idx"),
        ),
    ]
