from collections.abc import Sequence
from typing import ClassVar

from django import forms
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
from .events import withdraw_event
from .identity import accept_suggestion, reject_suggestion
from .import_forms import ImportRequestForm
from .import_jobs import ImportBusy, ImportConflict, enqueue_import
from .models import (
    Entity,
    Event,
    EventParty,
    Evidence,
    IdentitySuggestion,
    ImportRun,
    ParliamentImportState,
    ParliamentMember,
    ParliamentRecord,
    Relationship,
    ReviewEvent,
    Source,
    SourceIdentity,
    SourceObservation,
    Term,
    editorial_transaction,
)
from .services import publish_relationship, withdraw_relationship

ACTION_LIMIT = 100


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "classification", "is_public")
    list_filter = ("kind", "classification", "is_public")
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
    actions = ("publish_selected", "withdraw_selected")

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

    @admin.action(
        description="Retirar publicação das relações selecionadas", permissions=["publish"]
    )
    def withdraw_selected(self, request, queryset):
        if queryset.count() > 100:
            self.message_user(
                request, "Retire no máximo 100 relações por ação.", level=messages.ERROR
            )
            return
        for relationship in queryset.order_by("pk"):
            withdraw_relationship(relationship, request.user)
        self.message_user(request, "Publicação retirada.", level=messages.SUCCESS)


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
        description="Extrair cargos profissionais das biografias retidas",
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
                request, f"Não foi possível extrair os cargos: {exc}", level=messages.ERROR
            )
        else:
            self.message_user(
                request,
                f"{result['created']} observações criadas; cargos verificáveis publicados automaticamente.",
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


class EntityPicker(AutocompleteJsonView):
    """Entity names for editors without Entity admin access; one form field only."""

    source: ClassVar[tuple[str, str, str]]
    permission: ClassVar[str]
    kinds: ClassVar[tuple[str, ...]]

    def get(self, request, *args, **kwargs):
        if (
            request.GET.get("app_label"),
            request.GET.get("model_name"),
            request.GET.get("field_name"),
        ) != self.source:
            raise PermissionDenied
        return super().get(request, *args, **kwargs)

    def has_perm(self, request, obj=None):
        return request.user.is_active and request.user.has_perm(self.permission)

    def get_queryset(self):
        return super().get_queryset().filter(kind__in=self.kinds).only("id", "name")


class IdentityEntitySelect(AutocompleteSelect):
    url_name = "%s:core_sourceidentity_entity_picker"


class IdentityEntityPicker(EntityPicker):
    source = ("core", "sourceidentity", "entity")
    permission = "core.review_sourceidentity"
    kinds = tuple(Entity.Kind.values)


class ObservationSubjectSelect(AutocompleteSelect):
    url_name = "%s:core_sourceobservation_subject_picker"


class ObservationSubjectPicker(EntityPicker):
    # The widget borrows the observation's entity foreign key; only persons are offered.
    source = ("core", "sourceobservation", "object")
    permission = "core.review_sourceobservation"
    kinds = (Entity.Kind.PERSON,)


class ObservationAdminForm(ObservationReviewForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields.get("reviewed_subject")
        if isinstance(field, forms.ModelChoiceField):
            widget = ObservationSubjectSelect(
                SourceObservation._meta.get_field("object"), admin.site, choices=field.choices
            )
            widget.is_required = field.required
            field.widget = widget


class UnresolvedSubjectFilter(admin.SimpleListFilter):
    title = "identificação do titular"
    parameter_name = "titular"

    def lookups(self, request, model_admin):
        return (
            ("unresolved", "Por identificar (sem identificador oficial)"),
            ("resolved", "Identificado por identificador oficial"),
        )

    def queryset(self, request, queryset):
        if self.value() == "unresolved":
            return queryset.filter(identity__isnull=True)
        if self.value() == "resolved":
            return queryset.filter(identity__isnull=False)
        return queryset


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
            kwargs["queryset"] = Entity.objects.filter(kind__in=Entity.Kind.values)
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
    form = ObservationAdminForm
    list_display = (
        "subject",
        "category",
        "dataset",
        "as_of",
        "is_current",
        "relationship",
        "reviewed_at",
    )
    list_filter = ("source", "dataset", UnresolvedSubjectFilter, "category", "is_current")
    search_fields = (
        "identity__entity__name",
        "subject_name",
        "object_name",
        "external_id",
        "passage",
    )
    list_select_related = ("identity__entity", "relationship__subject", "relationship__object")
    readonly_fields = (
        "source",
        "dataset",
        "scope",
        "external_id",
        "revision",
        "identity",
        "subject_name",
        "subject_reference",
        "category",
        "passage",
        "source_url",
        "publisher",
        "reference",
        "title",
        "effective_start",
        "start_precision",
        "effective_end",
        "end_precision",
        "temporal_status",
        "declared_on",
        "object",
        "object_name",
        "object_identifier",
        "kind",
        "role",
        "role_class",
        "term",
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

    @admin.display(description="Titular")
    def subject(self, obj):
        if obj.identity is not None:
            return obj.identity.entity
        return f"{obj.subject_name} (por identificar)"

    def get_urls(self):
        return [
            path(
                "subject-picker/",
                self.admin_site.admin_view(
                    ObservationSubjectPicker.as_view(admin_site=self.admin_site)
                ),
                name="core_sourceobservation_subject_picker",
            ),
            *super().get_urls(),
        ]

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
        # Name-only subjects need an editor-chosen, verified person.
        subject = ("reviewed_subject",) if obj is not None and obj.identity_id is None else ()
        return (
            *self.readonly_fields,
            *subject,
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


@admin.register(IdentitySuggestion)
class IdentitySuggestionAdmin(admin.ModelAdmin):
    list_display = (
        "name_as_published",
        "scheme",
        "external_id",
        "candidate",
        "status",
        "created_at",
        "reviewed_by",
    )
    list_filter = ("status", "scheme")
    search_fields = ("name_as_published", "external_id", "candidate__name")
    list_select_related = ("candidate", "reviewed_by")
    readonly_fields = (
        "scheme",
        "external_id",
        "name_as_published",
        "candidate",
        "basis",
        "status",
        "created_at",
        "reviewed_by",
        "reviewed_at",
    )
    fields = readonly_fields
    actions = ("accept_selected", "reject_selected")

    def has_review_permission(self, request):
        return request.user.is_active and request.user.has_perm("core.review_sourceidentity")

    def has_view_permission(self, request, obj=None):
        return self.has_review_permission(request) or super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _decide(self, request, queryset, decide, done: str) -> None:
        if queryset.count() > ACTION_LIMIT:
            self.message_user(
                request,
                f"Decida no máximo {ACTION_LIMIT} sugestões por ação.",
                level=messages.ERROR,
            )
            return
        decided = 0
        for suggestion in queryset.order_by("pk"):
            try:
                decide(suggestion, request.user)
            except (PermissionDenied, ValidationError) as exc:
                self.message_user(
                    request,
                    f"Não foi possível decidir {suggestion}: {exc}",
                    level=messages.ERROR,
                )
            else:
                decided += 1
        if decided:
            self.message_user(request, f"{decided} sugestões {done}.", level=messages.SUCCESS)

    @admin.action(
        description="Aceitar: é a mesma pessoa (cria a correspondência revista)",
        permissions=["review"],
    )
    def accept_selected(self, request, queryset):
        self._decide(request, queryset, accept_suggestion, "aceites")

    @admin.action(
        description="Rejeitar: não é a mesma pessoa",
        permissions=["review"],
    )
    def reject_selected(self, request, queryset):
        self._decide(request, queryset, reject_suggestion, "rejeitadas")


class EventPartyInline(admin.TabularInline):
    model = EventParty
    extra = 0
    fields = ("role", "name", "identifier", "entity")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "dataset", "date", "amount", "status", "published_at")
    list_filter = ("status", "kind", "dataset")
    search_fields = ("title", "record_id", "parties__name")
    readonly_fields = (
        "dataset",
        "scope",
        "record_id",
        "kind",
        "title",
        "date",
        "start_date",
        "end_date",
        "amount",
        "currency",
        "amount_label",
        "record_url",
        "details",
        "source",
        "status",
        "fingerprint",
        "as_of",
        "retrieved_at",
        "published_at",
        "withdrawn_by",
        "withdrawn_at",
    )
    fields = readonly_fields
    inlines = (EventPartyInline,)
    actions = ("withdraw_selected",)

    def has_withdraw_permission(self, request):
        return request.user.is_active and request.user.has_perm("core.withdraw_event")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Retirar os eventos selecionados", permissions=["withdraw"])
    def withdraw_selected(self, request, queryset):
        if queryset.count() > ACTION_LIMIT:
            self.message_user(
                request,
                f"Retire no máximo {ACTION_LIMIT} eventos por ação.",
                level=messages.ERROR,
            )
            return
        for event in queryset.order_by("pk"):
            withdraw_event(event, request.user)
        self.message_user(
            request,
            "Eventos retirados; as importações seguintes não os voltam a publicar.",
            level=messages.SUCCESS,
        )


@admin.register(Term)
class TermAdmin(ParliamentReadOnlyAdmin):
    list_display = ("label", "kind", "code", "institution", "start_date", "end_date")
    list_filter = ("kind",)
    search_fields = ("label", "code")
    list_select_related = ("institution",)
