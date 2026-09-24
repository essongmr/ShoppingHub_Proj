from django.db import migrations, models
import django.db.models.deletion


def backfill_rule_snapshots(apps, schema_editor):
    ResponseRule = apps.get_model('linker', 'ResponseRule')
    ResponseRule.objects.filter(rendered_message_snapshot='').exclude(test_private_reply_text='').update(
        rendered_message_snapshot=models.F('test_private_reply_text')
    )


class Migration(migrations.Migration):
    dependencies = [('linker', '0020_socialaccount_store_set_null')]

    operations = [
        migrations.CreateModel(
            name='MessageType',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=80, unique=True)),
                ('name', models.CharField(max_length=160)),
                ('description', models.TextField(blank=True)),
                ('active', models.BooleanField(db_index=True, default=True)),
                ('display_order', models.PositiveIntegerField(db_index=True, default=0)),
                ('input_schema', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['display_order', 'id']},
        ),
        migrations.CreateModel(
            name='MessageTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('owner_type', models.CharField(choices=[('SYSTEM', 'System'), ('CREATOR', 'Creator')], db_index=True, default='SYSTEM', max_length=20)),
                ('name', models.CharField(max_length=160)),
                ('description', models.TextField(blank=True)),
                ('body_template', models.TextField()),
                ('active', models.BooleanField(db_index=True, default=True)),
                ('is_default', models.BooleanField(default=False)),
                ('is_recommended', models.BooleanField(default=False)),
                ('version', models.PositiveIntegerField(default=1)),
                ('display_order', models.PositiveIntegerField(db_index=True, default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('creator', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='message_templates', to='auth.user')),
                ('message_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='templates', to='linker.messagetype')),
                ('source_template', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='copies', to='linker.messagetemplate')),
            ],
            options={'ordering': ['display_order', 'id']},
        ),
        migrations.AddField(model_name='responserule', name='message_config', field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name='responserule', name='message_template', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='response_rules', to='linker.messagetemplate')),
        migrations.AddField(model_name='responserule', name='rendered_message_snapshot', field=models.TextField(blank=True)),
        migrations.RunPython(backfill_rule_snapshots, migrations.RunPython.noop),
    ]
