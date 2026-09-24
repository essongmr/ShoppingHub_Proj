from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_store_data(apps, schema_editor):
    CreatorStore = apps.get_model('linker', 'CreatorStore')
    Linker = apps.get_model('linker', 'Linker')
    ProductBlock = apps.get_model('linker', 'ProductBlock')
    StoreCategory = apps.get_model('linker', 'StoreCategory')
    SocialAccount = apps.get_model('linker', 'SocialAccount')

    stores_by_owner = {}
    for store in CreatorStore.objects.order_by('created_at', 'id'):
        stores_by_owner.setdefault(store.owner_id, store)

    for model in (Linker, ProductBlock, StoreCategory):
        for row in model.objects.filter(store__isnull=True).iterator():
            store = stores_by_owner.get(row.owner_id)
            if store:
                model.objects.filter(pk=row.pk).update(store_id=store.pk)

    for account in SocialAccount.objects.filter(store__isnull=True).iterator():
        store = stores_by_owner.get(account.owner_id)
        if store:
            SocialAccount.objects.filter(pk=account.pk).update(store_id=store.pk)


def reverse_store_data(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [('linker', '0018_responselog_uniq_response_rule_attempt')]

    operations = [
        migrations.AlterField(
            model_name='creatorstore',
            name='owner',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='creator_stores', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='linker',
            name='store',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='linkers', to='linker.creatorstore'),
        ),
        migrations.AddField(
            model_name='productblock',
            name='store',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='product_blocks', to='linker.creatorstore'),
        ),
        migrations.RemoveConstraint(model_name='storecategory', name='uniq_store_category_owner_name'),
        migrations.AddConstraint(
            model_name='storecategory',
            constraint=models.UniqueConstraint(fields=('store', 'name'), name='uniq_store_category_store_name'),
        ),
        migrations.RunPython(backfill_store_data, reverse_store_data),
    ]
