from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0018_identity_corroboration")]

    operations = [
        migrations.CreateModel(
            name="RefreshState",
            fields=[
                ("step", models.CharField(max_length=100, primary_key=True, serialize=False)),
                ("last_success", models.DateTimeField()),
            ],
        ),
    ]
