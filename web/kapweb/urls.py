from django.urls import path

from pages import views

urlpatterns = [
    path("", views.index, name="index"),
    path("tools/", views.tools, name="tools"),
    path("tools/<slug:name>/", views.tool_detail, name="tool_detail"),
    path("status/", views.status, name="status"),
    path("privacy/", views.privacy, name="privacy"),
    path("healthz", views.healthz, name="healthz"),
]
