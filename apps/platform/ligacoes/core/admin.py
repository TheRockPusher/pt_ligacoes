from collections.abc import Sequence
from typing import ClassVar

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError

from .models import Entity, Evidence, Relationship, ReviewEvent, Source
from .services import publish_relationship


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "is_public")
    list_filter = ("kind", "is_public")
    search_fields = ("name", "slug")
    prepopulated_fields: ClassVar[dict[str, Sequence[str]]] = {"slug": ("name",)}
    actions = None


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("title", "publisher", "is_public", "retrieved_at")
    list_filter = ("is_public",)
    search_fields = ("title", "publisher")
    actions = None


class EvidenceInline(admin.TabularInline):
    model = Evidence
    extra = 0
    autocomplete_fields = ("source",)
    fields = ("source", "excerpt", "page_reference", "is_public")


@admin.register(Relationship)
class RelationshipAdmin(admin.ModelAdmin):
    list_display = ("subject", "kind", "object", "status", "reviewed_at")
    list_filter = ("status", "kind")
    search_fields = ("subject__name", "object__name", "description")
    autocomplete_fields = ("subject", "object")
    readonly_fields = ("status", "reviewed_by", "reviewed_at")
    inlines = (EvidenceInline,)
    actions = ("publish_selected",)

    def has_publish_permission(self, request):
        return request.user.is_active and request.user.has_perm("core.publish_relationship")

    @admin.action(description="Rever e publicar relações selecionadas", permissions=["publish"])
    def publish_selected(self, request, queryset):
        if queryset.count() > 100:
            self.message_user(
                request, "Reveja no máximo 100 relações por ação.", level=messages.ERROR
            )
            return
        published = 0
        for relationship in queryset.order_by("pk"):
            try:
                publish_relationship(relationship, request.user)
            except (PermissionDenied, ValidationError) as exc:
                self.message_user(
                    request,
                    f"Não foi possível publicar {relationship}: {exc}",
                    level=messages.ERROR,
                )
            else:
                published += 1
        if published:
            self.message_user(
                request, f"{published} relações revistas e publicadas.", level=messages.SUCCESS
            )


@admin.register(Evidence)
class EvidenceAdmin(admin.ModelAdmin):
    list_display = ("source", "relationship", "is_public")
    list_filter = ("is_public",)
    autocomplete_fields = ("source", "relationship")
    search_fields = ("source__title", "excerpt")
    actions = None


@admin.register(ReviewEvent)
class ReviewEventAdmin(admin.ModelAdmin):
    list_display = ("relationship", "reviewer", "action", "created_at")
    list_filter = ("action",)
    readonly_fields = ("relationship", "reviewer", "action", "created_at")
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
