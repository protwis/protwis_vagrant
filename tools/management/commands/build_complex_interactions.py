from django.core.management.base import BaseCommand, CommandError
from build.management.commands.base_build import Command as BaseBuild
from django.core.management import call_command
from django.conf import settings
from django.db import connection
from signprot.models import SignprotComplex

from contactnetwork.cube import *

import logging, json, os

class Command(BaseBuild):

    help = "Function to calculate interaction for only structures of GPCRs in complex with a signaling protein."

    logger = logging.getLogger(__name__)
    pdbs = SignprotComplex.objects.values_list('structure__pdb_code__index', flat=True)


    def add_arguments(self, parser):
        parser.add_argument('-p', '--proc',
            type=int,
            action='store',
            dest='proc',
            default=1,
            help='Number of processes to run')

    def handle(self, *args, **options):        
        try:
            self.logger.info('CREATING COMPLEX INTERACTIONS')
            self.prepare_input(options['proc'], self.pdbs)
        except Exception as msg:
            print(msg)
            self.logger.error(msg)
        self.logger.info('COMPLETED COMPLEX INTERACTIONS')

    def main_func(self, positions, iteration,count,lock):
        pdbs = self.pdbs
        while count.value<len(pdbs):
            with lock:
                pdb = pdbs[count.value]
                count.value +=1 
            try:
                compute_interactions(pdb, True)
            except:
                print('Issue making interactions for',pdb)
