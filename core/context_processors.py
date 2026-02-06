from .models import Professional
from .permissions import is_receptionist, can_access_backoffice

def role_flags(request):
    is_professional = False
    is_reception = False
    can_backoffice = False
    user_avatar_url = ""
    if request.user.is_authenticated:
        prof = Professional.objects.filter(user=request.user).first()
        is_professional = bool(prof)
        is_reception = is_receptionist(request.user, ensure_group=True)
        can_backoffice = can_access_backoffice(request.user)
        if prof and prof.profile_photo:
            user_avatar_url = prof.profile_photo.url
    return {
        "is_professional": is_professional,
        "is_receptionist": is_reception,
        "can_access_backoffice": can_backoffice,
        "user_avatar_url": user_avatar_url,
    }
