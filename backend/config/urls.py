from django.contrib import admin
from django.http import HttpRequest, JsonResponse
from django.urls import path

from apps.cloudmock.views import ingest


def healthz(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz),
    path("cloud/ingest/", ingest),  # 404 unless CLOUD_MOCK_ENABLED (mock cloud service only)
]
