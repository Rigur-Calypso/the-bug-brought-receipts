from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='sample-home'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('runs/', views.start, name='sample-start'),
    path('runs/<uuid:run_id>/', views.detail, name='sample-run'),
    path('runs/<uuid:run_id>/status/', views.status, name='sample-status'),
    path('runs/<uuid:run_id>/cancel/', views.cancel, name='sample-cancel'),
]
