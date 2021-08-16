# Generated by Django 3.1.7 on 2021-08-16 14:02

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('common', '0005_auto_20210725_1110'),
        ('ligand', '0015_auto_20210727_1533'),
        ('protein', '0013_auto_20210816_1115'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='ProteinGProteinPair',
            new_name='ProteinCouplings',
        ),
        migrations.AlterModelTable(
            name='proteincouplings',
            table='protein_couplings',
        ),
        migrations.DeleteModel(
            name='ProteinArrestinPair',
        ),
    ]
