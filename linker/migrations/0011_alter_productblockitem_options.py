from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('linker', '0010_productblock_productblockitem'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='productblockitem',
            options={'ordering': ['sort_order', 'id']},
        ),
    ]