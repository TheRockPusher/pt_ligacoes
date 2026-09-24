from django.urls import path

from . import views

app_name = "public"
urlpatterns = [
    path("", views.index, name="index"),
    path("entidades/<slug:slug>/", views.entity_detail, name="entity_detail"),
    path("entidades/<slug:slug>/grafo/", views.graph, name="graph"),
    path("evidencias/<uuid:pk>/", views.evidence_detail, name="evidence_detail"),
    path("metodologia/", views.methodology, name="methodology"),
]
