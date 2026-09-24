from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('linker', '0021_message_templates_and_rule_snapshots')]

    operations = [
        migrations.AddField(
            model_name='responserule',
            name='starts_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='responserule',
            name='ends_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='responsekeyword',
            name='is_exclusion',
            field=models.BooleanField(default=False),
        ),
    ]
