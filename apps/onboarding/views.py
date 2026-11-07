"""Upload a workbook, read what it would do, decide the uncertain rows, then commit."""

from __future__ import annotations

import hashlib
from io import BytesIO

from django import forms
from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.generic import View

from apps.core.forms import StyledFormMixin
from apps.core.permissions import Capability, CapabilityRequiredMixin
from apps.core.spreadsheets import WorkbookError, build_workbook, read_named_sheets

from . import engine
from .models import BatchStatus, ImportBatch, ImportRow, MatchDecision, RowStatus
from .sheets import ALL_SHEETS, BY_NAME, KEYS

MAX_BYTES = 10 * 1024 * 1024
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ANSWERS = {
    "create": "Import it",
    "use_existing": "Same one, keep the record here",
    "skip": "Leave this row out",
}
_KEY_FOR = {sheet.name: key for key, sheet in KEYS.items()}


class ImportMixin(CapabilityRequiredMixin):
    capability = Capability.IMPORT_RECORDS


class UploadForm(StyledFormMixin, forms.Form):
    upload = forms.FileField(label="Workbook (.xlsx)")

    def clean_upload(self):
        upload = self.cleaned_data["upload"]
        if not (upload.name or "").lower().endswith(".xlsx"):
            raise forms.ValidationError("Save it as an Excel Workbook (.xlsx) and upload it again.")
        if upload.size > MAX_BYTES:
            raise forms.ValidationError(
                f"That file is {upload.size / 1_048_576:.1f} MB. The limit is "
                f"{MAX_BYTES // 1_048_576} MB, so split it into smaller files."
            )
        return upload


class IndexView(ImportMixin, View):
    def get(self, request):
        return self._page(request, UploadForm())

    def post(self, request):
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            school = request.user.school
            if school is None:
                raise Http404("Records are imported from inside a school.")
            content = form.cleaned_data["upload"].read()
            try:
                parsed = read_named_sheets(BytesIO(content), ALL_SHEETS)
            except WorkbookError as problem:
                form.add_error("upload", str(problem))
            else:
                if not parsed:
                    names = ", ".join(sheet.name for sheet in ALL_SHEETS)
                    form.add_error(
                        "upload",
                        f"None of the sheets this import reads were found. It looks for: {names}.",
                    )
                elif not any(sheet.rows for sheet in parsed.values()):
                    form.add_error("upload", "The sheets are there, but there is nothing to import in them.")
                else:
                    name = form.cleaned_data["upload"].name
                    batch = _stage(school, request.user, name, content, parsed)
                    return HttpResponseRedirect(batch.get_absolute_url())
        return self._page(request, form)

    def _page(self, request, form):
        return render(request, "onboarding/index.html", {
            "form": form,
            "batches": ImportBatch.objects.all()[:25],
            "sheets": ALL_SHEETS,
            "page_title": "Import records",
        })


def _stage(school, user, name, content, parsed) -> ImportBatch:
    digest = hashlib.sha256(content).hexdigest()
    with transaction.atomic():
        batch = ImportBatch(school=school, created_by=user, filename=name[:200], file_hash=digest)
        batch.file.save(f"{digest}.xlsx", ContentFile(content), save=False)
        batch.save()
        ImportRow.objects.bulk_create(
            [
                ImportRow(
                    batch=batch, sheet=_KEY_FOR[sheet_name], number=row.number, raw=row.values,
                    label=BY_NAME[sheet_name].describe(row.values)[:200],
                )
                for sheet_name, sheet in parsed.items()
                for row in sheet.rows
            ],
            batch_size=500,
        )
        engine.judge(batch)
    return batch


class TemplateDownloadView(ImportMixin, View):
    def get(self, request):
        response = HttpResponse(build_workbook(ALL_SHEETS), content_type=XLSX)
        response["Content-Disposition"] = 'attachment; filename="schoolcord-onboarding.xlsx"'
        return response


