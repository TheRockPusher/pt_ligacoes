"""Backfill the linking model for existing rows, then enforce entity classification.

Uses queryset updates: the documented facts do not change, so publication is not
invalidated. The classification constraint is added only after every existing
entity has a valid classification.
"""

from django.db import migrations, models
from django.utils import timezone

AR_INSTITUTION_ID = "institution:assembleia-da-republica"
AR_ROSTER_TITLE = "Assembleia da República — Informação de Base"
AR_BIOGRAPHY_TITLE = "Assembleia da República — Registo Biográfico"


def backfill(apps, schema_editor):
    Entity = apps.get_model("core", "Entity")
    ParliamentImportState = apps.get_model("core", "ParliamentImportState")
    Relationship = apps.get_model("core", "Relationship")
    Source = apps.get_model("core", "Source")
    SourceIdentity = apps.get_model("core", "SourceIdentity")
    SourceObservation = apps.get_model("core", "SourceObservation")

    Entity.objects.filter(kind="person").update(classification="")
    Entity.objects.filter(kind="company").update(classification="company")
    Entity.objects.filter(kind="university").update(classification="higher_education")
    Entity.objects.filter(kind="organisation").update(classification="other")
    Entity.objects.filter(
        kind="organisation",
        pk__in=SourceIdentity.objects.filter(
            source="government", external_id__startswith="portfolio:"
        ).values("entity_id"),
    ).update(classification="government_department")

    state = ParliamentImportState.objects.filter(key="assembly").first()
    if state is not None:
        Entity.objects.filter(pk=state.institution_id, kind="organisation").update(
            classification="parliament"
        )
        if not SourceIdentity.objects.filter(
            source="parliament", external_id=AR_INSTITUTION_ID
        ).exists():
            SourceIdentity.objects.create(
                source="parliament", external_id=AR_INSTITUTION_ID, entity_id=state.institution_id
            )
        Source.objects.filter(pk=state.roster_source_id).update(dataset="ar_informacao_base")
        Source.objects.filter(pk=state.biography_source_id).update(dataset="ar_registo_biografico")

    ar_sources = Source.objects.filter(dataset="", publisher="Assembleia da República")
    ar_sources.filter(title__startswith=AR_ROSTER_TITLE).update(dataset="ar_informacao_base")
    ar_sources.filter(title__startswith=AR_BIOGRAPHY_TITLE).update(dataset="ar_registo_biografico")
    Source.objects.filter(dataset="", publisher="Governo da República Portuguesa").update(
        dataset="gov_composicao"
    )
    Source.objects.filter(dataset="", publisher="Entidade para a Transparência").update(
        dataset="ept_declaracoes"
    )

    SourceObservation.objects.filter(source="government").update(dataset="gov_composicao")
    SourceObservation.objects.filter(source="ept").update(dataset="ept_declaracoes")
    SourceObservation.objects.filter(source="parliament", category="biography_role").update(
        dataset="ar_registo_biografico"
    )

    Relationship.objects.filter(end_date__lt=timezone.localdate()).update(temporal_status="ended")
    # Fire deferred foreign-key checks now so the constraint below can alter the table.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):
    dependencies = [("core", "0008_linking_model")]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="entity",
            constraint=models.CheckConstraint(
                condition=models.Q(classification="", kind="person")
                | models.Q(classification__in=["company", "state_company"], kind="company")
                | models.Q(classification="higher_education", kind="university")
                | (
                    models.Q(kind="organisation")
                    & ~models.Q(
                        classification__in=["", "company", "state_company", "higher_education"]
                    )
                ),
                name="entity_kind_classification",
            ),
        ),
    ]
