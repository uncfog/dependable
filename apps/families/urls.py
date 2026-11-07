from django.urls import path

from . import views

app_name = "families"

urlpatterns = [
    path("", views.FamilyListView.as_view(), name="list"),
    path("new/", views.FamilyCreateView.as_view(), name="create"),
    path("suggestions/", views.FamilySuggestionsView.as_view(), name="suggestions"),
    path("<int:pk>/", views.FamilyDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.FamilyUpdateView.as_view(), name="update"),
    path("<int:pk>/children/add/", views.AddChildView.as_view(), name="add_child"),
    path(
        "memberships/<int:pk>/remove/",
        views.RemoveChildView.as_view(),
        name="remove_child",
    ),
]
