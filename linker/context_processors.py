from linker.models import CreatorStore
from linker.services.active_store import get_active_store


def shoppinghub_store_context(request):
    if not request.user.is_authenticated:
        return {'active_store': None, 'creator_stores': []}
    active_store = get_active_store(request)
    stores = CreatorStore.objects.filter(owner=request.user, active=True).order_by('created_at', 'id')
    return {'active_store': active_store, 'creator_stores': stores}
