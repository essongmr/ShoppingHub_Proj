from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('linker', '0007_creatorstore'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CreatorProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('onboarding_status', models.CharField(choices=[('ACCOUNT_CREATED', 'Account created'), ('STORE_NAMING', 'Store naming'), ('PROFILE_SELECTION', 'Profile selection'), ('STORE_INTRO', 'Store intro'), ('STORE_ADDRESS', 'Store address'), ('COMPLETED', 'Completed')], db_index=True, default='ACCOUNT_CREATED', max_length=32)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='creator_profile', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
