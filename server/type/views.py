from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from .models import CoreDataProcessing


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def onboard(request):
    # Only receives a list of kanji and updates it in the model
    user = request.user

    try:
        kanji_list = request.data['kanji_list']
        user.coredataprocessing.process_onboarding(kanji_list)
    except KeyError:
        return Response({"error": "Function excepts a list of kanji that user already knows, 'kanji_list'"}, status=400)
    except CoreDataProcessing.DoesNotExist:
        CoreDataProcessing.objects.create(user=user)
        user.coredataprocessing.process_onboarding(kanji_list)

    return Response({"message": "Onboarding successful."})



@api_view(['POST'])
@permission_classes([IsAuthenticated])
def render_sentence(request):
    try: 
        test, fetched_sentence = request.user.coredataprocessing.fetch_sentence()
    except CoreDataProcessing.DoesNotExist:
        return Response({"error": "User does not have any data."}, status=404)  
    
    response_data = {
        "test": test,
        "sentence": fetched_sentence["japanese"],
        "sentence_counter": fetched_sentence["sentence_counter"],
    }

    if not test:
        response_data = {
            "test": test,
            "japanese": fetched_sentence["japanese"],
            "english": fetched_sentence["english"],
            "romaji": fetched_sentence['romaji'],
            "sentence_counter": fetched_sentence["sentence_counter"],
            "kanji": fetched_sentence['kanji'],
            "vocabulary": fetched_sentence['vocabulary'],
        }
    
    return Response(response_data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def update_learning_sentence(request):
    user = request.user
    try:
        user.coredataprocessing.update_character_type_count(request.data['sentence'], False)
    except KeyError:
        return Response({"error": "Function excepts a sentence to update the Typing Statistics."}, status=400)
 
    return Response({"message": "Sentence updated successfully."})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_passed(request):
    user = request.user
    try:
        user.coredataprocessing.test_passed(request.data['sentence'])
    except KeyError:
        return Response({"error": "Function excepts a sentence to validate the test."}, status=400)
    
    return Response({"test_result": "Successful"})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def skip_test(request):
    user = request.user
    try:
        user.coredataprocessing.skip_test(request.data['skipped_kanjis'])
    except KeyError:
        return Response({"error": "Function excepts a sentence to skip the test."}, status=400)
    
    return Response({"message": "Test skipped successfully."})
