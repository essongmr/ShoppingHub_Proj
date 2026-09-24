import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('linker', '0011_alter_productblockitem_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='responserule',
            name='target_type',
            field=models.CharField(choices=[('PRODUCT', 'Product'), ('PRODUCT_BLOCK', 'Product block'), ('STORE', 'Store')], default='STORE', max_length=20),
        ),
        migrations.AddField(
            model_name='responserule',
            name='target_linker',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='response_rules', to='linker.linker'),
        ),
        migrations.AddField(
            model_name='responserule',
            name='target_product_block',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='response_rules', to='linker.productblock'),
        ),
        migrations.AddField(
            model_name='responserule',
            name='target_store',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='response_rules', to='linker.creatorstore'),
        ),
        migrations.AddField(
            model_name='responsekeyword',
            name='target_type',
            field=models.CharField(choices=[('PRODUCT', 'Product'), ('PRODUCT_BLOCK', 'Product block'), ('STORE', 'Store')], default='STORE', max_length=20),
        ),
        migrations.AddField(
            model_name='responsekeyword',
            name='target_linker',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='response_keyword_targets', to='linker.linker'),
        ),
        migrations.AddField(
            model_name='responsekeyword',
            name='target_product_block',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='response_keyword_block_targets', to='linker.productblock'),
        ),
        migrations.AddField(
            model_name='responsekeyword',
            name='target_store',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='response_keyword_store_targets', to='linker.creatorstore'),
        ),
    ]