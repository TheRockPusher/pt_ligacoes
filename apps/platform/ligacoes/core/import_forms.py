from uuid import uuid4

from django import forms

from .models import ImportRun
from .parliament_fetch import ParliamentImportError, validate_legislature


class ImportRequestForm(forms.Form):
    request_id = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    mode = forms.ChoiceField(
        label="Operação",
        choices=(
            (ImportRun.Mode.DRY_RUN, "Apenas validar"),
            (ImportRun.Mode.APPLY, "Guardar rascunhos privados"),
        ),
        initial=ImportRun.Mode.DRY_RUN,
        help_text="A validação não altera dados editoriais; ambas as operações ficam no histórico.",
    )
    legislature = forms.CharField(
        label="Legislatura",
        initial="XVII",
        max_length=12,
        help_text="Número romano da legislatura, por exemplo XVII.",
    )
    as_of = forms.DateField(
        label="Data de referência",
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "vDateField"}),
        help_text="Opcional. Se ficar vazia, usa-se a data do pedido.",
    )
    confirm_apply = forms.BooleanField(
        label="Confirmo que pretendo guardar rascunhos privados, sem publicação automática.",
        required=False,
        help_text="Obrigatório apenas para guardar rascunhos privados.",
    )

    def clean_legislature(self):
        legislature = self.cleaned_data["legislature"]
        try:
            validate_legislature(legislature)
        except ParliamentImportError as exc:
            raise forms.ValidationError("Indique a legislatura em números romanos.") from exc
        return legislature

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned
            and cleaned.get("mode") == ImportRun.Mode.APPLY
            and not cleaned.get("confirm_apply")
        ):
            self.add_error(
                "confirm_apply", "Confirme explicitamente a gravação de rascunhos privados."
            )
        return cleaned
