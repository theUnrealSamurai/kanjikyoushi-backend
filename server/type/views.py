from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from .models import CoreDataProcessing


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def onboard(request):
    try:
        kanji_list = request.data['kanji_list']
        request.user.coredataprocessing.onboard(kanji_list)
    except KeyError:
        return Response({"error": "Function excepts a list of kanji that user already knows, 'kanji_list'"}, status=400)
    except AttributeError:
        CoreDataProcessing.objects.create(user=request.user)
        request.user.coredataprocessing.onboard(kanji_list)

    return Response({"message": "Onboarding successful."})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def render_practice(request):
    kanji = request.user.coredataprocessing.render_practice()
    return Response(kanji)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def update_practice(request):
    try:
        request.user.coredataprocessing.update_practice(request.data['sentence'])
        return Response({"message": "Practice updated."})
    except KeyError:
        return Response({"error": "Function excepts a 'sentence'"}, status=400)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def render_revision(request):
    response = request.user.coredataprocessing.render_revision()
    if not response:
        return Response({"message": "No cards to revise."}, status=200)
    return Response(response)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def update_revision(request):
    try:
        request.user.coredataprocessing.update_revision(request.data['kanji_rating_dict'])
        return Response({"message": "Revision updated."})
    except KeyError:
        return Response({"error": "Function excepts a 'kanji_rating_dict'"}, status=400)