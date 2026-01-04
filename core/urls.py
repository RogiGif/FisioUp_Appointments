from django.urls import path
from .views import book_view, login_view, logout_view, register_view

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("marcar/", book_view, name="book"),
    path("registar/", register_view, name="register"),

]
