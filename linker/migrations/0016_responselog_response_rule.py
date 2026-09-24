from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('linker', '0015_responserule_scope')]
    operations = [migrations.AddField(
        model_name='responselog', name='response_rule',
        field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='response_logs', to='linker.responserule'),
    )]
