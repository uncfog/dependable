from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("", views.PaymentListView.as_view(), name="index"),
    path("record/", views.PaymentCreateView.as_view(), name="record"),
    # Before the <int:pk> patterns, so these words are never read as ids.
    path("pending/", views.PendingQueueView.as_view(), name="pending"),
    path("position/", views.StudentPositionView.as_view(), name="student_position"),
    path("students/search/", views.StudentSearchView.as_view(), name="student_search"),
    path("outstanding/", views.OutstandingView.as_view(), name="outstanding"),
    path(
        "student/<int:pk>/",
        views.StudentPaymentHistoryView.as_view(),
        name="student_history",
    ),
    path("<int:pk>/", views.PaymentDetailView.as_view(), name="payment_detail"),
    path("<int:pk>/edit/", views.PaymentUpdateView.as_view(), name="payment_update"),
    path("<int:pk>/void/", views.PaymentVoidView.as_view(), name="payment_void"),
    path("<int:pk>/receipt/", views.ReceiptView.as_view(), name="receipt"),
    path(
        "<int:pk>/receipt/file/",
        views.ReceiptFileView.as_view(),
        name="receipt_file",
    ),
]
