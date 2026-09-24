from django.db import migrations, models
import django.db.models.deletion


def backfill_social_identity(apps, schema_editor):
    SocialAccount = apps.get_model('linker', 'SocialAccount')
    CreatorStore = apps.get_model('linker', 'CreatorStore')
    SocialCredential = apps.get_model('linker', 'SocialCredential')
    for account in SocialAccount.objects.all().iterator():
        updates = {}
        if account.external_account_id:
            updates['platform_user_id'] = account.external_account_id
        if account.owner_id:
            store = CreatorStore.objects.filter(owner_id=account.owner_id).first()
            if store:
                updates['store_id'] = store.pk
        credential = SocialCredential.objects.filter(social_account_id=account.pk, provider='META_INSTAGRAM', revoked_at__isnull=True).first()
        if credential:
            updates['connection_status'] = 'CONNECTED'
            updates['granted_scopes'] = credential.scopes or []
            updates['last_verified_at'] = credential.last_verified_at
            updates['connected_at'] = account.created_at
            updates['token_expires_at'] = credential.expires_at
        if updates:
            SocialAccount.objects.filter(pk=account.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [('linker', '0013_linker_display_order_linker_store_visible_and_more')]

    operations = [
        migrations.AddField(
            model_name='socialaccount', name='store',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='social_accounts', to='linker.creatorstore'),
        ),
        migrations.AddField(
            model_name='socialaccount', name='platform_user_id',
            field=models.CharField(blank=True, db_index=True, max_length=255),
        ),
        migrations.AddField(
            model_name='socialaccount', name='username',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='socialaccount', name='profile_picture_url',
            field=models.URLField(blank=True, max_length=1000),
        ),
        migrations.AddField(
            model_name='socialaccount', name='connection_status',
            field=models.CharField(choices=[('NOT_CONNECTED', 'Not connected'), ('PENDING', 'Pending'), ('CONNECTING', 'Connecting'), ('CONNECTED', 'Connected'), ('ERROR', 'Error'), ('REAUTH_REQUIRED', 'Re-auth required')], db_index=True, default='NOT_CONNECTED', max_length=24),
        ),
        migrations.AddField(
            model_name='socialaccount', name='granted_scopes',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='socialaccount', name='connected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='socialaccount', name='last_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_social_identity, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='socialaccount',
            constraint=models.UniqueConstraint(condition=~models.Q(platform_user_id=''), fields=('platform', 'platform_user_id'), name='uniq_platform_user_id'),
        ),
    ]
