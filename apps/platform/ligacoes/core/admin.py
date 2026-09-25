from collections.abc import Sequence
from typing import ClassVar

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html_join

from .import_forms import ImportRequestForm
from .import_jobs import ImportBusy, ImportConflict, enqueue_import
from .models import (
    Entity,
    Evidence,
    ImportRun,
    ParliamentImportState,
    ParliamentMember,
    ParliamentRecord,
    Relationship,
    ReviewEvent,
    Source,
)
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


class ParliamentReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ParliamentMember)
class ParliamentMemberAdmin(ParliamentReadOnlyAdmin):
    list_display = ("cadastro_id", "entity", "is_current", "as_of")
    list_filter = ("is_current",)
    search_fields = ("cadastro_id", "entity__name")
    list_select_related = ("entity",)


@admin.register(ParliamentRecord)
class ParliamentRecordAdmin(ParliamentReadOnlyAdmin):
    list_display = ("member", "legislature", "as_of", "retrieved_at", "relationship")
    list_filter = ("legislature",)
    search_fields = ("member__cadastro_id", "member__entity__name")
    list_select_related = ("member__entity", "relationship__subject", "relationship__object")


@admin.register(ParliamentImportState)
class ParliamentImportStateAdmin(ParliamentReadOnlyAdmin):
    list_display = ("key", "as_of")


@admin.register(ImportRun)
class ImportRunAdmin(ParliamentReadOnlyAdmin):
    change_list_template = "admin/core/importrun/change_list.html"
    change_form_template = "admin/core/importrun/change_form.html"
    list_display = ("id", "mode", "status", "legislature", "as_of", "created_at", "requested_by")
    list_filter = ("status", "mode", "origin")
    list_select_related = ("requested_by",)
    ordering = ("-created_at",)
    readonly_fields = (
        "id",
        "mode",
        "status",
        "legislature",
        "as_of",
        "origin",
        "requested_by",
        "created_at",
        "started_at",
        "finished_at",
        "result_summary",
        "error",
    )
    fields = readonly_fields

    def has_view_permission(self, request, obj=None):
        return (
            request.user.is_active
            and request.user.is_staff
            and (
                request.user.has_perm("core.view_importrun")
                or request.user.has_perm("core.run_import")
            )
        )

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_run_permission(self, request):
        return (
            request.user.is_active
            and request.user.is_staff
            and request.user.has_perm("core.run_import")
        )

    def get_urls(self):
        return [
            path(
                "request/",
                self.admin_site.admin_view(self.request_import),
                name="core_importrun_request",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(
            request,
            extra_context={
                **(extra_context or {}),
                "can_run_import": self.has_run_permission(request),
            },
        )

    @admin.display(description="Resultado")
    def result_summary(self, obj):
        if not obj.result:
            return "Ainda sem resultado."
        labels = (
            ("serving", "Deputados em funções validados"),
            ("created_members", "Novos deputados guardados"),
            ("created_records", "Novas revisões de fontes guardadas"),
            ("ceased_members", "Deputados que deixaram de estar em funções"),
        )
        return format_html_join(
            "",
            "<p><strong>{}:</strong> {}</p>",
            ((label, obj.result[key]) for key, label in labels if key in obj.result),
        )

    def request_import(self, request):
        if not self.has_run_permission(request):
            raise PermissionDenied
        if request.method not in {"GET", "POST"}:
            return HttpResponseNotAllowed(["GET", "POST"])
        form = ImportRequestForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            try:
                run = enqueue_import(
                    request_id=form.cleaned_data["request_id"],
                    mode=form.cleaned_data["mode"],
                    legislature=form.cleaned_data["legislature"],
                    as_of=form.cleaned_data["as_of"],
                    requested_by=request.user,
                    origin="admin",
                    confirm_apply=form.cleaned_data["confirm_apply"],
                )
            except ImportBusy:
                form.add_error(
                    None,
                    "Já existe uma importação em espera ou em execução. "
                    "Consulte o histórico e volte a tentar quando terminar.",
                )
            except ImportConflict:
                form.add_error(
                    None,
                    "Este identificador já pertence a outro pedido. "
                    "Abra uma nova importação para usar opções diferentes.",
                )
            except ValidationError:
                form.add_error(None, "Não foi possível validar o pedido. Reveja as opções.")
            else:
                return HttpResponseRedirect(reverse("admin:core_importrun_change", args=[run.pk]))
        if form.errors:
            focus = next(
                (field for field in form.visible_fields() if field.errors),
                form["mode"],
            )
            focus.field.widget.attrs["autofocus"] = True
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Nova importação parlamentar",
            "form": form,
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(request, "admin/core/importrun/request.html", context)
