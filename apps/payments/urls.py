from django.urls import path

from . import shared_views, views

app_name = "payments"

urlpatterns = [
    path("", views.PaymentListView.as_view(), name="index"),
    path("record/", views.PaymentCreateView.as_view(), name="record"),
    # Before the <int:pk> patterns, so these words are never read as ids.
    path("pending/", views.PendingQueueView.as_view(), name="pending"),
    path("daily/", views.DailyPaymentsView.as_view(), name="daily"),
    path("position/", views.StudentPositionView.as_view(), name="student_position"),
    path("students/search/", views.StudentSearchView.as_view(), name="student_search"),
    path("outstanding/", views.OutstandingView.as_view(), name="outstanding"),
    path(
        "student/<int:pk>/",
        views.StudentPaymentHistoryView.as_view(),
        name="student_history",
    ),
    path("family/", shared_views.SharedListView.as_view(), name="shared_list"),
    path("family/record/", shared_views.SharedRecordView.as_view(), name="shared_record"),
    path("family/<int:pk>/", shared_views.SharedDetailView.as_view(), name="shared_detail"),
    path(
        "family/<int:pk>/confirm/",
        shared_views.SharedConfirmView.as_view(),
        name="shared_confirm",
    ),
    path("family/<int:pk>/void/", shared_views.SharedVoidView.as_view(), name="shared_void"),
    path(
        "family/<int:pk>/receipt/",
        shared_views.SharedReceiptView.as_view(),
        name="shared_receipt",
    ),
    path(
        "family/<int:pk>/receipt/file/",
        shared_views.SharedReceiptFileView.as_view(),
        name="shared_receipt_file",
    ),
    path("<int:pk>/move/", shared_views.AllocationMoveView.as_view(), name="allocation_move"),
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
