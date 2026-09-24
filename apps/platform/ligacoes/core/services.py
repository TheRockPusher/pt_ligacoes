from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from .models import Entity, Evidence, Relationship, ReviewEvent, editorial_transaction


@editorial_transaction()
def publish_relationship(relationship, reviewer):
    """Publish persisted, locked facts only, with a fresh permission check and audit."""
    if not getattr(reviewer, "pk", None):
        raise PermissionDenied("A publicação exige uma pessoa revisora autorizada.")
    reviewer = User.objects.select_for_update().get(pk=reviewer.pk)
    if not reviewer.is_active or not reviewer.has_perm("core.publish_relationship"):
        raise PermissionDenied("Não tem permissão para publicar relações.")
    if not relationship.pk or relationship._state.adding:
        raise ValidationError("Guarde o rascunho antes da revisão.")
    current = Relationship.objects.select_for_update(of=("self",)).get(pk=relationship.pk)
    current.full_clean()
    endpoints = list(
        Entity.objects.select_for_update()
        .filter(pk__in=[current.subject_id, current.object_id])
        .order_by("pk")
    )
    if len(endpoints) != 2 or not all(entity.is_public for entity in endpoints):
        raise ValidationError("As duas entidades têm de estar públicas antes da publicação.")
    evidence = (
        Evidence.objects.select_related("source")
        .select_for_update(of=("self", "source"))
        .filter(relationship=current, is_public=True, source__is_public=True)
        .order_by("pk")
        .first()
    )
    if evidence is None:
        raise ValidationError("A publicação exige evidência pública numa fonte pública.")
    evidence.full_clean()
    evidence.source.full_clean()
    if current.status == Relationship.Status.PUBLISHED:
        return current
    current.status = Relationship.Status.PUBLISHED
    current.reviewed_by = reviewer
    current.reviewed_at = timezone.now()
    current.full_clean()
    # The sole publication write: model saves cannot manufacture review metadata.
    Relationship.objects.filter(pk=current.pk).update(
        status=current.status, reviewed_by=reviewer, reviewed_at=current.reviewed_at
    )
    ReviewEvent.objects.create(
        relationship=current, reviewer=reviewer, action=ReviewEvent.Action.PUBLISH
    )
    return current
