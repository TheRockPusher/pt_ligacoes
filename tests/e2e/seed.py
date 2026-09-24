"""Seed fictional browser fixtures into an explicitly disposable E2E database."""

import os
from datetime import UTC, date, datetime

import django
from django.conf import settings
from django.db import transaction


def main():
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings.test":
        raise SystemExit("Browser fixtures require config.settings.test.")
    django.setup()
    if not settings.DATABASES["default"]["NAME"].endswith("_e2e"):
        raise SystemExit("Browser fixtures require a dedicated database ending in _e2e.")

    from django.contrib.auth.models import Permission, User

    from ligacoes.core.models import Entity, Evidence, Relationship, Source
    from ligacoes.core.services import publish_relationship

    with transaction.atomic():
        reviewer, _ = User.objects.get_or_create(username="e2e-fictional-reviewer")
        reviewer.is_active = True
        reviewer.is_staff = False
        reviewer.is_superuser = False
        reviewer.set_unusable_password()
        reviewer.save()
        reviewer.user_permissions.set(
            [
                Permission.objects.get(
                    content_type__app_label="core", codename="publish_relationship"
                )
            ]
        )
        person, _ = Entity.objects.update_or_create(
            slug="e2e-pessoa-alfa-ficticia",
            defaults={
                "name": "Pessoa Alfa — personagem fictícia",
                "kind": "person",
                "is_public": True,
                "description": "Perfil sintético para testes de navegação.",
                "private_notes": "PRIVATE_BROWSER_ENTITY_CANARY",
            },
        )
        company, _ = Entity.objects.update_or_create(
            slug="e2e-companhia-beta-ficticia",
            defaults={
                "name": "Companhia Beta — entidade fictícia",
                "kind": "company",
                "is_public": True,
            },
        )
        uncertain, _ = Entity.objects.update_or_create(
            slug="e2e-laboratorio-gama-ficticio",
            defaults={
                "name": '<img src=x onerror="window.__xss=true"> — entidade fictícia',
                "kind": "organisation",
                "is_public": True,
            },
        )
        source, _ = Source.objects.update_or_create(
            url="https://example.org/e2e-documento-ficticio",
            defaults={
                "title": "Documento de demonstração fictício",
                "publisher": "Arquivo fictício",
                "retrieved_at": datetime(2025, 1, 15, 12, tzinfo=UTC),
                "is_public": True,
                "private_notes": "PRIVATE_BROWSER_SOURCE_CANARY",
            },
        )
        for endpoint, kind, description, start, end in [
            (
                company,
                "employment",
                "Emprego de demonstração fictício.",
                date(2019, 1, 1),
                date(2021, 12, 31),
            ),
            (uncertain, "membership", "Participação fictícia com datas desconhecidas.", None, None),
        ]:
            relationship, _ = Relationship.objects.get_or_create(
                subject=person,
                object=endpoint,
                kind=kind,
                defaults={"description": description, "start_date": start, "end_date": end},
            )
            relationship.description = description
            relationship.start_date = start
            relationship.end_date = end
            relationship.save()
            Evidence.objects.update_or_create(
                relationship=relationship,
                source=source,
                defaults={
                    "excerpt": "Passagem inteiramente fictícia: não descreve pessoas reais.",
                    "page_reference": "p. 3",
                    "is_public": True,
                },
            )
            relationship.refresh_from_db()
            if relationship.status != "published":
                publish_relationship(relationship, reviewer)
        private, _ = Entity.objects.update_or_create(
            slug="e2e-pessoa-reservada-ficticia",
            defaults={"name": "Pessoa reservada fictícia", "kind": "person", "is_public": False},
        )
        Relationship.objects.get_or_create(
            subject=person,
            object=private,
            kind="membership",
            defaults={"description": "PRIVATE_BROWSER_DRAFT_CANARY"},
        )
    print("Fictional browser fixtures prepared in the dedicated E2E database.")


if __name__ == "__main__":
    main()
