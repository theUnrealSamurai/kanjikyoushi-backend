from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from .models import CoreDataProcessing


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def onboard(request):
    # Only receives a list of kanji and updates it in the model
    try:
        kanji_list = request.data['kanji_list']
        user = request.user
        user.coredataprocessing.process_onboarding(kanji_list)

    except KeyError:
        return Response({"error": "Function excepts a list of kanji that user already knows"}, status=400)



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def render_sentence(request):
    try: 
        test, fetched_sentence = request.user.coredataprocessing.fetch_sentence()
    except CoreDataProcessing.DoesNotExist:
        return Response({"error": "User does not have any data."}, status=404)  
    
    response_data = {
        "test": test,
        "sentence": fetched_sentence
    }

    if not test:
        response_data = {
            "test": test,
            "sentence": fetched_sentence,
            "romaji": fetched_sentence['romaji'],
            "kanji": fetched_sentence['kanji'],
            "vocabulary": fetched_sentence['vocabulary'],
        }
    
    return Response(response_data)

