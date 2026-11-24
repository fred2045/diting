from django.urls import path

from . import views

urlpatterns = [
    path("prefs", views.index, name="prefs_index"),
    path("prefs/save", views.save, name="prefs_save"),
    path("prefs/reset", views.reset, name="prefs_reset"),
    # 全局层文件（画像 / 本体）：整篇 markdown，不是键值，单独端点
    path("prefs/file", views.save_file, name="prefs_file"),
]
