from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='sample-home'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('github/', views.github_issue, name='github-issue'),
    path('runs/', views.start, name='sample-start'),
    path('runs/<uuid:run_id>/', views.detail, name='sample-run'),
    path('runs/<uuid:run_id>/status/', views.status, name='sample-status'),
    path('runs/<uuid:run_id>/cancel/', views.cancel, name='sample-cancel'),
    path('runs/<uuid:run_id>/publish/', views.publish, name='github-publish'),
    path('runs/<uuid:run_id>/download/<str:artifact>/', views.download, name='sample-download'),
]
