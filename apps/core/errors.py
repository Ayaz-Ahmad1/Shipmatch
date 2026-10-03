"""Branded error pages. The 500 page renders without the database or request context."""
from django.http import HttpResponseServerError
from django.shortcuts import render
from django.template import loader


def permission_denied(request, exception=None):
    message = str(exception) if exception and str(exception) else "Your role doesn't allow this action."
    return render(request, "errors/403.html", {"message": message}, status=403)


def not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)


def server_error(request):
    template = loader.get_template("errors/500.html")
    return HttpResponseServerError(template.render({"request_id": getattr(request, "request_id", "")}))
