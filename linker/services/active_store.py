from linker.models import CreatorStore


ACTIVE_STORE_SESSION_KEY = 'active_store_id'


def get_active_store(request):
    if not request.user.is_authenticated:
        return None

    store_id = request.session.get(ACTIVE_STORE_SESSION_KEY)
    store = None
    if store_id:
        store = CreatorStore.objects.filter(pk=store_id, owner=request.user, active=True).first()

    if store is None:
        store = CreatorStore.objects.filter(owner=request.user, active=True).order_by('created_at', 'id').first()
        if store is None:
            store = CreatorStore.objects.filter(owner=request.user).order_by('created_at', 'id').first()
        if store is not None:
            request.session[ACTIVE_STORE_SESSION_KEY] = store.pk

    return store


def set_active_store(request, store):
    if store.owner_id != request.user.pk:
        raise ValueError('Store ownership mismatch')
    request.session[ACTIVE_STORE_SESSION_KEY] = store.pk
    request.session.modified = True
    return store