class BatchDetailView(ImportMixin, View):
    def get(self, request, pk):
        batch = get_object_or_404(ImportBatch.objects, pk=pk)
        rows = batch.rows
        reviews = list(rows.filter(status=RowStatus.REVIEW)[:50])
        for row in reviews:
            row.answers = [(option, ANSWERS[option]) for option in row.options]
        open_reviews = rows.filter(status=RowStatus.REVIEW).count()
        return render(request, "onboarding/detail.html", {
            "batch": batch,
            "summary": engine.summary(batch),
            "money": engine.money_preview(batch),
            "reviews": reviews,
            "open_reviews": open_reviews,
            "problems": rows.filter(status__in=[RowStatus.PROBLEM, RowStatus.FAILED])[:100],
            "left_out": rows.filter(status=RowStatus.SKIP)[:50],
            "same_file": ImportBatch.objects.filter(file_hash=batch.file_hash, id__lt=batch.pk).exists(),
            "can_commit": batch.status in (BatchStatus.DRAFT, BatchStatus.COMMITTING) and not open_reviews,
            "can_discard": batch.status == BatchStatus.DRAFT,
            "can_undo": batch.status in (BatchStatus.COMMITTED, BatchStatus.UNDOING),
            "page_title": batch.filename,
        })


class DecideView(ImportMixin, View):
    def post(self, request, pk):
        row = get_object_or_404(
            ImportRow.objects.filter(batch__in=ImportBatch.objects.all()).select_related("batch"),
            pk=pk,
        )
        decision = request.POST.get("decision", "")
        batch = row.batch
        if batch.status != BatchStatus.DRAFT or decision not in row.options:
            messages.error(request, "That row can't be changed now.")
            return HttpResponseRedirect(batch.get_absolute_url())
        row.decision = decision
        row.decided_by = request.user
        row.save(update_fields=["decision", "decided_by"])
        if row.signature:
            MatchDecision.all_objects.update_or_create(
                school_id=batch.school_id, signature=row.signature,
                defaults={"decision": decision, "decided_by": request.user},
            )
        # What depends on this row may change with it.
        engine.judge(batch)
        return HttpResponseRedirect(batch.get_absolute_url() + "#review")


class CommitView(ImportMixin, View):
    def post(self, request, pk):
        batch = get_object_or_404(ImportBatch.objects, pk=pk)
        finished = engine.commit_batch(batch, actor=request.user)
        batch.refresh_from_db()
        if finished:
            done = batch.rows.filter(status=RowStatus.DONE).count()
            messages.success(request, f"Imported {done} row{'' if done == 1 else 's'}.")
        elif batch.status == BatchStatus.DRAFT:
            messages.error(request, "Some rows need a decision first. Nothing was imported.")
        else:
            messages.info(request, "Still importing. Press Continue, and nothing is repeated.")
        return HttpResponseRedirect(batch.get_absolute_url())


class UndoView(ImportMixin, View):
    def post(self, request, pk):
        batch = get_object_or_404(ImportBatch.objects, pk=pk)
        reason = request.POST.get("reason", "").strip()
        if batch.status not in (BatchStatus.COMMITTED, BatchStatus.UNDOING):
            messages.info(request, "Only a finished import can be undone.")
        elif not reason:
            messages.error(request, "Say why, so the record explains itself later.")
        elif engine.undo_batch(batch, actor=request.user, reason=reason):
            messages.warning(request, "Import undone. Its payments are voided and kept on record.")
        else:
            messages.info(request, "Still undoing. Press Continue, and nothing is repeated.")
        return HttpResponseRedirect(batch.get_absolute_url())


class DiscardView(ImportMixin, View):
    """Nothing has been written yet, so nothing else has to change."""

    def post(self, request, pk):
        batch = get_object_or_404(ImportBatch.objects, pk=pk)
        if batch.status != BatchStatus.DRAFT:
            messages.error(request, "An import that has started can only be undone.")
            return HttpResponseRedirect(batch.get_absolute_url())
        if batch.file:
            batch.file.delete(save=False)
        batch.delete()
        messages.success(request, "File discarded.")
        return HttpResponseRedirect(reverse("onboarding:index"))
