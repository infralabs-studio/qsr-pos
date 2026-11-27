from django.contrib import admin
from django.http import HttpRequest, JsonResponse
from django.urls import include, path

from apps.cloudmock.mpesa_views import records_collection, records_detail
from apps.cloudmock.views import ingest


def healthz(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz),
    path("api/", include("apps.api.urls")),
    # Mock cloud only (each 404s unless CLOUD_MOCK_ENABLED):
    path("cloud/ingest/", ingest),
    path("cloud/mpesa/transactions/", records_collection),
    path("cloud/mpesa/transactions/<str:receipt_code>/", records_detail),
]
