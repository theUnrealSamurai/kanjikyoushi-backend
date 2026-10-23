from django.db import transaction
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import Learner, Sentence


class SubmitSerializer(serializers.Serializer):
    sentence_id = serializers.IntegerField()
    typed = serializers.CharField(trim_whitespace=False)
    revealed = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    missed = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)


def get_learner(user):
    """The user's learner row, locked until the request's transaction ends so one user's requests run one at a time."""
    Learner.objects.get_or_create(user=user)
    return Learner.objects.select_for_update().get(user=user)


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
    get_learner(request.user).onboard(kanji_list)
    return Response({"message": "Onboarding successful."})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def next_sentence(request):
    return next_sentence_response(get_learner(request.user))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def submit(request):
    """Grade the typed sentence and return the next one, so the page needs only one request per sentence."""
    serializer = SubmitSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    data, learner = serializer.validated_data, get_learner(request.user)
    try:
        learner.submit(data["sentence_id"], data["typed"], data["revealed"], data["missed"])
    except ValueError as error:
        return Response({"error": str(error)}, status=400)
    return next_sentence_response(learner)
