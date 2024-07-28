from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .forms import TypedTextForm
from django.shortcuts import redirect
from django.http import HttpResponse as HtmlResponse
from .models import UserKanjiData


from django.http import JsonResponse
from django.views.decorators.http import require_GET
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def type(request):
    try:
        test, fetched_sentence = request.user.userkanjidata.fetch_sentence()
    except UserKanjiData.DoesNotExist:
        onboard()

    response_data = {
        "test": test,
        "fetched_sentence": fetched_sentence,
        # "japanese": fetched_sentence["japanese"],
        # "english": fetched_sentence["english"],
    }

    if not test:
        response_data.update({
            "romaji": fetched_sentence["romaji"],
            "kanji": fetched_sentence["kanji"],
            "vocabulary": fetched_sentence["vocabulary"],
        })

    return Response(response_data)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def onboard(request):
    user_kanji_data, created = UserKanjiData.objects.get_or_create(
        user=request.user,
        defaults={'learning_kanji': "日"}  # Default value for new users
    )

    if request.method == 'POST':
        # Here you can add logic to handle any additional onboarding data
        # For example, updating user preferences or initial kanji set
        # This is just a placeholder, adjust according to your needs
        learning_kanji = request.data.get('learning_kanji', "日本語")
        user_kanji_data.learning_kanji = learning_kanji
        user_kanji_data.save()
        return Response({"message": "Onboarding completed successfully"}, status=status.HTTP_200_OK)

    # For GET requests, just return the current onboarding status
    return Response({
        "onboarded": not created,
        "current_learning_kanji": user_kanji_data.learning_kanji
    })



def update_typed_learning(request):
    if request.method == 'POST':
        form = TypedTextForm(request.POST)
        if form.is_valid():
            sentence = form.cleaned_data['typed_text']
            request.user.userkanjidata.update_typed_learning(sentence)
            return redirect(request.META['HTTP_REFERER'])
    return redirect(request.META['HTTP_REFERER'])


def update_typed_test(request):
    if request.method == 'POST':
        form = TypedTextForm(request.POST)
        if form.is_valid():
            sentence = form.cleaned_data['typed_text']
            request.user.userkanjidata.update_typed_test(sentence)
            return redirect(request.META['HTTP_REFERER'])
