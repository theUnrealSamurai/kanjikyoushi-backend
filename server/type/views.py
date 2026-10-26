from django.db import transaction
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import REPORT_DAYS, Learner, Sentence, load_zone


class SubmitSerializer(serializers.Serializer):
    sentence_id = serializers.IntegerField()
    typed = serializers.CharField(trim_whitespace=False)
    revealed = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    skipped = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)


def get_learner(request):
    """The user's learner row, locked until the request's transaction ends so one user's requests run one at a time.
    The X-Timezone header (an IANA zone name) is stored when it is valid and new, so "today" is the user's day."""
    Learner.objects.get_or_create(user=request.user)
    learner = Learner.objects.select_for_update().get(user=request.user)
    name = request.headers.get("X-Timezone", "")
    if name != learner.timezone and load_zone(name):
        learner.timezone = name
        learner.save(update_fields=["timezone"])
    return learner


def next_sentence_response(learner):
    if not learner.onboarded:
        return Response({"error": "not_onboarded"}, status=409)
    payload = learner.next_sentence()
    if payload is None:
        return Response({"done": True, "corpus_empty": not Sentence.objects.exists(), "stats": learner.stats()})
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def onboard(request):
    kanji_list = request.data.get('kanji_list')
    if not isinstance(kanji_list, str):
        return Response({"error": "Expected 'kanji_list': a string of the kanji the user already knows."}, status=400)
    get_learner(request).onboard(kanji_list)
    return Response({"message": "Onboarding successful."})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def next_sentence(request):
    return next_sentence_response(get_learner(request))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def mode(request):
    """Switch between learning new words and reviewing only, and return the sentence for the new mode."""
    learn_new = request.data.get('learn_new')
    if not isinstance(learn_new, bool):
        return Response({"error": "Expected 'learn_new': true or false."}, status=400)
    learner = get_learner(request)
    learner.set_learn_new(learn_new)
    return next_sentence_response(learner)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def paragraphs(request):
    """Turn paragraph mode on or off and return the sentence for the new mode. It only turns on once unlocked."""
    on = request.data.get('on')
    if not isinstance(on, bool):
        return Response({"error": "Expected 'on': true or false."}, status=400)
    learner = get_learner(request)
    try:
        learner.set_paragraphs(on)
    except ValueError as error:
        return Response({"error": str(error)}, status=400)
    return next_sentence_response(learner)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def due(request):
    return Response(get_learner(request).due_list())


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def submit(request):
    """Grade the typed sentence and return the next one, so the page needs only one request per sentence."""
    serializer = SubmitSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    data, learner = serializer.validated_data, get_learner(request)
    try:
        learner.submit(data["sentence_id"], data["typed"], data["revealed"], data["skipped"])
    except ValueError as error:
        return Response({"error": str(error)}, status=400)
    return next_sentence_response(learner)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def stats(request):
    """The stats page: learning history per day over the last `days` days (7 to 365, default 30)."""
    days = request.data.get('days', 30)
    days = min(max(days, REPORT_DAYS[0]), REPORT_DAYS[1]) if isinstance(days, int) and not isinstance(days, bool) else 30
    learner = get_learner(request)
    if not learner.onboarded:
        return Response({"error": "not_onboarded"}, status=409)
    return Response(learner.report(days))
