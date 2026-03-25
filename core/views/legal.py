from django.shortcuts import render


def privacy_view(request):
    return render(request, "core/privacy.html")


def terms_view(request):
    return render(request, "core/terms.html")


def cookies_view(request):
    return render(request, "core/cookies.html")


def help_view(request):
    return render(request, "core/help.html")
