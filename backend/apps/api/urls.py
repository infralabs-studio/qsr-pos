from django.urls import path

from . import views

urlpatterns = [
    path("auth/csrf/", views.CsrfView.as_view()),
    path("auth/login/", views.LoginView.as_view()),
    path("auth/logout/", views.LogoutView.as_view()),
    path("auth/me/", views.MeView.as_view()),
    path("menu/", views.MenuView.as_view()),
    path("shifts/current/", views.CurrentShiftView.as_view()),
    path("shifts/open/", views.OpenShiftView.as_view()),
    path("orders/", views.OrderListCreateView.as_view()),
    path("orders/<uuid:pk>/", views.OrderDetailView.as_view()),
    path("orders/<uuid:pk>/items/", views.OrderItemsView.as_view()),
    path("orders/<uuid:pk>/charge/", views.OrderChargeView.as_view()),
    path("orders/<uuid:pk>/payments/cash/", views.CashPaymentView.as_view()),
    path("orders/<uuid:pk>/payments/stk/", views.StkPaymentView.as_view()),
    path("orders/<uuid:pk>/payments/manual-mpesa/", views.ManualMpesaView.as_view()),
    path("payments/<uuid:pk>/", views.PaymentDetailView.as_view()),
    path("payments/<uuid:pk>/confirm/", views.PaymentConfirmView.as_view()),
    path("payments/<uuid:pk>/reject/", views.PaymentRejectView.as_view()),
    path("payments/<uuid:pk>/defer/", views.PaymentDeferView.as_view()),
    path("manager/review/", views.ReviewQueueView.as_view()),
    path("backfill/", views.BackfillView.as_view()),
    path("sync/status/", views.SyncStatusView.as_view()),
    path("mpesa/callback/<str:token>/", views.stk_callback),
    path("dev/mpesa/online/", views.DevMpesaOnlineView.as_view()),
    path("dev/stk/<uuid:pk>/simulate/", views.DevSimulateStkView.as_view()),
]
