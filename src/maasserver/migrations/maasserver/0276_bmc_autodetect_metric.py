# Generated by Django 2.2.12 on 2022-05-24 13:08

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("maasserver", "0275_interface_children"),
    ]

    operations = [
        # All existing rows should have NULL values
        migrations.AddField(
            model_name="bmc",
            name="created_by_commissioning",
            field=models.BooleanField(default=None, editable=False, null=True),
        ),
        # All new rows should default to False.
        migrations.AlterField(
            model_name="bmc",
            name="created_by_commissioning",
            field=models.BooleanField(
                default=False, editable=False, null=True
            ),
        ),
    ]
