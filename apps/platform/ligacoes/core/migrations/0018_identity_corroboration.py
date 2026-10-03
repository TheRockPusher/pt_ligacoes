import re
import unicodedata

from django.db import migrations, models, transaction

# Freeze the canonical-name normalisation used at this schema cutover.
HONORIFICS = {
    "arq",
    "arqa",
    "arquiteta",
    "arquiteto",
    "doutor",
    "doutora",
    "dr",
    "dra",
    "eng",
    "enga",
    "engo",
    "engenheira",
    "engenheiro",
    "exma",
    "exmo",
    "juiz",
    "juiza",
    "mestre",
    "prof",
    "profa",
    "professor",
    "professora",
    "sr",
    "sra",
}


def backfill_names(apps, schema_editor):
    entity_model = apps.get_model("core", "Entity")
    entities = entity_model.objects.using(schema_editor.connection.alias)
    batch = []
    for entity in entities.only("pk", "name").order_by("pk").iterator(chunk_size=1000):
        joined = re.sub(r"\.\s*([ªº])", r"\1", entity.name)
        plain = "".join(
            char
            for char in unicodedata.normalize("NFKD", joined)
            if not unicodedata.combining(char)
        )
        tokens = re.sub(r"[\W_]+", " ", plain.casefold()).split()
        while tokens and tokens[0] in HONORIFICS:
            tokens.pop(0)
        entity.normalised_name = " ".join(tokens)
        batch.append(entity)
        if len(batch) == 1000:
            with transaction.atomic(using=schema_editor.connection.alias):
                entities.bulk_update(batch, ["normalised_name"], batch_size=1000)
            batch.clear()
    if batch:
        with transaction.atomic(using=schema_editor.connection.alias):
            entities.bulk_update(batch, ["normalised_name"], batch_size=1000)


class Migration(migrations.Migration):
    # Release AddField's table lock before the batched data backfill starts.
    atomic = False

    dependencies = [("core", "0017_event_summary_performance")]

    operations = [
        migrations.AddField(
            model_name="entity",
            name="normalised_name",
            field=models.CharField(
                max_length=300,
                db_index=True,
                editable=False,
                blank=True,
            ),
        ),
        migrations.RunPython(backfill_names, migrations.RunPython.noop),
    ]
