from django.urls import path

from . import views

app_name = "onboarding"

urlpatterns = [
    path("", views.IndexView.as_view(), name="index"),
    path("template/", views.TemplateDownloadView.as_view(), name="template"),
    path("<int:pk>/", views.BatchDetailView.as_view(), name="detail"),
    path("<int:pk>/commit/", views.CommitView.as_view(), name="commit"),
    path("<int:pk>/undo/", views.UndoView.as_view(), name="undo"),
    path("<int:pk>/discard/", views.DiscardView.as_view(), name="discard"),
    path("rows/<int:pk>/decide/", views.DecideView.as_view(), name="decide"),
]
