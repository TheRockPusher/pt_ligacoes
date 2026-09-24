import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils.html import escape

from ligacoes.core.models import Evidence, Relationship, Source
from ligacoes.core.services import publish_relationship

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.org/source",
        "//example.org/source",
        "https://name:password@example.org/source",
        "http://localhost/source",
        "http://localhost.localdomain/source",
        "http://internal.local/source",
        "http://127.0.0.1/source",
        "http://10.0.0.1/source",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/source",
    ],
)
def test_source_rejects_unsafe_external_url(catalog, unsafe_url):
    catalog.source.url = unsafe_url
    with pytest.raises(ValidationError):
        catalog.source.save()
    catalog.source.refresh_from_db()
    assert catalog.source.url == "https://example.org/documento-ficticio"


def test_untrusted_editorial_text_is_escaped_in_html_and_remains_json_data(
    client, catalog, reviewer
):
    payload = '<img src=x onerror="window.__xss=true"> — entidade fictícia'
    excerpt = "</script><script>window.__xss=true</script> — passagem fictícia"
    catalog.company.name = payload
    catalog.company.save()
    catalog.evidence.excerpt = excerpt
    catalog.evidence.save()
    publish_relationship(catalog.relation, reviewer)
    profile = client.get(reverse("public:entity_detail", kwargs={"slug": catalog.company.slug}))
    evidence = client.get(reverse("public:evidence_detail", kwargs={"pk": catalog.evidence.pk}))
    assert str(escape(payload)) in profile.content.decode()
    assert payload not in profile.content.decode()
    assert str(escape(excerpt)) in evidence.content.decode()
    assert excerpt not in evidence.content.decode()
    graph = client.get(reverse("public:graph", kwargs={"slug": catalog.person.slug}))
    assert graph["Content-Type"].startswith("application/json")
    assert (
        next(
            node["data"]["label"]
            for node in graph.json()["nodes"]
            if node["data"]["id"] == str(catalog.company.pk)
        )
        == payload
    )
    assert graph["X-Content-Type-Options"] == "nosniff"


def test_public_routes_reject_cross_site_writes(catalog):
    client = Client(enforce_csrf_checks=True)
    before = (Relationship.objects.count(), Evidence.objects.count(), Source.objects.count())
    urls = [
        reverse("public:index"),
        reverse("public:entity_detail", kwargs={"slug": catalog.person.slug}),
        reverse("public:graph", kwargs={"slug": catalog.person.slug}),
        reverse("public:evidence_detail", kwargs={"pk": catalog.evidence.pk}),
        reverse("public:methodology"),
    ]
    for url in urls:
        response = client.post(url, {"status": "published"}, HTTP_ORIGIN="https://attacker.example")
        assert response.status_code in {403, 405}
    assert (
        Relationship.objects.count(),
        Evidence.objects.count(),
        Source.objects.count(),
    ) == before


def test_admin_is_not_exposed_by_default(client):
    assert client.get("/admin/").status_code == 404
    assert client.get("/admin/login/").status_code == 404


def test_browser_response_has_script_and_frame_boundaries(client):
    response = client.get(reverse("public:index"))
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"
    policy = response["Content-Security-Policy"]
    directives = {
        pieces[0]: set(pieces[1:]) for value in policy.split(";") if (pieces := value.split())
    }
    assert directives["script-src"] == {"'self'"}
    assert directives["object-src"] == {"'none'"}
    assert directives["frame-ancestors"] == {"'none'"}
    assert "'unsafe-eval'" not in policy


def test_health_readiness_does_not_expose_connection_details(client):
    response = client.get("/healthz/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
