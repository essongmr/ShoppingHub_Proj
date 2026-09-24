import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('linker', '0009_creatorprofile_representative_social_account'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductBlock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('block_number', models.CharField(db_index=True, max_length=40)),
                ('title', models.CharField(max_length=240)),
                ('representative_image', models.ImageField(blank=True, upload_to='creator/product-blocks/')),
                ('description', models.TextField(blank=True)),
                ('active', models.BooleanField(db_index=True, default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='product_blocks', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='ProductBlockItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('linker', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='product_block_items', to='linker.linker')),
                ('product_block', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='linker.productblock')),
            ],
        ),
        migrations.AddConstraint(
            model_name='productblock',
            constraint=models.UniqueConstraint(fields=('owner', 'block_number'), name='uniq_product_block_owner_number'),
        ),
        migrations.AddConstraint(
            model_name='productblockitem',
            constraint=models.UniqueConstraint(fields=('product_block', 'linker'), name='uniq_product_block_item'),
        ),
    ]