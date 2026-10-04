from datetime import date

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("member_current", [True, False])
def test_populated_history_upgrade_preserves_selected_period_and_source_scopes(member_current):
    # Roll back schema and records together so the suite keeps its latest schema.
    with transaction.atomic():
        try:
            executor = MigrationExecutor(connection)
            before = ("core", "0015_linking_contract")
            executor.migrate([before])
            historical = executor.loader.project_state([before]).apps
            Entity = historical.get_model("core", "Entity")
            Source = historical.get_model("core", "Source")
            Relationship = historical.get_model("core", "Relationship")
            Evidence = historical.get_model("core", "Evidence")
            Member = historical.get_model("core", "ParliamentMember")
            Record = historical.get_model("core", "ParliamentRecord")
            State = historical.get_model("core", "ParliamentImportState")
            as_of = date(2025, 6, 5)
            person = Entity.objects.create(
                name="Pessoa Histórica Fictícia", slug="pessoa-historica-ficticia", kind="person"
            )
            institution = Entity.objects.create(
                name="Assembleia Fictícia",
                slug="assembleia-ficticia",
                kind="organisation",
                classification="parliament",
            )
            source = Source.objects.create(
                title="Fonte Histórica Fictícia", url="https://example.org/fonte-ficticia"
            )
            member = Member.objects.create(
                cadastro_id="fictional-1", entity=person, as_of=as_of, is_current=member_current
            )
            records = []
            for legislature, start, fingerprint in (
                ("XV", date(2022, 3, 29), "1" * 64),
                ("XVII", date(2025, 6, 3), "2" * 64),
            ):
                relationship = Relationship.objects.create(
                    subject=person, object=institution, kind="public_office", start_date=start
                )
                evidence = Evidence.objects.create(
                    relationship=relationship,
                    source=source,
                    excerpt="Mandato inteiramente fictício para testar a migração.",
                )
                records.append(
                    Record.objects.create(
                        member=member,
                        relationship=relationship,
                        evidence=evidence,
                        legislature=legislature,
                        fingerprint=fingerprint,
                        as_of=as_of,
                        data={"fictional": True},
                        roster_url="https://example.org/lista-ficticia",
                        biography_url="https://example.org/biografia-ficticia",
                    )
                )
            member.current_record = records[1]
            member.save(update_fields=["current_record"])
            State.objects.create(
                key="assembly",
                institution=institution,
                roster_source=source,
                biography_source=source,
                as_of=as_of,
            )
            # Fixture writes represent committed legacy data, not migration updates.
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            executor = MigrationExecutor(connection)
            after = ("core", "0016_parliament_full_history")
            executor.migrate([after])
            upgraded = executor.loader.project_state([after]).apps
            Record = upgraded.get_model("core", "ParliamentRecord")
            State = upgraded.get_model("core", "ParliamentImportState")
            assert list(
                Record.objects.order_by("pk").values_list("period_start", "is_current", "data")
            ) == [
                (None, False, {"fictional": True}),
                (date(2025, 6, 3), member_current, {"fictional": True}),
            ]
            assert set(
                State.objects.values_list(
                    "key", "institution_id", "roster_source_id", "biography_source_id", "as_of"
                )
            ) == {
                ("XV", institution.pk, source.pk, source.pk, as_of),
                ("XVII", institution.pk, source.pk, source.pk, as_of),
            }
        finally:
            transaction.set_rollback(True)
