from django.urls import reverse
from django.db.models import Q

from linker.models import CreatorStore, Linker, ProductBlock, ResponseRule


def build_shoppinghub_target_url(store, target_type, target):
    if target_type == ResponseRule.TargetType.PRODUCT:
        if getattr(target, 'item_type', 'PRODUCT') == 'PRODUCT':
            return reverse('public_store', args=[store.slug]) + f'?product={target.product_no}'
        return reverse('public_store', args=[store.slug]) + f'?item={target.pk}'
    if target_type == ResponseRule.TargetType.PRODUCT_BLOCK:
        return reverse('public_store', args=[store.slug]) + f'?block={target.block_number}'
    return reverse('public_store', args=[store.slug])


def resolve_target_url(store, target_type, target):
    """Return the internal public path for a DM target; templates never build URLs."""
    return build_shoppinghub_target_url(store, target_type, target)


def shoppinghub_target_label(target_type, target):
    if target_type == ResponseRule.TargetType.PRODUCT:
        return f'{target.product_no} 상품 보기'
    if target_type == ResponseRule.TargetType.PRODUCT_BLOCK:
        return f'{target.block_number} 상품 모아보기'
    return f'{target.store_name} Store 보기'


def default_dm_message(store, target_type, target, greeting='안녕하세요 😊', guidance=''):
    if target_type == ResponseRule.TargetType.PRODUCT_BLOCK:
        body = '요청하신 상품들을 한곳에 모아두었어요.'
        destination = '비교해보세요.'
    elif target_type == ResponseRule.TargetType.PRODUCT:
        body = guidance or '요청하신 상품이에요.'
        destination = '확인해보세요.'
    else:
        body = '소개한 상품들을 Store에 모아두었어요.'
        destination = '둘러보세요.'
    return f'{greeting}\n{body}\n\n{store.store_name} Store에서 {destination}'


def resolve_target(owner, target_type, target_id, store=None):
    store_filter = {'store': store} if store is not None else {}
    if store is not None:
        first_store_id = CreatorStore.objects.filter(owner=owner).order_by('created_at', 'id').values_list('pk', flat=True).first()
        store_filter = {'store': store} if store.pk != first_store_id else None
    if target_type == ResponseRule.TargetType.PRODUCT:
        query = Linker.objects.filter(pk=target_id, owner=owner, status='ACTIVE')
        if store_filter is None:
            query = query.filter(Q(store=store) | Q(store__isnull=True))
        else:
            query = query.filter(**store_filter)
        return query.get()
    if target_type == ResponseRule.TargetType.PRODUCT_BLOCK:
        query = ProductBlock.objects.filter(pk=target_id, owner=owner, active=True)
        if store_filter is None:
            query = query.filter(Q(store=store) | Q(store__isnull=True))
        else:
            query = query.filter(**store_filter)
        return query.get()
    return CreatorStore.objects.get(pk=target_id, owner=owner, active=True)
