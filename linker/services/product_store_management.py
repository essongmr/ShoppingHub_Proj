from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from pathlib import Path

from linker.models import (
    CreatorStore,
    Linker,
    ProductBlock,
    ProductBlockItem,
    RecipeDetail,
    ResponseKeyword,
    ResponseRule,
    SocialContent,
    SocialContentLink,
    ServiceDetail,
)


class ProductStoreManagementError(ValidationError):
    pass


def _owned_current_product(product_id, owner, current_store):
    product = (
        Linker.objects.select_for_update()
        .filter(pk=product_id, owner=owner, store=current_store)
        .first()
    )
    if not product:
        raise ProductStoreManagementError('현재 스토어의 상품만 관리할 수 있습니다.')
    return product


def validate_target_store(owner, current_store, target_store_id):
    target = CreatorStore.objects.filter(
        pk=target_store_id,
        owner=owner,
        active=True,
    ).first()
    if not target or target.pk == current_store.pk:
        raise ProductStoreManagementError('대상 스토어를 확인해 주세요.')
    return target


def get_product_dependencies(product):
    return {
        'product_blocks': ProductBlockItem.objects.filter(linker=product).count(),
        'rules': ResponseRule.objects.filter(target_linker=product).count(),
        'keywords': ResponseKeyword.objects.filter(target_linker=product).count(),
        'content_links': SocialContentLink.objects.filter(linker=product).count(),
        'contents': SocialContent.objects.filter(linker=product).count(),
    }


def _clear_product_references(product):
    ProductBlockItem.objects.filter(linker=product).delete()
    ResponseRule.objects.filter(target_linker=product).update(
        target_linker=None,
        enabled=False,
    )
    ResponseRule.objects.filter(keywords__target_linker=product).update(enabled=False)
    ResponseKeyword.objects.filter(target_linker=product).update(target_linker=None)
    SocialContentLink.objects.filter(linker=product).delete()
    SocialContent.objects.filter(linker=product).update(linker=None)


def copy_product_to_store(product_id, owner, current_store, target_store_id):
    with transaction.atomic():
        source = _owned_current_product(product_id, owner, current_store)
        target = validate_target_store(owner, current_store, target_store_id)
        duplicate_query = Linker.objects.filter(owner=owner, store=target)
        if source.destination_url:
            duplicate = duplicate_query.filter(destination_url=source.destination_url).first()
        else:
            duplicate = duplicate_query.filter(
                product_no=source.product_no,
                title=source.title,
            ).first()
        if duplicate:
            raise ProductStoreManagementError('이미 해당 스토어에 등록된 상품입니다.')
        copied = Linker.objects.create(
            owner=owner,
            store=target,
            linker_group_code=source.linker_group_code,
            title=source.title,
            description=source.description,
            status='ACTIVE',
            marketplace=source.marketplace,
            destination_url=source.destination_url,
            product_no=source.product_no,
            thumbnail_url=source.thumbnail_url,
            store_category=None,
            store_visible=True,
            display_order=source.display_order,
            memo=source.memo,
            item_type=source.item_type,
        )
        if source.item_type == Linker.ItemType.RECIPE and hasattr(source, 'recipe_detail'):
            detail = source.recipe_detail
            RecipeDetail.objects.create(linker=copied, ingredients=detail.ingredients, instructions=detail.instructions, tips=detail.tips, servings=detail.servings, prep_time=detail.prep_time, cook_time=detail.cook_time)
        elif source.item_type == Linker.ItemType.SERVICE and hasattr(source, 'service_detail'):
            detail = source.service_detail
            ServiceDetail.objects.create(linker=copied, service_description=detail.service_description, price_info=detail.price_info, duration=detail.duration, location_info=detail.location_info, contact_info=detail.contact_info, booking_url=detail.booking_url)
        if source.product_image:
            source.product_image.open('rb')
            copied.product_image.save(Path(source.product_image.name).name, ContentFile(source.product_image.read()), save=True)
            source.product_image.close()
        return copied, target


def move_product_to_store(product_id, owner, current_store, target_store_id):
    with transaction.atomic():
        product = _owned_current_product(product_id, owner, current_store)
        target = validate_target_store(owner, current_store, target_store_id)
        dependencies = get_product_dependencies(product)
        ProductBlockItem.objects.filter(linker=product).delete()
        ResponseRule.objects.filter(target_linker=product).update(target_linker=None, enabled=False)
        ResponseRule.objects.filter(keywords__target_linker=product).update(enabled=False)
        ResponseKeyword.objects.filter(target_linker=product).update(target_linker=None)
        SocialContentLink.objects.filter(linker=product).delete()
        SocialContent.objects.filter(linker=product).update(linker=None)
        product.store = target
        product.save(update_fields=['store', 'updated_at'])
        return product, target, dependencies


def hide_product(product_id, owner, current_store, visible=False):
    with transaction.atomic():
        product = _owned_current_product(product_id, owner, current_store)
        product.store_visible = visible
        product.save(update_fields=['store_visible', 'updated_at'])
        return product


def delete_product_safely(product_id, owner, current_store):
    with transaction.atomic():
        product = _owned_current_product(product_id, owner, current_store)
        dependencies = get_product_dependencies(product)
        _clear_product_references(product)
        product.delete()
        return dependencies


def delete_product_block_safely(block_id, owner, current_store):
    with transaction.atomic():
        block = (
            ProductBlock.objects.select_for_update()
            .filter(pk=block_id, owner=owner, store=current_store)
            .first()
        )
        if not block:
            raise ProductStoreManagementError('현재 스토어의 상품묶음만 관리할 수 있습니다.')
        dependencies = {
            'rules': ResponseRule.objects.filter(target_product_block=block).count(),
            'keywords': ResponseKeyword.objects.filter(target_product_block=block).count(),
            'products': ProductBlockItem.objects.filter(product_block=block).count(),
        }
        ResponseRule.objects.filter(target_product_block=block).update(
            target_product_block=None,
            enabled=False,
        )
        ResponseKeyword.objects.filter(target_product_block=block).update(target_product_block=None)
        ProductBlockItem.objects.filter(product_block=block).delete()
        block.delete()
        return dependencies
