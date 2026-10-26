from rest_framework import serializers
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone

from .models import InviteCode


INVALID_INVITE = "That invite code isn't valid or has already been used."


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()  # a username or an email address
    password = serializers.CharField(write_only=True)


class SignUpSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(write_only=True, required=True, validators=[validate_password])
    invite_code = serializers.CharField(write_only=True, required=True)

    class Meta:
        model = User
        fields = ('username', 'email', 'password', 'invite_code')

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_invite_code(self, value):
        invite = InviteCode.objects.filter(code__iexact=value.strip(), used_by__isnull=True).first()
        if invite is None:
            raise serializers.ValidationError(INVALID_INVITE)
        return invite

    def create(self, validated_data):
        invite = validated_data['invite_code']
        with transaction.atomic():
            user = User.objects.create_user(
                username=validated_data['username'],
                email=validated_data['email'],
                password=validated_data['password']
            )
            # Claim the code in the same transaction, so two signups racing for it can't both get in.
            claimed = InviteCode.objects.filter(pk=invite.pk, used_by__isnull=True).update(used_by=user, used_at=timezone.now())
            if not claimed:
                raise serializers.ValidationError({'invite_code': [INVALID_INVITE]})
        return user
