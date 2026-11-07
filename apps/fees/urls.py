from django.urls import path

from . import views

app_name = "fees"

urlpatterns = [
    path("", views.FeeStructureListView.as_view(), name="structure_list"),
    path("terms/", views.TermListView.as_view(), name="term_list"),
    path("terms/new/", views.TermCreateView.as_view(), name="term_create"),
    path("terms/<int:pk>/edit/", views.TermUpdateView.as_view(), name="term_update"),
    path("new/", views.FeeStructureCreateView.as_view(), name="structure_create"),
    path(
        "students/<int:student>/adjustments/new/",
        views.AdjustmentCreateView.as_view(),
        name="adjustment_create",
    ),
    path(
        "adjustments/<int:pk>/void/",
        views.AdjustmentVoidView.as_view(),
        name="adjustment_void",
    ),
    # Before the <int:pk> patterns, so "import" is never read as a structure id.
    path("import/", views.FeeImportView.as_view(), name="structure_import"),
    path(
        "import/template.xlsx",
        views.FeeImportTemplateView.as_view(),
        name="structure_import_template",
    ),
    path(
        "import/review/",
        views.FeeImportReviewView.as_view(),
        name="structure_import_review",
    ),
    path(
        "<int:pk>/edit/",
        views.FeeStructureUpdateView.as_view(),
        name="structure_update",
    ),
    path(
        "<int:pk>/delete/",
        views.FeeStructureDeleteView.as_view(),
        name="structure_delete",
    ),
]
