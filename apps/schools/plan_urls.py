from django.urls import path

from . import plan_views

app_name = "billing"

urlpatterns = [
    path("", plan_views.PlanCheckoutView.as_view(), name="checkout"),
    path(
        "orders/<str:reference>/",
        plan_views.PlanOrderView.as_view(),
        name="order",
    ),
    path(
        "orders/<str:reference>/transfer-reported/",
        plan_views.PlanOrderReportView.as_view(),
        name="order_reported",
    ),
]
