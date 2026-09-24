from django.db import OperationalError
from django.shortcuts import render
from rest_framework import status, viewsets
from rest_framework.response import Response

# Create your views here.
from django.http import HttpResponse

from .models import (select_test_failures, attach_builder_ids, is_statement_timeout,
                     SEARCH_TIMEOUT_MESSAGE, TestFailure)
from .serializers import TestFailureSerializer, APIQueryParamsSerializer


def index(request):
    available_filters = [
        "branch",
        "revision",
        "platform",
        "dt",
        "max_dt",
        "bbnum",
        "typ",
        "info",
        "test_name",
        "test_variant",
        "info_text",
        "failure_text",
        "limit",
        "sort_order",
    ]

    # HEAD (uptime checks, link previews) is answered like GET
    qd = request.POST if request.method == "POST" else request.GET

    if qd == {}:
        return render(request, "cr/index.html", {})

    context = {}
    try:
        test_failures = select_test_failures(qd)["test_runs"]
        attach_builder_ids(test_failures)
        context["test_runs"] = test_failures
    except OperationalError as e:
        if not is_statement_timeout(e):
            raise
        context["error"] = SEARCH_TIMEOUT_MESSAGE

    for f in available_filters:
        if f in qd:
            context[f] = qd[f]

    return render(request, "cr/index.html", context)


class TestFailureViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TestFailureSerializer
    expected_filters = [
        "branch",
        "revision",
        "platform",
        "dt",
        "max_dt",
        "bbnum",
        "typ",
        "info",
        "test_name",
        "test_variant",
        "info_text",
        "failure_text",
        "limit",
        "sort_order",
    ]

    def get_queryset(self):
        # Return an empty queryset here; we'll handle filtering in `list()`
        return TestFailure.objects.none()

    def list(self, request, *args, **kwargs):
        filterset = APIQueryParamsSerializer(data=request.query_params)
        filterset.is_valid(raise_exception=True)
        filters = filterset.validated_data

        # mimic frontend so that "select_test_failures" works correctly
        # i.e. sending keys with empty string values for missing filters
        for key in self.expected_filters:
            if key not in filters:
                filters[key] = ""
        try:
            test_failures = select_test_failures(filters)
            serializer = TestFailureSerializer(test_failures["test_runs"], many=True)
            return Response(serializer.data)
        except OperationalError as e:
            if not is_statement_timeout(e):
                raise
            return Response({"detail": SEARCH_TIMEOUT_MESSAGE},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)


def health_check(request):
    # Check the db connection by performing a simple query
    try:
        TestFailure.objects.using('buildbot').exists()
        return HttpResponse("OK", status=200)
    except Exception:
        return HttpResponse("Database connection error", status=500)
