from django.urls import path

from . import paths, sources, views

app_name = "public"
urlpatterns = [
    path("", views.index, name="index"),
    path("entidades/<slug:slug>/", views.entity_detail, name="entity_detail"),
    path("entidades/<slug:slug>/grafo/", views.graph, name="graph"),
    path("entidades/<slug:slug>/eventos/", views.entity_events, name="entity_events"),
    path("evidencias/<uuid:pk>/", views.evidence_detail, name="evidence_detail"),
    path("fontes/", sources.sources_index, name="sources"),
    path("caminhos/", paths.path_finder, name="path_finder"),
    path("caminhos/entidades/", paths.entity_options, name="path_entities"),
    path("metodologia/", views.methodology, name="methodology"),
]
