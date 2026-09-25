from collections.abc import Sequence
from typing import ClassVar

from django.contrib import admin, messages
from django.contrib.admin.views.autocomplete import AutocompleteJsonView
from django.contrib.admin.widgets import AutocompleteSelect
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html_join

from .enrichment import (
    ObservationReviewForm,
    backfill_biography_roles,
    convert_observation,
)
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
    SourceApproval,
    SourceIdentity,
    SourceObservation,
    editorial_transaction,
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
    actions = ("extract_biography_roles",)

    def has_extract_permission(self, request):
        return request.user.is_active and request.user.has_perm("core.review_sourceobservation")

    @admin.action(
        description="Extrair candidatas profissionais das biografias retidas",
        permissions=["extract"],
    )
    def extract_biography_roles(self, request, queryset):
        if queryset.count() > 100:
            self.message_user(
                request, "Selecione no máximo 100 observações por ação.", level=messages.ERROR
            )
            return
        try:
            result = backfill_biography_roles(queryset.order_by("pk"), request.user)
        except (PermissionDenied, ValidationError) as exc:
            self.message_user(
                request, f"Não foi possível extrair as candidatas: {exc}", level=messages.ERROR
            )
        else:
            self.message_user(
                request,
                f"{result['created']} candidatas privadas criadas. Reveja a organização, o tipo e as datas antes de converter em rascunho.",
                level=messages.SUCCESS,
            )


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


@admin.register(SourceApproval)
class SourceApprovalAdmin(admin.ModelAdmin):
    list_display = ("source", "is_active", "review_due_at", "approved_by", "approved_at")
    readonly_fields = ("approved_by", "approved_at")
    actions = None

    def has_add_permission(self, request):
        return request.user.is_active and request.user.has_perm("core.approve_sourceapproval")

    def has_change_permission(self, request, obj=None):
        return self.has_add_permission(request)

    def has_view_permission(self, request, obj=None):
        return self.has_add_permission(request) or super().has_view_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not self.has_add_permission(request):
            raise PermissionDenied
        obj.approved_by = request.user
        obj.approved_at = timezone.now()
        super().save_model(request, obj, form, change)


class IdentityEntitySelect(AutocompleteSelect):
    url_name = "%s:core_sourceidentity_entity_picker"


class IdentityEntityPicker(AutocompleteJsonView):
    def get(self, request, *args, **kwargs):
        if (
            request.GET.get("app_label"),
            request.GET.get("model_name"),
            request.GET.get("field_name"),
        ) != ("core", "sourceidentity", "entity"):
            raise PermissionDenied
        return super().get(request, *args, **kwargs)

    def has_perm(self, request, obj=None):
        return request.user.is_active and request.user.has_perm("core.review_sourceidentity")

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(kind__in=(Entity.Kind.PERSON, Entity.Kind.ORGANISATION))
            .only("id", "name")
        )


@admin.register(SourceIdentity)
class SourceIdentityAdmin(admin.ModelAdmin):
    list_display = ("source", "external_id", "entity", "reviewed_by", "used_at")
    list_filter = ("source",)
    search_fields = ("external_id", "entity__name")
    autocomplete_fields = ("entity",)
    readonly_fields = ("reviewed_by", "reviewed_at", "used_at")
    actions = None

    def get_urls(self):
        return [
            path(
                "entity-picker/",
                self.admin_site.admin_view(
                    IdentityEntityPicker.as_view(admin_site=self.admin_site)
                ),
                name="core_sourceidentity_entity_picker",
            ),
            *super().get_urls(),
        ]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "entity":
            kwargs["widget"] = IdentityEntitySelect(db_field, self.admin_site)
            kwargs["queryset"] = Entity.objects.filter(
                kind__in=(Entity.Kind.PERSON, Entity.Kind.ORGANISATION)
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.used_at is not None:
            return (*self.readonly_fields, "source", "external_id", "entity")
        return self.readonly_fields

    def has_add_permission(self, request):
        return request.user.is_active and request.user.has_perm("core.review_sourceidentity")

    def has_change_permission(self, request, obj=None):
        return self.has_add_permission(request)

    def has_view_permission(self, request, obj=None):
        return self.has_add_permission(request) or super().has_view_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change=change, **kwargs)
        if "review_notes" in form.base_fields:
            form.base_fields["review_notes"].required = True
        return form

    def save_model(self, request, obj, form, change):
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        obj.reviewed_by = request.user
        obj.reviewed_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(SourceObservation)
class SourceObservationAdmin(admin.ModelAdmin):
    form = ObservationReviewForm
    list_display = ("identity", "category", "as_of", "is_current", "relationship", "reviewed_at")
    list_filter = ("source", "category", "is_current")
    search_fields = ("identity__entity__name", "external_id", "passage")
    list_select_related = ("identity__entity", "relationship__subject", "relationship__object")
    readonly_fields = (
        "source",
        "scope",
        "external_id",
        "revision",
        "identity",
        "category",
        "passage",
        "source_url",
        "publisher",
        "reference",
        "title",
        "effective_start",
        "effective_end",
        "declared_on",
        "object",
        "kind",
        "as_of",
        "retrieved_at",
        "is_current",
        "relationship",
        "evidence",
        "reviewed_by",
        "reviewed_at",
        "review_notes",
    )
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return (
            request.user.is_active
            and request.user.has_perm("core.review_sourceobservation")
            and (obj is None or obj.is_current)
        )

    def has_view_permission(self, request, obj=None):
        return (
            request.user.is_active and request.user.has_perm("core.review_sourceobservation")
        ) or super().has_view_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_fields(self, request, obj=None) -> tuple[str, ...]:
        if not self.has_change_permission(request, obj):
            return tuple(self.readonly_fields)
        return (
            *self.readonly_fields,
            "reviewed_object",
            "reviewed_kind",
            "reviewed_start",
            "reviewed_end",
            "reviewed_description",
            "conversion_notes",
        )

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST":
            with editorial_transaction():
                return super().changeform_view(request, object_id, form_url, extra_context)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        relationship = convert_observation(obj, request.user, **form.conversion_values())
        obj.refresh_from_db()
        self.message_user(
            request,
            f"Rascunho preparado: {relationship}. A conversão não publica a relação nem a evidência.",
            level=messages.SUCCESS,
        )
