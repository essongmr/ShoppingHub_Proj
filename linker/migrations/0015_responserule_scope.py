from django.db import migrations, models
import django.db.models.deletion


def migrate_existing_rules(apps, schema_editor):
    ResponseRule = apps.get_model('linker', 'ResponseRule')
    for rule in ResponseRule.objects.select_related('social_content__social_account__store'):
        content = rule.social_content
        rule.scope = 'CONTENT'
        rule.store_id = content.social_account.store_id if content and content.social_account else None
        rule.save(update_fields=['scope', 'store'])


class Migration(migrations.Migration):
    dependencies = [('linker', '0014_socialaccount_store_identity')]
    operations = [
        migrations.AddField(model_name='responserule', name='scope', field=models.CharField(choices=[('STORE', 'Store'), ('ACCOUNT', 'Account'), ('CONTENT', 'Content')], db_index=True, default='CONTENT', max_length=20)),
        migrations.AddField(model_name='responserule', name='store', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='scope_response_rules', to='linker.creatorstore')),
        migrations.AddField(model_name='responserule', name='social_account', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='scope_response_rules', to='linker.socialaccount')),
        migrations.AlterField(model_name='responserule', name='social_content', field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='response_rule', to='linker.socialcontent')),
        migrations.RunPython(migrate_existing_rules, migrations.RunPython.noop),
    ]
