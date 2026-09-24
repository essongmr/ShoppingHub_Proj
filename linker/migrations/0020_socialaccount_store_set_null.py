from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('linker', '0019_multistore')]

    operations = [
        migrations.AlterField(
            model_name='socialaccount',
            name='store',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='social_accounts',
                to='linker.creatorstore',
            ),
        ),
    ]
