from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import connection
from django.utils.text import slugify
from django.db import IntegrityError

from build.management.commands.base_build import Command as BaseBuild
from build.management.commands.build_ligand_functions import get_or_create_ligand, match_id_via_unichem
from protein.models import (Protein, ProteinConformation, ProteinState, ProteinAnomaly, ProteinAnomalyType,
    ProteinSegment)
from residue.models import ResidueGenericNumber, ResidueNumberingScheme, Residue, ResidueGenericNumberEquivalent
from common.models import WebLink, WebResource, Publication
from structure.models import Structure, StructureType, StructureStabilizingAgent,PdbData, Rotamer, Fragment
from construct.functions import *

from contactnetwork.models import *
import contactnetwork.interaction as ci
from contactnetwork.cube import compute_interactions

from Bio.PDB import PDBParser,PPBuilder
from Bio import pairwise2

from structure.assign_generic_numbers_gpcr import GenericNumbering
from structure.functions import StructureBuildCheck, ParseStructureCSV
from ligand.models import Ligand, LigandType, LigandRole, LigandPeptideStructure
from interaction.models import *
from interaction.views import runcalculation,parsecalculation
from residue.functions import dgn

import logging
import os
import re
import yaml
import time
import gc
from collections import OrderedDict
import json
from urllib.request import urlopen
from Bio.PDB import parse_pdb_header
from Bio.PDB.Selection import *


## FOR VIGNIR ORDERED DICT YAML IMPORT/DUMP
_mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG
def dict_constructor(loader, node):
    return OrderedDict(loader.construct_pairs(node))

def represent_ordereddict(dumper, data):
    value = []

    for item_key, item_value in data.items():
        node_key = dumper.represent_data(item_key)
        node_value = dumper.represent_data(item_value)

        value.append((node_key, node_value))

    return yaml.nodes.MappingNode(u'tag:yaml.org,2002:map', value)

yaml.add_representer(OrderedDict, represent_ordereddict)
yaml.add_constructor(_mapping_tag, dict_constructor)

class Command(BaseBuild):
    help = 'Reads source data and creates pdb structure records'

    def add_arguments(self, parser):
        parser.add_argument('-p', '--proc',
            type=int,
            action='store',
            dest='proc',
            default=1,
            help='Number of processes to run')
        parser.add_argument('-s', '--structure',
            dest='structure',
            help='Structure to import (PDB ID)',
            nargs='+')
        parser.add_argument('-u', '--purge',
            action='store_true',
            dest='purge',
            default=False,
            help='Purge existing records')
        parser.add_argument('--skip_cn',
            action='store_true',
            default=False,
            help='Skip building contact network for test build')
        parser.add_argument('-i', '--incremental',
            action='store_true',
            dest='incremental',
            default=False,
            help='Incremental update to structures for small live update')
        parser.add_argument('--debug',
            action='store_true',
            dest='debug',
            default=False,
            help='Print info for debugging')

    # source file directory
    pdb_data_dir = os.sep.join([settings.DATA_DIR, 'structure_data', 'pdbs'])

    ### USE below to fix seg ends
    xtal_seg_end_file = os.sep.join([settings.DATA_DIR, 'structure_data', 'annotation', 'mod_xtal_segends.yaml'])
    with open(xtal_seg_end_file, 'r') as f:
        xtal_seg_ends = yaml.load(f, Loader=yaml.FullLoader)

    xtal_anomalies_file = os.sep.join([settings.DATA_DIR, 'structure_data', 'annotation', 'all_anomalities.yaml'])
    with open(xtal_anomalies_file, 'r') as f2:
        xtal_anomalies = yaml.load(f2, Loader=yaml.FullLoader)

    xtal_representatives = os.sep.join([settings.DATA_DIR, 'structure_data', 'annotation', 'xtal_representatives.yaml'])
    with open(xtal_representatives, 'r') as f3:
        xtal_representatives = yaml.load(f3, Loader=yaml.FullLoader)

    s = ProteinSegment.objects.all()
    segments = {}
    for segment in s:
        segments[segment.slug] = segment


    def handle(self, *args, **options):
        # delete any existing structure data
        if options['purge']:
            try:
                self.purge_structures()
            except Exception as msg:
                print(msg)
                self.logger.error(msg)

        if options['skip_cn']:
            self.run_contactnetwork=False
        else:
            self.run_contactnetwork=True

        self.parsed_structures = ParseStructureCSV()
        self.parsed_structures.parse_ligands()
        self.parsed_structures.parse_nanobodies()
        self.parsed_structures.parse_fusion_proteins()
        self.parsed_structures.parse_ramp()
        self.parsed_structures.parse_grk()

        if options['structure']:
            self.parsed_structures.pdb_ids = [i for i in self.parsed_structures.pdb_ids if i in options['structure'] or i.lower() in options['structure']]

        if options['incremental']:
            self.incremental_mode = True
        else:
            self.incremental_mode = False

        self.debug = options['debug']

        try:
            self.logger.info('CREATING STRUCTURES')
            # run the function twice (once for representative structures, once for non-representative)
            iterations = 1
            for i in range(1,iterations+1):
                self.prepare_input(options['proc'], self.parsed_structures.pdb_ids, i)

            self.logger.info('COMPLETED CREATING STRUCTURES')
        except Exception as msg:
            print(msg)
            self.logger.error(msg)

    @staticmethod
    def queryset_iterator(qs, batchsize = 5000, gc_collect = True):
        # See https://www.guguweb.com/2020/03/27/optimize-django-memory-usage/
        iterator = qs.values_list('pk', flat=True).order_by('pk').distinct().iterator()
        eof = False
        while not eof:
            primary_key_buffer = []
            try:
                while len(primary_key_buffer) < batchsize:
                    primary_key_buffer.append(iterator.__next__())
            except StopIteration:
                eof = True
            for obj in qs.filter(pk__in=primary_key_buffer).order_by('pk').iterator():
                yield obj
            if gc_collect:
                gc.collect()

    def purge_structures(self):
        Structure.objects.all().delete()
        ResidueFragmentInteraction.objects.all().delete()
        ResidueFragmentInteractionType.objects.all().delete()
        StructureLigandInteraction.objects.all().delete()
        #Remove previous Rotamers/Residues to prepare repopulate
        Fragment.objects.all().delete()
        Rotamer.objects.all().delete()

        # FOR DEBUGGING - using queryset_iterator as otherwise the
        # memory was exceeded when deleting >300K PDBs simultaneously
        #for obj in self.queryset_iterator(PdbData.objects.all()):
        #    obj.delete()
        PdbData.objects.all().delete()

    def create_rotamers(self, structure, pdb_path,d):
        wt_lookup = {} #used to match WT seq_number to WT residue record
        pdbseq = {} #used to keep track of pdbseq residue positions vs index in seq
        ref_positions = {} #WT postions in alignment
        mapped_seq = {} # index in contruct, tuple of AA and WT [position,AA]
        debug = False
        preferred_chain = structure.preferred_chain
        wt_pdb_lookup = []

        if len(preferred_chain.split(','))>1: #if A,B
            preferred_chain = preferred_chain.split(',')[0]


        AA = {'ALA':'A', 'ARG':'R', 'ASN':'N', 'ASP':'D',
             'CYS':'C', 'GLN':'Q', 'GLU':'E', 'GLY':'G',
             'HIS':'H', 'ILE':'I', 'LEU':'L', 'LYS':'K',
             'MET':'M', 'PHE':'F', 'PRO':'P', 'SER':'S',
             'THR':'T', 'TRP':'W', 'TYR':'Y', 'VAL':'V',
             'YCM':'C', 'CSD':'C', 'TYS':'Y', 'SEP':'S', 'TPO':'T'} #non-standard AAs

        atom_num_dict = {'E':9, 'S':6, 'Y':12, 'G':4, 'A':5, 'V':7, 'M':8, 'L':8, 'I':8, 'T':7, 'F':11, 'H':10, 'K':9,
                         'D':8, 'C':6, 'R':11, 'P':7, 'Q':9, 'N':8, 'W':14}


        entry_name = d['construct_crystal']['uniprot']

        # print(d['xml_segments'])
        # print(d['deletions'])
        deletions = []
        ## Remove WT ranges that arent in xtal (no need to try to map them)
        # print(d)
        if 'deletions' in d:
            for del_range in d['deletions']:
                if del_range['start']==146 and structure.pdb_code.index=='4K5Y':
                    #Manual fix for faulty 4K5Y annotation
                    continue
                for i in range(del_range['start'],del_range['end']+1):
                    deletions.append(i)
            #print("Annotation missing WT residues",d['deletions'])
        removed = []
        ## Remove segments that arent receptor (tags, fusion etc)
        if 'xml_segments' in d:
            for seg in d['xml_segments']:
                if seg[1]:
                    # Odd rules to fit everything..
                    # print(seg[1][0], entry_name)
                    if seg[1][0]!=entry_name and seg[-1]!=True and seg[1][0]!='Uncharacterized protein' and 'receptor' not in seg[1][0]:
                        if seg[0].split("_")[1]==preferred_chain:
                            #print(seg[2],seg[3]+1)
                            #for i in range(seg[2],seg[3]+1):
                            # print(seg)
                            for i in seg[6]:
                                removed.append(i)
        # Reset removed, since it causes more problems than not

        # Overwrite reset to fix annotation
        if structure.pdb_code.index in ['6H7N','6H7J','6H7L','6H7M','6H7O']:
            removed = list(range(3,40))
            deletions = deletions+list(range(244,272))
        elif structure.pdb_code.index=='6MEO':
            removed = []
        elif structure.pdb_code.index=='5N2R':
            deletions = [1]+list(range(209,219))+list(range(306,413))
        elif structure.pdb_code.index in ['5WIU','5WIV']:
            removed = removed+[1001]
        elif structure.pdb_code.index=='6QZH':
            removed = list(range(1001,1473))+list(range(255,260))
        elif structure.pdb_code.index in ['6KUX', '6KUY']:
            deletions = list(range(1,20))
        elif structure.pdb_code.index=='7BZ2':
            deletions = list(range(240,265))
        elif structure.pdb_code.index=='7C6A':
            removed = list(range(1,35))
        elif structure.pdb_code.index=='6S0L':
            removed = [-1,0] + list(range(1001,1107))
        elif structure.pdb_code.index=='7D7M':
            deletions = list(range(1,4)) + list(range(367,489))
        elif structure.pdb_code.index in ['7D77', '7D76', '4GRV']:
            deletions = []
        elif structure.pdb_code.index=='6A94':
            removed.remove(69)
            deletions.remove(69)
        elif structure.pdb_code.index in ['6LI1']:
            for i in range(261,265):
                removed.remove(i)
                deletions.remove(i)
        elif structure.pdb_code.index=='6LI2':
            for i in range(263,265):
                removed.remove(i)
                deletions.remove(i)
        elif structure.pdb_code.index=='5JQH':
            for i in range(1023,1030):
                removed.remove(i)
            for i in range(23,30):
                deletions.remove(i)
        elif structure.pdb_code.index=='5T1A':
            for i in range(226,241):
                deletions.remove(i)
            removed.append(1002)
            removed.remove(234)
            removed.remove(319)
            removed.remove(320)
        elif structure.pdb_code.index=='5UEN':
            for i in range(220,228):
                removed.remove(i)
                deletions.remove(i)
        elif structure.pdb_code.index=='3SN6':
            removed = list(range(1002,1161))
        elif structure.pdb_code.index in ['6ZDV','6ZDR','6MH8','6PS7','6S0Q','6WQA','6AQF','6GT3','6JZH','6LPJ','6LPL','6LPK',
                                          '5JTB','5OLH','5NM2','5OLG','5OM1','5OLO','5OLZ','5OLV','5OM4','5UVI','5VRA']:
            removed.remove(1)
            deletions.remove(1)
        elif structure.pdb_code.index in ['5NLX','5NM4']:
            removed.remove(10)
            deletions.remove(1)
            for i in range(209,214):
                deletions.remove(i)
            for i in range(218,223):
                removed.remove(i)
        elif structure.pdb_code.index=='6N48':
            for i in range(1023,1029):
                removed.remove(i)
            for i in range(23,29):
                deletions.remove(i)
        elif structure.pdb_code.index=='5ZK3':
            deletions.remove(382)
        elif structure.pdb_code.index=='6A93':
            removed.remove(69)
            deletions.remove(69)
        elif structure.pdb_code.index=='6IBL':
            removed = list(range(1003,1110))
            for i in range(41,44):
                deletions.remove(i)
            deletions.remove(243)
            deletions.append(271)
        elif structure.pdb_code.index=='6LUQ':
            removed.remove(387)
            deletions.remove(366)
        elif structure.pdb_code.index=='6W2Y':
            for i in range(845,862):
                deletions.remove(i)
        elif structure.pdb_code.index in ['4Z34','4Z35','4Z36']:
            removed.remove(327)
            deletions.remove(327)
        elif structure.pdb_code.index=='6TKO':
            removed.remove(358)
            deletions.remove(358)
        elif structure.pdb_code.index=='6DO1':
            removed = []
        elif structure.pdb_code.index=='5D6L':
            for i in range(224,231):
                removed.remove(i)
                deletions.remove(i)
        elif structure.pdb_code.index=='7DFL':
            deletions = list(range(222,405))
        elif structure.pdb_code.index=='2I35':
            removed = [330,331,332]
        elif structure.pdb_code.index in ['7ARO', '7RM5']:
            removed.remove(1)
            deletions.remove(1)
        elif structure.pdb_code.index in ['7BTS','7BU6','7BU7','7BVQ']:
            removed, deletions = list(range(884,1054)), list(range(884,1054))
        elif structure.pdb_code.index=='7D68':
            for i in range(395,456):
                deletions.remove(i)
        elif structure.pdb_code.index=='7EB2':
            deletions, removed = [], []
        elif structure.pdb_code.index=='7F1R':
            deletions = list(range(314,400))
            removed = list(range(1,128))+list(range(188,192))
        elif structure.pdb_code.index=='7F1Q':
            removed = list(range(1,113))
        elif structure.pdb_code.index in ['7EPE','7EPF']:
            removed, deletions = list(range(1000,1148)), list(range(1000,1148))
        elif structure.pdb_code.index in ['7EZM','7EZK','7EZH']:
            for i in range(38,64):
                removed.remove(i)
        elif structure.pdb_code.index in ['6ZFZ', '6ZG4', '6ZG9']:
            for i in range(21,27):
                removed.remove(i)
                deletions.remove(i)
        elif structure.pdb_code.index=='7EWR':
            removed, deletions = [], []
        elif structure.pdb_code.index in ['7T10', '7T11']:
            deletions = []
        elif structure.pdb_code.index in ['7PX4','7PYR']:
            deletions.remove(1)
            removed.remove(1)
        elif structure.pdb_code.index=='7B6W':
            deletions = list(range(352,525))
        elif structure.pdb_code.index=='7V9M':
            deletions = []
        elif structure.pdb_code.index=='7F4F':
            deletions = []
        elif structure.pdb_code.index in ['7EVY','7EVZ','7EW0']:
            removed = list(range(1,47))
        elif structure.pdb_code.index=='7VAB':
            removed = list(range(1,127))
        elif structure.pdb_code.index=='7SBF':
            for i in range(65,73):
                deletions.remove(i)
        elif structure.pdb_code.index in ['7SF7','7SF8']:
            deletions, removed = [], []

        if self.debug:
            print('Deletions: ', deletions)
            print('Removed: ', removed)
        if len(deletions)>len(d['wt_seq'])*0.9:
            #if too many deletions
            removed = []
            deletions = []

        s = PDBParser(PERMISSIVE=True, QUIET=True).get_structure('ref', pdb_path)[0]
        chain = s[preferred_chain] #select only one chain (avoid n-mer receptors)

        ppb=PPBuilder()
        seq = ''
        i = 1

        # Checking Na+ atom in xtal
        parent_prot_conf = ProteinConformation.objects.get(protein=structure.protein_conformation.protein.parent)
        try:
            wt_2x50 = Residue.objects.get(protein_conformation=parent_prot_conf, display_generic_number__label=dgn('2x50',parent_prot_conf))
            wt_3x39 = Residue.objects.get(protein_conformation=parent_prot_conf, display_generic_number__label=dgn('3x39',parent_prot_conf))
            if wt_2x50.amino_acid=='D' and wt_3x39.amino_acid=='S':
                if chain[wt_2x50.sequence_number].get_resname()=='ASP' and chain[wt_3x39.sequence_number].get_resname()=='SER':
                    v_2x50 = chain[wt_2x50.sequence_number]['OD1'].get_vector()
                    v_3x39 = chain[wt_3x39.sequence_number]['OG'].get_vector()
                    all_resis = uniqueify(chain)
                    for r in all_resis:
                        id_ = r.get_id()
                        if id_[0]=='H_ NA':
                            v_na = r['NA'].get_vector()
                            d_2x50 = (v_na-v_2x50).norm()
                            d_3x39 = (v_na-v_3x39).norm()
                            if d_2x50<3 and d_3x39<3:
                                structure.sodium = True
                                structure.save()
                                break
        except:
            pass

        check_1000 = 0
        prev_id = 0
        bigjump = False
        all_pdb_residues_in_chain = 0
        for pp in ppb.build_peptides(chain, aa_only=False): #remove >1000 pos (fusion protein / gprotein)
            for i,res in enumerate(pp,1 ):
                all_pdb_residues_in_chain += 1
                residue_id = res.get_full_id()

        if len(removed)+100>all_pdb_residues_in_chain:
            print(structure,'More (or almost) sequence set to be removed from sequence',len(removed),' than exists',all_pdb_residues_in_chain,' removing removed[]')
            #print(removed)
            removed = []

        for pp in ppb.build_peptides(chain, aa_only=False): #remove >1000 pos (fusion protein / gprotein)
            for i,res in enumerate(pp,1 ):
                id = res.id
                residue_id = res.get_full_id()
                if id[1] in removed:
                    chain.detach_child(id)
                    continue
                # if id[1]<600:
                #     check_1000 += 1
                #     #need check_1000 to catch structures where they lie in 1000s (4LDE, 4LDL, 4LDO, 4N4W, 4QKX)
                # if structure.pdb_code.index in ["4RWD","3SN6","4L6R"] and id[1]>1000:
                #     last_valid = 0
                #     bigjump = True
                #     removed.append(id[1])
                # if (id[1]-prev_id)>100 and check_1000>150:
                #     last_valid = prev_id
                #     bigjump = True
                # if bigjump:
                #     if (id[1]-last_valid)<100 or (id[1]<1000 and (id[1]-last_valid)<300 ):
                #         bigjump = False
                # if (id[1]>1000 or bigjump) and check_1000>150 and not (structure.pdb_code.index=="4PHU" and id[1]>2000):
                #     chain.detach_child(id)
                #     #print("removing",id)
                #     removed.append(id[1])
                prev_id = id[1]
        ranges = []
        for k, g in groupby(enumerate(removed), lambda x:x[0]-x[1]):
            group = list(map(itemgetter(1), g))
            ranges.append((group[0], group[-1]))
        if debug: print("Removed XTAL positions due to not being WT receptor",ranges)
        i = 1
        for pp in ppb.build_peptides(chain, aa_only=False):
            seq += str(pp.get_sequence()) #get seq from fasta (only chain A)
            for residue in pp:
                residue_id = residue.get_full_id()
                chain = residue_id[2]
                if chain not in pdbseq:
                    pdbseq[chain] = {}
                pos = residue_id[3][1]

                if residue.resname != "NH2": # skip amidation of peptide
                    pdbseq[chain][pos] = [i, AA[residue.resname]]
                    i += 1

        parent_seq_protein = str(structure.protein_conformation.protein.parent.sequence)
        # print(structure.protein_conformation.protein.parent.entry_name)
        rs = Residue.objects.filter(protein_conformation__protein=structure.protein_conformation.protein.parent).prefetch_related('display_generic_number','generic_number','protein_segment')

        if structure.pdb_code.index.upper() in self.xtal_seg_ends:
            seg_ends = self.xtal_seg_ends[structure.pdb_code.index.upper()]
        else:
            seg_ends = []
            # print('No SEG ENDS info for {}'.format(structure.pdb_code.index))
            self.logger.info('No SEG ENDS info for {}'.format(structure.pdb_code.index))

        parent_seq = ""
        for i,r in enumerate(rs.exclude(sequence_number__in = deletions),1): #required to match WT position to a record (for duplication of GN values)
            # wt_lookup[r.sequence_number] = r
            wt_lookup[i] = r
            parent_seq += r.amino_acid
        # if parent_seq != parent_seq_protein:
        #     print('Residues sequence differ from sequence in protein',structure.protein_conformation.protein.parent.entry_name,structure.pdb_code.index)

        if len(wt_lookup)==0:
            print("No residues for",structure.protein_conformation.protein.parent.entry_name)
            return None

        if self.debug:
            print('parent_seq-pdb_seq length',len(parent_seq),'pdb_seq',len(seq))
            print(parent_seq)
            print(seq)
        #align WT with structure seq -- make gaps penalties big, so to avoid too much overfitting

        if structure.pdb_code.index=='6U1N':
            seq = seq[:265]
        elif structure.pdb_code.index in ['1GZM', '3C9L']:
            seq = seq[:-3]
        if structure.pdb_code.index in ['6NBI','6NBF','6NBH','6U1N','6M1H','6PWC','7JVR','7SHF','7EJ0','7EJ8','7EJA','7EJK']:
            pw2 = pairwise2.align.localms(parent_seq, seq, 3, -4, -3, -1)
        elif structure.pdb_code.index in ['6KUX', '6KUY', '6KUW']:
            pw2 = pairwise2.align.localms(parent_seq, seq, 3, -4, -4, -1.5)
        else:
            pw2 = pairwise2.align.localms(parent_seq, seq, 3, -4, -5, -2)

        gaps = 0
        unmapped_ref = {}
        ref_seq, temp_seq = str(pw2[0][0]), str(pw2[0][1])
        # if structure.pdb_code.index in ['5WIU','5WIV']:
        #     temp_seq = temp_seq[:144]+'D'+temp_seq[145:]
        #     temp_seq = temp_seq[:149]+'-'+temp_seq[150:]
        # Custom fixes for alignment issues
        if structure.pdb_code.index=='5ZKP':
            ref_seq = ref_seq[:197]+'-'+ref_seq[198:]
            ref_seq = ref_seq[:198]+'A'+ref_seq[199:]
        elif structure.pdb_code.index in ['5VEW','5VEX']:
            ref_seq = ref_seq[:164]+'IG'+ref_seq[167:]
            temp_seq = temp_seq[:166]+temp_seq[167:]
        elif structure.pdb_code.index in ['3V2W']:
            ref_seq = ref_seq[:201]+ref_seq[202:]
            temp_seq = temp_seq[:207]+temp_seq[208:]
        elif structure.pdb_code.index in ['3V2Y']:
            ref_seq = ref_seq[:209]+ref_seq[210:]
            temp_seq = temp_seq[:215]+temp_seq[216:]
        elif structure.pdb_code.index in ['6KUX','6KUY']:
            ref_seq = ref_seq[:416]+('-'*(416-233))+ref_seq[416:]
            temp_seq = temp_seq[:233]+('-'*(416-233))+temp_seq[233:]
        elif structure.pdb_code.index in ['6KJV','6KK1','6KK7']:
            ref_seq = ref_seq[:176]+ref_seq[177:]
            temp_seq = temp_seq[:178]+temp_seq[179:]
        elif structure.pdb_code.index in ['6LN2']:
            ref_seq = ref_seq[:292]+ref_seq[293:]
            temp_seq = temp_seq[:294]+temp_seq[295:]
        elif structure.pdb_code.index in ['6WHA']:
            temp_seq = temp_seq[:76]+'P--'+temp_seq[79:]
        elif structure.pdb_code.index in ['6X18']:
            temp_seq = temp_seq[:105]+'S------'+temp_seq[112:]
        elif structure.pdb_code.index in ['6K41']:
            temp_seq = temp_seq[:71]+'W------'+temp_seq[78:]
        elif structure.pdb_code.index in ['6LPB']:
            temp_seq = temp_seq[:316]+'S--------'+temp_seq[325:]
        elif structure.pdb_code.index in ['6RZ5']:
            temp_seq = temp_seq[:221]+'K-'+temp_seq[223:]
        elif structure.pdb_code.index in ['6TPK']:
            temp_seq = temp_seq[:30]+'H-----'+temp_seq[36:]
        elif structure.pdb_code.index in ['6W25']:
            temp_seq = temp_seq[:94]+'T--'+temp_seq[97:]
        elif structure.pdb_code.index in ['7C61']:
            temp_seq = temp_seq[:207]+'-'+temp_seq[207:225]+'-'+temp_seq[225:238]+'-'+temp_seq[238:251]+'-'+temp_seq[251:]
            ref_seq = ref_seq[:304]+'----'+ref_seq[304:]
        elif structure.pdb_code.index in ['6WHC']:
            temp_seq = temp_seq[:202]+'S----------'+temp_seq[213:]
        elif structure.pdb_code.index=='5T1A':
            temp_seq = temp_seq[:229]+temp_seq[231:238]+temp_seq[239:242]+temp_seq[245:]
            ref_seq = ref_seq[:224]+ref_seq[227:233]+ref_seq[236:]
        elif structure.pdb_code.index=='5UEN':
            temp_seq = temp_seq[:218]+temp_seq[222:]
            ref_seq = ref_seq[:210]+ref_seq[214:]
        elif structure.pdb_code.index=='6DO1':
            temp_seq = temp_seq[:228]+temp_seq[230:]
            ref_seq = ref_seq[:225]+ref_seq[227:]
        elif structure.pdb_code.index=='7KH0':
            temp_seq = temp_seq[:240]+'S'+temp_seq[240:262]+temp_seq[263:]
        elif structure.pdb_code.index=='7BB6':
            temp_seq = temp_seq[:231]+'A'+temp_seq[231:257]+temp_seq[258:]
        elif structure.pdb_code.index=='7C4S':
            temp_seq = temp_seq[:224]+'S'+temp_seq[224:234]+temp_seq[235:]
        elif structure.pdb_code.index=='7M3J':
            temp_seq = temp_seq[:100]+'I'+temp_seq[100:115]+temp_seq[116:337]+'N'+temp_seq[337:380]+temp_seq[381:]
        elif structure.pdb_code.index=='7DUQ':
            temp_seq = temp_seq[:105]+'S------'+temp_seq[112:]
        elif structure.pdb_code.index in ['7KI0','7KI1']:
            temp_seq = temp_seq[:105]+'S-------'+temp_seq[113:]
        elif structure.pdb_code.index=='7MTQ':
            temp_seq = temp_seq[:694]+'E---'+temp_seq[698:]
        elif structure.pdb_code.index=='7FD9':
            temp_seq = temp_seq[:99]+'S'+temp_seq[99:118]+temp_seq[119:]
        elif structure.pdb_code.index in ['6ZFZ', '6ZG4', '6ZG9']:
            temp_seq = temp_seq[:28]+temp_seq[29:207]+temp_seq[208:]
            ref_seq = ref_seq[:23]+ref_seq[24:211]+ref_seq[212:]
        elif structure.pdb_code.index in ['7NA7', '7NA8']:
            temp_seq = temp_seq[:242]+'R'+temp_seq[242:253]+temp_seq[254:]
        elif structure.pdb_code.index in ['7F8V', '7F8W']:
            temp_seq = temp_seq[:247]+'L'+temp_seq[247:323]+temp_seq[324:]
        elif structure.pdb_code.index=='7RTB':
            temp_seq = temp_seq[:107]+'S'+temp_seq[107:113]+temp_seq[114:]
        elif structure.pdb_code.index=='6Z4Q':
            temp_seq = temp_seq[:131]+'H-'+temp_seq[133:]
        elif structure.pdb_code.index=='6ZA8':
            temp_seq = temp_seq[:212]+'L'+temp_seq[212:219]+temp_seq[220:]
        elif structure.pdb_code.index=='7EO4':
            temp_seq = temp_seq[:36]+'I'+temp_seq[36:44]+temp_seq[45:]
        elif structure.pdb_code.index in ['7EWP','7EWR']:
            temp_seq = temp_seq[:667]+'S'+temp_seq[667:701]+temp_seq[702:]
        elif structure.pdb_code.index in ['7T10','7T11']:
            temp_seq = temp_seq[:237]+temp_seq[239:246]+'R'+temp_seq[251:]
            ref_seq = ref_seq[:244]+ref_seq[245:253]+ref_seq[258:]
        elif structure.pdb_code.index in ['7FIY']:
            temp_seq = temp_seq[:306]+'R---'+temp_seq[310:]
        elif structure.pdb_code.index in ['7B6W']:
            temp_seq = temp_seq[:318]+'L----'+temp_seq[323:]
            temp_seq = temp_seq[:245]+temp_seq[246:282]+'-S'+temp_seq[283:]
        elif structure.pdb_code.index in ['7SIL','7SIM','7SIN']:
            temp_seq = temp_seq[:702]+'L'+temp_seq[702:720]+temp_seq[721:]
        elif structure.pdb_code.index in ['7WIH']:
            temp_seq = temp_seq[:26]+'E'+temp_seq[26:33]+temp_seq[34:94]+'L'+temp_seq[94:117]+temp_seq[118:]
        elif structure.pdb_code.index=='7SBF':
            temp_seq = temp_seq[:8]+temp_seq[10:]
            ref_seq = ref_seq[2:]
        elif structure.pdb_code.index=='7RA3':
            temp_seq = temp_seq[:306]+'R'+temp_seq[306:311]+temp_seq[312:]
        elif structure.pdb_code.index in ['7EJ8']:
            temp_seq = temp_seq[:182]+'P'+temp_seq[182:200]+temp_seq[201:]
        elif structure.pdb_code.index in ['7EJ0']:
            temp_seq = temp_seq[:242]+'R'+temp_seq[242:377]+temp_seq[378:]
        elif structure.pdb_code.index in ['7EJK']:
            temp_seq = temp_seq[:182]+'P'+temp_seq[182:200]+temp_seq[201:242]+'R'+temp_seq[242:379]+temp_seq[380:]
        elif structure.pdb_code.index=='7EZC':
            temp_seq = temp_seq[:146]+'Q'+temp_seq[146:155]+temp_seq[156:]
        elif structure.pdb_code.index=='7RBT':
            temp_seq = temp_seq[:306]+'R'+temp_seq[306:311]+temp_seq[312:]
        elif structure.pdb_code.index=='7WUJ':
            temp_seq = temp_seq[:158]+'T'+temp_seq[158:163]+temp_seq[164:]
        


        for i, r in enumerate(ref_seq, 1): #loop over alignment to create lookups (track pos)
            if self.debug:
                print(i,r,temp_seq[i-1]) #print alignment for sanity check
            if r == "-":
                gaps += 1
            if r != "-":
                ref_positions[i] = [i-gaps,r]
            elif r == "-":
                ref_positions[i] = [None,'-']

            if temp_seq[i-1]=='-':
                unmapped_ref[i-gaps] = '-'

        gaps = 0
        for i, r in enumerate(temp_seq, 1): #make second lookup
            # print(i,r,ref_seq[i-1]) #print alignment for sanity check
            if r == "-":
                gaps += 1
            if r != "-":
                mapped_seq[i-gaps] = [r,ref_positions[i]]
                # if r!=ref_seq[i-1]:
                #     print('aa mismatch')
        # print("seg res not mapped",gaps)

        pdb = structure.pdb_data.pdb
        protein_conformation=structure.protein_conformation
        temp = ''
        check = 0
        errors = 0
        mismatch_seq = 0
        match_seq = 0
        not_matched = 0
        matched_by_pos = 0
        aa_mismatch = 0
        generic_change = 0
        generic_change_from = []

        pdblines_temp = pdb.splitlines()
        pdblines = []
        for line in pdblines_temp: #Get rid of all odd records
            if line.startswith('ATOM') or (line[17:20] in ['YCM','CSD','TYS','SEP'] and line.startswith('HETATM')):
                pdblines.append(line)
            # Only build residues for the first model
            if line.startswith('MODEL        2'):
                break
        pdblines.append('') #add a line to not "run out"
        rotamer_bulk = []
        rotamer_data_bulk = []
        residues_bulk = []
        if structure.pdb_code.index=='5LWE':
            seg_ends['5b'] = 209
            seg_ends['5e'] = 244
        # import pprint
        # pprint.pprint(wt_lookup)
        # pprint.pprint(mapped_seq)
        # pprint.pprint(unmapped_ref)
        # print('deletions: ',deletions)
        # print('removed: ',removed)
        for i,line in enumerate(pdblines):
            # print(line)
            if line.startswith('ATOM') or (line[17:20] in ['YCM','CSD','TYS','SEP'] and line.startswith('HETATM')):
                # if line[17:20] in ['YCM','CSD','TYS','SEP']: # sanity check for non-standard helix residues
                #     print(line)
                chain = line[21]
                if preferred_chain and chain!=preferred_chain: #If perferred is defined and is not the same as the current line, then skip
                    pass
                else:
                    nextline = pdblines[i+1]
                    residue_number = line[22:26].strip()
                    if (check==0 or nextline[22:26].strip()==check) and nextline.startswith('TER')==False and (nextline.startswith('ATOM')==True or nextline.startswith('HETATM')==True): #If this is either the begining or the same as previous line add to current rotamer
                        temp += line + "\n"
                        #print('same res',pdb.splitlines()[i+1])
                    else: #if this is a new residue
                        #print(pdb.splitlines()[i+1][22:26].strip(),check)
                        temp += line + "\n"
                        if structure.pdb_code.index=='7E9H' and line[17:20]=='SEP':
                            continue
                        #(int(check.strip())<2000 or structure.pdb_code.index=="4PHU") and
                        if int(check.strip()) not in removed:
                            # print(line)
                            residue = Residue()
                            residue.sequence_number = int(check.strip())
                            residue.amino_acid = AA[residue_name.upper()]
                            residue.protein_conformation = protein_conformation

                            try:
                                seq_num_pos = pdbseq[chain][residue.sequence_number][0]
                            except:
                                # print('failed residue',pdb_path,residue.sequence_number)
                                temp = "" #start new line for rotamer
                                check = pdblines[i+1][22:26].strip()
                                continue
                            # print('hi',seq_num_pos,residue.sequence_number)
                            if seq_num_pos in mapped_seq:
                                # print(int(check.strip()),seq_num_pos) #sanity check
                                if mapped_seq[seq_num_pos][1][0]==None:
                                    # print('no match found') #sanity check
                                    # print(residue.sequence_number,residue.amino_acid) #sanity check
                                    residue.display_generic_number = None
                                    residue.generic_number = None
                                    residue.protein_segment = None
                                    not_matched +=1
                                else:
                                    wt_r = wt_lookup[mapped_seq[seq_num_pos][1][0]]
                                    # print(seq_num_pos, mapped_seq[seq_num_pos], wt_r, residue.sequence_number, residue.amino_acid) #sanity check for mapped resis
                                    if residue.sequence_number!=wt_r.sequence_number and residue.amino_acid!=wt_r.amino_acid and residue.sequence_number in wt_lookup: #if pos numbers not work -- see if the pos number might be in WT and unmapped
                                        if wt_lookup[residue.sequence_number].amino_acid==residue.amino_acid:
                                            if residue.sequence_number in unmapped_ref: #WT was not mapped, so could be it
                                                # print(residue.sequence_number,residue.amino_acid) #sanity check
                                                # print('wrongly matched, better match on pos+aa',residue.sequence_number,residue.amino_acid,wt_r.sequence_number,wt_r.amino_acid)
                                                wt_r = wt_lookup[residue.sequence_number]
                                                matched_by_pos +=1
                                                match_seq += 1
                                            else:
                                                mismatch_seq += 1
                                                ### REPLACE seq number with WT to fix odd PDB annotation. FIXME kinda dangerous, but best way to ensure consistent GN numbering
                                                residue.sequence_number = wt_r.sequence_number
                                                #print('could have been matched, but already aligned to another position',residue.sequence_number,residue.amino_acid,wt_r.sequence_number,wt_r.amino_acid)
                                        else:
                                            # print('WT pos not same AA, mismatch',residue.sequence_number,residue.amino_acid,wt_r.sequence_number,wt_r.amino_acid)
                                            wt_pdb_lookup.append(OrderedDict([('WT_POS',wt_r.sequence_number), ('PDB_POS',residue.sequence_number), ('AA','.')]))
                                            mismatch_seq += 1
                                            aa_mismatch += 1
                                    elif residue.sequence_number!=wt_r.sequence_number:
                                        # print('WT pos not same pos, mismatch',residue.sequence_number,residue.amino_acid,wt_r.sequence_number,wt_r.amino_acid)
                                        wt_pdb_lookup.append(OrderedDict([('WT_POS',wt_r.sequence_number), ('PDB_POS',residue.sequence_number), ('AA',wt_r.amino_acid)]))
                                        if structure.pdb_code.index not in ['4GBR','6C1R','6C1Q']:
                                            if residue.sequence_number in unmapped_ref:
                                                #print('residue.sequence_number',residue.sequence_number,'not mapped though')
                                                if residue.amino_acid == wt_lookup[residue.sequence_number].amino_acid:
                                                    #print('they are same amino acid!')
                                                    wt_r = wt_lookup[residue.sequence_number]
                                                    mismatch_seq -= 1
                                        mismatch_seq += 1
                                        ### REPLACE seq number with WT to fix odd PDB annotation. FIXME kinda dangerous, but best way to ensure consistent GN numbering
                                        ### 2019.01.18 DISABLED underneat, to be sure that sequence number can be found in DB correctly.
                                        # residue.sequence_number = wt_r.sequence_number

                                    if residue.amino_acid!=wt_r.amino_acid:
                                        if debug: print('aa mismatch',residue.sequence_number,residue.amino_acid,wt_r.sequence_number,wt_r.amino_acid)
                                        aa_mismatch += 1

                                    else:
                                        match_seq += 1
                                    if wt_r.generic_number is not None:
                                        residue.display_generic_number = wt_r.display_generic_number
                                        residue.generic_number = wt_r.generic_number
                                    else:
                                        residue.display_generic_number = None
                                        residue.generic_number = None
                                        # print('no GN')

                                    residue.protein_segment = wt_r.protein_segment
                                    residue.missing_gn = False
                                    # print(residue, residue.protein_segment, residue.display_generic_number, wt_r, wt_r.protein_segment, wt_r.display_generic_number)
                                    if len(seg_ends):
                                        if residue.protein_segment.slug=='TM1':
                                            if seg_ends['1b']!='-' and seg_ends['1e']!='-':
                                                if residue.sequence_number<seg_ends['1b']:
                                                    residue.protein_segment = self.segments['N-term']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                elif residue.sequence_number>seg_ends['1e']:
                                                    residue.protein_segment = self.segments['ICL1']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                        elif residue.protein_segment.slug=='ICL1':
                                            if seg_ends['i1b']!='-' and seg_ends['i1e']!='-':
                                                if residue.sequence_number<seg_ends['i1b'] and residue.sequence_number<=seg_ends['1e']:
                                                    residue.protein_segment = self.segments['TM1']
                                                # elif residue.sequence_number>seg_ends['i1e']:
                                                #     residue.protein_segment = self.segments['TM2']
                                                elif (residue.sequence_number>=seg_ends['i1b'] and residue.sequence_number<=seg_ends['i1e']) and residue.generic_number is None:
                                                    if debug: print("Missing GN in loop!",residue.sequence_number)
                                                    residue.missing_gn = True
                                                elif (residue.sequence_number<seg_ends['i1b'] and residue.sequence_number>seg_ends['i1e']):
                                                    if debug: print("Remove former GN for ",residue.generic_number)
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                            else:
                                                if residue.generic_number is not None:
                                                    if debug: print("Remove former GN for ",residue.generic_number)
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                if residue.sequence_number<=seg_ends['1e']:
                                                    residue.protein_segment = self.segments['TM1']
                                                elif residue.sequence_number>=seg_ends['2b']:
                                                    residue.protein_segment = self.segments['TM2']
                                        elif residue.protein_segment.slug=='TM2':
                                            if seg_ends['2b']!='-' and seg_ends['2e']!='-':
                                                if residue.sequence_number<seg_ends['2b']:
                                                    residue.protein_segment = self.segments['ICL1']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                elif residue.sequence_number>seg_ends['2e']:
                                                    residue.protein_segment = self.segments['ECL1']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                        elif residue.protein_segment.slug=='ECL1':
                                            if seg_ends['e1b']!='-' and seg_ends['e1e']!='-' and seg_ends['2e']!='-':
                                                if residue.sequence_number<seg_ends['e1b'] and residue.sequence_number<=seg_ends['2e']:
                                                    residue.protein_segment = self.segments['TM2']
                                                # elif residue.sequence_number>seg_ends['e1e']:
                                                #     residue.protein_segment = self.segments['TM3']
                                                elif (residue.sequence_number>=seg_ends['e1b'] and residue.sequence_number<=seg_ends['e1e']) and residue.generic_number is None:
                                                    if debug: print("Missing GN in loop!",residue.sequence_number)
                                                    residue.missing_gn = True
                                                elif (residue.sequence_number<seg_ends['e1b'] and residue.sequence_number>seg_ends['e1e']):
                                                    if debug: print("Remove former GN for ",residue.generic_number)
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                            else:
                                                if residue.generic_number is not None:
                                                    if debug: print("Remove former GN for ",residue.generic_number)
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                if residue.sequence_number<=seg_ends['2e']:
                                                    residue.protein_segment = self.segments['TM2']
                                                elif residue.sequence_number>=seg_ends['3b']:
                                                    residue.protein_segment = self.segments['TM3']
                                        elif residue.protein_segment.slug=='TM3':
                                            if seg_ends['3b']!='-' and seg_ends['3e']!='-':
                                                if residue.sequence_number<seg_ends['3b']:
                                                    residue.protein_segment = self.segments['ECL1']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                elif residue.sequence_number>seg_ends['3e']:
                                                    residue.protein_segment = self.segments['ICL2']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                        elif residue.protein_segment.slug=='ICL2':
                                            if seg_ends['i2b']!='-' and seg_ends['i2e']!='-':
                                                # if residue.sequence_number<seg_ends['i2b']:
                                                #     residue.protein_segment = self.segments['TM3']
                                                if residue.sequence_number>seg_ends['i2e'] and residue.sequence_number>=seg_ends['4b']:
                                                    residue.protein_segment = self.segments['TM4']
                                                elif (residue.sequence_number>=seg_ends['i2b'] and residue.sequence_number<=seg_ends['i2e']) and residue.generic_number is None:
                                                    if debug: print("Missing GN in loop!",residue.sequence_number)
                                                    residue.missing_gn = True
                                                elif (residue.sequence_number<seg_ends['i2b'] and residue.sequence_number>seg_ends['i2e']):
                                                    if debug: print("Remove former GN for ",residue.generic_number)
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                            else:
                                                if residue.generic_number is not None:
                                                    if debug: print("Remove former GN for ",residue.generic_number)
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                if residue.sequence_number<=seg_ends['3e']:
                                                    residue.protein_segment = self.segments['TM3']
                                                elif residue.sequence_number>=seg_ends['4b']:
                                                    residue.protein_segment = self.segments['TM4']
                                        elif residue.protein_segment.slug=='TM4':
                                            if seg_ends['4b']!='-' and seg_ends['4e']!='-':
                                                if residue.sequence_number<seg_ends['4b']:
                                                    residue.protein_segment = self.segments['ICL2']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                elif residue.sequence_number>seg_ends['4e']:
                                                    residue.protein_segment = self.segments['ECL2']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                        elif residue.protein_segment.slug=='ECL2':
                                            if seg_ends['e2b']!='-' and seg_ends['e2e']!='-' and seg_ends['4e']!='-':
                                                if residue.sequence_number<seg_ends['e2b'] and residue.sequence_number<=seg_ends['4e']:
                                                    residue.protein_segment = self.segments['TM4']
                                                elif residue.sequence_number>seg_ends['e2e'] and residue.sequence_number>=seg_ends['5b']:
                                                    residue.protein_segment = self.segments['TM5']
                                            else:
                                                if residue.sequence_number<=seg_ends['4e']:
                                                    residue.protein_segment = self.segments['TM4']
                                                elif residue.sequence_number>=seg_ends['5b']:
                                                    residue.protein_segment = self.segments['TM5']
                                        elif residue.protein_segment.slug=='TM5':
                                            if seg_ends['5b']!='-' and seg_ends['5e']!='-':
                                                if residue.sequence_number<seg_ends['5b']:
                                                    residue.protein_segment = self.segments['ECL2']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                elif residue.sequence_number>seg_ends['5e']:
                                                    residue.protein_segment = self.segments['ICL3']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                        elif residue.protein_segment.slug=='ICL3':
                                            if residue.sequence_number<=seg_ends['5e']:
                                                residue.protein_segment = self.segments['TM5']
                                            elif residue.sequence_number>=seg_ends['6b']:
                                                residue.protein_segment = self.segments['TM6']
                                        elif residue.protein_segment.slug=='TM6':
                                            if seg_ends['6b']!='-' and seg_ends['6e']!='-':
                                                if residue.sequence_number<seg_ends['6b']:
                                                    residue.protein_segment = self.segments['ICL3']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                elif residue.sequence_number>seg_ends['6e']:
                                                    residue.protein_segment = self.segments['ECL3']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                        elif residue.protein_segment.slug=='TM7':
                                            if seg_ends['7b']!='-' and seg_ends['7e']!='-':
                                                if residue.sequence_number<seg_ends['7b']:
                                                    residue.protein_segment = self.segments['ECL3']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                elif residue.sequence_number>seg_ends['7e']:
                                                    if seg_ends['8b']!='-':
                                                        if residue.sequence_number<seg_ends['8b']:
                                                            residue.protein_segment = self.segments['ICL4']
                                                            residue.display_generic_number = None
                                                            residue.generic_number = None
                                                    else:
                                                        residue.protein_segment = self.segments['C-term']
                                                        residue.display_generic_number = None
                                                        residue.generic_number = None
                                        elif residue.protein_segment.slug=='H8':
                                            if seg_ends['8b']!='-' and seg_ends['8e']!='-':
                                                if residue.sequence_number<seg_ends['8b']:
                                                    residue.protein_segment = self.segments['ICL4']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                                elif residue.sequence_number>seg_ends['8e']:
                                                    residue.protein_segment = self.segments['C-term']
                                                    residue.display_generic_number = None
                                                    residue.generic_number = None
                                            else:
                                                residue.protein_segment = self.segments['C-term']
                                                residue.display_generic_number = None
                                                residue.generic_number = None
                                        elif residue.protein_segment.slug=='C-term':
                                            if seg_ends['8e']!='-':
                                                if residue.sequence_number<=seg_ends['8e']:
                                                    residue.protein_segment = self.segments['H8']

                                        residue.sequence_number = int(check.strip())
                                        #HAVE TO RESET SO IT FITS FOR INTERACTION SCRIPT

                                        if residue.protein_segment != wt_r.protein_segment:
                                            residue.display_generic_number = None
                                            residue.generic_number = None
                                            generic_change += 1
                                            # print(residue.protein_segment.slug[0:2])
                                            if residue.protein_segment.slug[0:2]=="TM" or 1==1:
                                                if debug: print(structure.protein_conformation.protein.entry_name,residue.amino_acid,"XTAL POS",residue.sequence_number, "WT POS",wt_r.sequence_number,"XTAL:",residue.protein_segment,"WT:",wt_r.protein_segment)

                                                # FIX ME
                                                # self.logger.info('No SEG ENDS info for {}'.format(structure.protein_conformation.protein.entry_name,residue.amino_acid,residue.sequence_number, wt_r.sequence_number,residue.protein_segment,wt_r.protein_segment))
                                                pass
                                    if structure.pdb_code.index=='5LWE' and 209<=residue.sequence_number<=244:
                                        residue.display_generic_number = None
                                        residue.generic_number = None
                                        residue.protein_segment = None
                                    # print(residue.sequence_number, "(",int(check.strip()),")",residue.amino_acid,residue.protein_segment,wt_r.amino_acid,wt_r.sequence_number) #sanity check

                            else:
                                #print('wierd error') #sanity check
                                residue.display_generic_number = None
                                residue.generic_number = None
                                residue.protein_segment = None



                            # print('aa ',residue.sequence_number,residue.amino_acid,residue.display_generic_number, residue.protein_segment)
                            if residue.protein_segment==None:
                                pass
                            else:
                                #print('inserted',residue.sequence_number) #sanity check
                                # residue.save()
                                residues_bulk.append(residue)
                                rotamer_data, created = PdbData.objects.get_or_create(pdb=temp)
                                #rotamer_data_bulk.append(PdbData(pdb=temp))
                                missing_atoms = False
                                if rotamer_data.pdb.startswith('COMPND'):
                                    lines = len(rotamer_data.pdb.split('\n'))-2
                                else:
                                    lines = len(rotamer_data.pdb.split('\n'))
                                if lines<atom_num_dict[residue.amino_acid]:
                                    missing_atoms = True
                                rotamer_data_bulk.append([rotamer_data, missing_atoms])
                                # rotamer, created = Rotamer.objects.get_or_create(residue=residue, structure=structure, pdbdata=rotamer_data)
                                #rotamer_bulk.append(Rotamer(residue=residue, structure=structure, pdbdata=rotamer_data))

                        temp = "" #start new line for rotamer
                        check = pdblines[i+1][22:26].strip()

                    check = pdblines[i+1][22:26].strip()
                chain = line[21]
                residue_name = line[17:20].title() #use title to get GLY to Gly so it matches

        ns = settings.DEFAULT_NUMBERING_SCHEME
        ns_obj = ResidueNumberingScheme.objects.get(slug=ns)
        scheme = structure.protein_conformation.protein.residue_numbering_scheme
        segments_present = []
        for res in residues_bulk:
            if res.protein_segment:
                if res.protein_segment.slug not in segments_present:
                    segments_present.append(res.protein_segment.slug)
                if res.generic_number==None and (res.protein_segment.category == "helix" or res.missing_gn): # residue.missing_gn
                    if (res.protein_segment==prev_segment):
                        gn_split = prev_gn.split("x")
                        new_gn = gn_split[0]+"x"+str(int(gn_split[1])+1)

                        display_split=prev_display.split("x")
                        seq_split = display_split[0].split(".")

                        new_display = seq_split[0]+"."+str(int(seq_split[1])+1)+"x"+str(int(display_split[1])+1)
                        new_equivalent = seq_split[0]+"x"+str(int(display_split[1])+1)

                        if debug: print("Added Generic Number for",res.sequence_number,": GN",new_gn," Display",new_display)

                        gn, created = ResidueGenericNumber.objects.get_or_create(
                                scheme=ns_obj, label=new_gn, protein_segment=res.protein_segment)
                        display_gn, created = ResidueGenericNumber.objects.get_or_create(
                                scheme=scheme, label=new_display, protein_segment=res.protein_segment)

                        try:
                            gn_equivalent, created = ResidueGenericNumberEquivalent.objects.get_or_create(
                                default_generic_number=gn,
                                scheme=scheme,
                                defaults={'label': new_equivalent})
                        except IntegrityError:
                            gn_equivalent = ResidueGenericNumberEquivalent.objects.get(
                                default_generic_number=gn,
                                scheme=scheme)

                        res.generic_number = gn
                        res.display_generic_number = display_gn

                        prev_gn = new_gn
                        prev_display = new_display
                        prev_segment = res.protein_segment
                else:
                    if res.generic_number:
                        prev_gn = res.generic_number.label
                        prev_display = res.display_generic_number.label
                    else:
                        prev_gn = None
                        prev_display = None
                    prev_segment = res.protein_segment

        for res in reversed(residues_bulk):
            if res.protein_segment:
                if res.generic_number==None and (res.protein_segment.category == "helix" or res.missing_gn): # residue.missing_gn
                    if (res.protein_segment==prev_segment):
                        gn_split = prev_gn.split("x")
                        new_gn = gn_split[0]+"x"+str(int(gn_split[1])-1)

                        display_split=prev_display.split("x")
                        seq_split = display_split[0].split(".")

                        new_display = seq_split[0]+"."+str(int(seq_split[1])-1)+"x"+str(int(display_split[1])-1)
                        new_equivalent = seq_split[0]+"x"+str(int(display_split[1])-1)

                        if debug: print("Added Generic Number for",res.sequence_number,": GN",new_gn," Display",new_display)

                        gn, created = ResidueGenericNumber.objects.get_or_create(
                                scheme=ns_obj, label=new_gn, protein_segment=res.protein_segment)
                        display_gn, created = ResidueGenericNumber.objects.get_or_create(
                                scheme=scheme, label=new_display, protein_segment=res.protein_segment)

                        try:
                            gn_equivalent, created = ResidueGenericNumberEquivalent.objects.get_or_create(
                                default_generic_number=gn,
                                scheme=scheme,
                                defaults={'label': new_equivalent})
                        except IntegrityError:
                            gn_equivalent = ResidueGenericNumberEquivalent.objects.get(
                                default_generic_number=gn,
                                scheme=scheme)

                        res.generic_number = gn
                        res.display_generic_number = display_gn

                        prev_gn = new_gn
                        prev_display = new_display
                        prev_segment = res.protein_segment
                else:
                    if res.generic_number:
                        prev_gn = res.generic_number.label
                        prev_display = res.display_generic_number.label
                    else:
                        prev_gn = None
                        prev_display = None
                    prev_segment = res.protein_segment

        bulked_res = Residue.objects.bulk_create(residues_bulk)
        #bulked_rot = PdbData.objects.bulk_create(rotamer_data_bulk)
        bulked_rot = rotamer_data_bulk

        rotamer_bulk = []
        for i,res in enumerate(bulked_res):
            rotamer_bulk.append(Rotamer(residue=res, structure=structure, pdbdata=bulked_rot[i][0],
                                        missing_atoms=bulked_rot[i][1]))

        Rotamer.objects.bulk_create(rotamer_bulk)
        #
        # for i in bulked:
        #     print(i.pk)
        if debug: print("WT",structure.protein_conformation.protein.parent.entry_name,"length",len(parent_seq),structure.pdb_code.index,'length',len(seq),len(mapped_seq),'mapped res',str(mismatch_seq+match_seq+aa_mismatch),'pos mismatch',mismatch_seq,'aa mismatch',aa_mismatch,'not mapped',not_matched,' mapping off, matched on pos,aa',matched_by_pos,"generic_segment_changes",generic_change)
        if (len(segments_present)<8 and 'H8' in segments_present) or len(segments_present)<7:
            print("Present helices:",segments_present)
            print("MISSING HELICES?!")
        if debug: print("===============**================")

        if not os.path.exists(os.sep.join([settings.DATA_DIR, 'structure_data', 'wt_pdb_lookup'])):
            os.makedirs(os.sep.join([settings.DATA_DIR, 'structure_data', 'wt_pdb_lookup']))
        wt_pdb_lookup_folder = os.sep.join([settings.DATA_DIR, 'structure_data', 'wt_pdb_lookup', str(structure) + '.json'])
        if len(wt_pdb_lookup)>0:
            with open(wt_pdb_lookup_folder, 'w') as f2:
                json.dump(wt_pdb_lookup, f2)
        elif os.path.exists(os.sep.join([settings.DATA_DIR, 'structure_data', 'wt_pdb_lookup', str(structure) + '.json'])):
            os.remove(os.sep.join([settings.DATA_DIR, 'structure_data', 'wt_pdb_lookup', str(structure) + '.json']))
        return None


    def build_contact_network(self,s,pdb_code):
        try:
            interacting_pairs, distances  = compute_interactions(pdb_code, save_to_db=True)
        except:
            self.logger.error('Error with computing interactions (%s)' % (pdb_code))
            return


    def main_func(self, positions, iteration,count,lock):
        # setting up processes
        # if not positions[1]:
        #     pdbs = self.parsed_structures[positions[0]:]
        # else:
        #     pdbs = self.parsed_structures[positions[0]:positions[1]]
        pdbs = self.parsed_structures.pdb_ids
        while count.value<len(pdbs):
            with lock:
                pdb_id = pdbs[count.value]
                count.value +=1

            sd = self.parsed_structures.structures[pdb_id]
            # is this a representative structure (will be used to guide structure-based alignments)?
            representative = False
            if sd['name'].upper() in self.xtal_representatives:#sd['representative']:
                representative = True

            # only process representative structures on first iteration
            # if not representative and iteration == 1:
            #     continue

            # skip representative structures on second iteration
            # if representative and iteration == 2:
            #     continue

            # is there a construct?
            # if 'construct' not in sd:
            #     self.logger.error('No construct specified, skipping!')
            #     continue

            self.logger.info('Building structure {}'.format(sd['name']))

            # does the construct exist?
            try:
                con = Protein.objects.get(entry_name=sd['name'].lower())
            except Protein.DoesNotExist:
                print('BIG ERROR Construct {} does not exists, skipping!'.format(sd['name'].lower()))
                self.logger.error('Construct {} does not exists, skipping!'.format(sd['name'].lower()))
                continue

            # create a structure record
            try:
                s = Structure.objects.get(protein_conformation__protein=con)

                # If update_flag is true then update existing structures
                # Otherwise only make new structures
                if not self.incremental_mode:
                    s = s.delete()
                    s = Structure()
                else:
                    continue

            except Structure.DoesNotExist:
                s = Structure()

            s.representative = representative

            # protein state
            if 'state' not in sd:
                self.logger.warning('State not defined, using default state {}'.format(
                    settings.DEFAULT_PROTEIN_STATE))
                state = settings.DEFAULT_STATE.title()
            else:
                state = sd['state']
            state_slug = slugify(state)
            try:
                ps, created = ProteinState.objects.get_or_create(slug=state_slug, defaults={'name': state})
                if created:
                    self.logger.info('Created protein state {}'.format(ps.name))
            except IntegrityError:
                ps = ProteinState.objects.get(slug=state_slug)
            s.state = ps
            s.author_state = ps

            # # xtal activation value aka Delta Distance (Å)
            # if 'distance' not in sd:
            #     self.logger.warning('Delta distance not defined, using default value {}'.format(None))
            #     distance = None
            # else:
            #     distance = sd['distance']
            # s.distance = distance

            # protein conformation
            try:
                s.protein_conformation = ProteinConformation.objects.get(protein=con)
            except ProteinConformation.DoesNotExist:
                self.logger.error('Protein conformation for construct {} does not exists'.format(con))
                continue
            if s.protein_conformation.state is not state:
                ProteinConformation.objects.filter(protein=con).update(state=ps)

            # get the PDB file and save to DB
            sd['pdb'] = sd['name'].upper()
            if not os.path.exists(self.pdb_data_dir):
                os.makedirs(self.pdb_data_dir)

            pdb_path = os.sep.join([self.pdb_data_dir, sd['pdb'] + '.pdb'])
            if not os.path.isfile(pdb_path):
                self.logger.info('Fetching PDB file {}'.format(sd['pdb']))
                url = 'http://www.rcsb.org/pdb/files/%s.pdb' % sd['pdb']
                pdbdata_raw = urlopen(url).read().decode('utf-8')
                with open(pdb_path, 'w') as f:
                    f.write(pdbdata_raw)
            else:
                with open(pdb_path, 'r') as pdb_file:
                    pdbdata_raw = pdb_file.read()

            pdbdata, created = PdbData.objects.get_or_create(pdb=pdbdata_raw)
            s.pdb_data = pdbdata

            # UPDATE HETSYN with its PDB reference instead + GRAB PUB DATE, PMID, DOI AND RESOLUTION
            hetsyn = {}
            hetsyn_reverse = {}
            for line in pdbdata_raw.splitlines():
                if line.startswith('HETSYN'):
                    m = re.match("HETSYN[\s]+([\w]{3})[\s]+(.+)",line) ### need to fix bad PDB formatting where col4 and col5 are put together for some reason -- usually seen when the id is +1000
                    if (m):
                        hetsyn[m.group(2).strip()] = m.group(1).upper()
                        hetsyn_reverse[m.group(1)] = m.group(2).strip().upper()
                if line.startswith('HETNAM'):
                    m = re.match("HETNAM[\s]+([\w]{3})[\s]+(.+)",line) ### need to fix bad PDB formatting where col4 and col5 are put together for some reason -- usually seen when the id is +1000
                    if (m):
                        hetsyn[m.group(2).strip()] = m.group(1).upper()
                        hetsyn_reverse[m.group(1)] = m.group(2).strip().upper()
                if line.startswith('REVDAT   1'):
                    sd['publication_date'] = line[13:22]
                if line.startswith('JRNL        PMID'):
                    sd['pubmed_id'] = line[19:].strip()
                if line.startswith('JRNL        DOI'):
                    sd['doi_id'] = line[19:].strip()

            if len(hetsyn) == 0:
                self.logger.info("PDB file contained NO hetsyn")

            with open(pdb_path,'r') as header:
                header_dict = parse_pdb_header(header)
            sd['publication_date'] = header_dict['release_date']
            if str(header_dict['resolution']).strip()!='None':
                sd['resolution'] = str(header_dict['resolution']).strip()
            sd['structure_method'] = header_dict['structure_method']

            # structure type
            if 'structure_method' in sd and sd['structure_method']:
                structure_type = sd['structure_method'].capitalize()
                structure_type_slug = slugify(sd['structure_method'])
                if sd['pdb']=='6ORV':
                    structure_type_slug = 'electron-microscopy'
                elif sd['pdb'] in ['6YVR','6Z4Q','6Z4S','6Z4V','6Z66','6Z8N','6ZA8','6ZIN','7B6W']:
                    structure_type_slug = 'x-ray-diffraction'

                try:
                    st, created = StructureType.objects.get_or_create(slug=structure_type_slug,
                        defaults={'name': structure_type})
                    if created:
                        self.logger.info('Created structure type {}'.format(st))
                except IntegrityError:
                    st = StructureType.objects.get(slug=structure_type_slug)
                s.structure_type = st
            else:
                self.logger.warning('No structure type specified in PDB file {}'.format(sd['pdb']))

            matched = 0
            if 'ligand' in sd and sd['ligand']:
                if isinstance(sd['ligand'], list):
                    ligands = sd['ligand']
                else:
                    ligands = [sd['ligand']]
                for ligand in ligands:
                    if 'name' in ligand:
                        if ligand['name'].upper() in hetsyn:
                            self.logger.info('Ligand {} matched to PDB records'.format(ligand['name']))
                            matched = 1
                            ligand['name'] = hetsyn[ligand['name'].upper()]
                        elif ligand['name'].upper() in hetsyn_reverse:
                            matched = 1

            if matched==0 and len(hetsyn)>0:
                self.logger.info('No ligand names found in HET in structure {}'.format(sd['pdb']))

            # REMOVE? can be used to dump structure files with updated ligands
            # yaml.dump(sd, open(source_file_path, 'w'), indent=4)

            # pdb code
            if 'pdb' in sd:
                web_resource = WebResource.objects.get(slug='pdb')
                s.pdb_code, created = WebLink.objects.get_or_create(index=sd['pdb'], web_resource=web_resource)
            else:
                self.logger.error('PDB code not specified for structure {}, skipping!'.format(sd['pdb']))
                continue

            # insert into plain text fields
            if 'preferred_chain' in sd:
                s.preferred_chain = sd['preferred_chain']
            else:
                self.logger.warning('Preferred chain not specified for structure {}'.format(sd['pdb']))
            if 'resolution' in sd:
                s.resolution = float(sd['resolution'])
            else:
                self.logger.warning('Resolution not specified for structure {}'.format(sd['pdb']))
            if sd['pdb']=='6ORV':
                sd['publication_date'] = '2020-01-08'
            elif sd['pdb'] in ['6YVR','6Z4Q','6Z4S','6Z4V','6Z66','6Z8N','6ZA8','6ZIN']:
                sd['publication_date'] = '2021-02-10'
            elif sd['pdb']=='7B6W':
                sd['publication_date'] = '2022-01-12'
            if 'publication_date' in sd:
                s.publication_date = sd['publication_date']
            else:
                self.logger.warning('Publication date not specified for structure {}'.format(sd['pdb']))

            # publication
            try:
                if 'doi_id' in sd:
                    s.publication = Publication.get_or_create_from_doi(sd['doi_id'])
                elif 'pubmed_id' in sd:
                    s.publication = Publication.get_or_create_from_pubmed(sd['pubmed_id'])
            except:
                self.logger.error('Error saving publication'.format(sd['pdb']))

            if sd['pdb'] in self.xtal_seg_ends and not self.incremental_mode:
                s.annotated = True
            else:
                s.annotated = False

            s.refined = False
            s.stats_text = None

            # save structure before adding M2M relations
            s.save()
            # StructureLigandInteraction.objects.filter(structure=s).delete()

            # ligands
            peptide_chain = ""
            if 'ligand' in sd and sd['ligand'] and sd['ligand']!='None':
                if isinstance(sd['ligand'], list):
                    ligands = sd['ligand']
                else:
                    ligands = [sd['ligand']]
                for ligand in ligands:
                    l = False
                    peptide_chain = ""
                    if 'chain'!='':
                        peptide_chain = ligand['chain']
                        # ligand['name'] = 'pep'
                    if ligand['name'] and ligand['name'] != 'None': # some inserted as none.
                        ligand['type'] = ligand['type'].lower()
                        # use annoted ligand type or default type
                        if ligand['type']:
                            lt, created = LigandType.objects.get_or_create(slug=slugify(ligand['type']),
                                defaults={'name': ligand['type']})
                        else:
                            lt, created = LigandType.objects.get_or_create(
                                slug=slugify(default_ligand_type), defaults={'name': default_ligand_type})
                        # set pdb reference for structure-ligand interaction
                        # if ligand['type'] in ['peptide','protein']:
                        #     pdb_reference = 'pep'
                        #     db_lig = Ligand.objects.filter(name=ligand['title'])
                        # else:
                        #     pdb_reference = ligand['name']
                        #     if ligand['name']=='RET':
                        #         db_lig = Ligand.objects.filter(name=ligand['title'])
                        #     else:
                        #         db_lig = Ligand.objects.filter(pdbe=ligand['name'])
                        #
                        # # check if ligand exists already
                        # if len(db_lig)>0:
                        #     l = db_lig[0]
                        # else:
                        # use ligand title, if specified
                        if 'title' in ligand and ligand['title']:
                            ligand_title = ligand['title']
                        else:
                            ligand_title = ligand['name']

                        # Adding the PDB three-letter code
                        ids = {}
                        pdb_reference = ligand['name']
                        if ligand['name'] != "pep" and ligand['name'] != "apo":
                            ids["pdb"] = ligand['name']

                        # use pubchem_id
                        if 'pubchemId' in ligand and ligand['pubchemId'] and ligand['pubchemId'] != 'None':
                            # TODO expand list and expand structure annotation
                            if isinstance(ligand['pubchemId'], str) and ligand['pubchemId'].startswith("gtoplig:"):
                                ids["gtoplig"] = ligand['pubchemId'][8:]
                            else:
                                ids["pubchem"] = ligand['pubchemId']
                        elif "pdb" in ids:
                            # match PDB via UniChem
                            uc_entries = match_id_via_unichem("pdb", ids["pdb"])
                            #print("UNICHEM matching", ids["pdb"])
                            for entry in uc_entries:
                                if entry["type"] not in ids:
                                    ids[entry["type"]] = entry["id"]

                        with lock:
                            l = get_or_create_ligand(ligand_title, ids, ligand['type'])

                        # Create LigandPeptideStructure object to store chain ID for peptide ligands - supposed to b TEMP
                        if ligand['type'] in ['peptide','protein']:
                            lps, created = LigandPeptideStructure.objects.get_or_create(structure=s, ligand=l, chain=peptide_chain)
                    else:
                        continue

                    # structure-ligand interaction
                    if l and ligand['role']:
                        role_slug = slugify(ligand['role'])
                        try:
                            lr, created = LigandRole.objects.get_or_create(slug=role_slug,
                            defaults={'name': ligand['role']})
                            if created:
                                self.logger.info('Created ligand role {}'.format(ligand['role']))
                        except IntegrityError:
                            lr = LigandRole.objects.get(slug=role_slug)

                        i, created = StructureLigandInteraction.objects.get_or_create(structure=s,
                            ligand=l, ligand_role=lr, annotated=True,
                            defaults={'pdb_reference': pdb_reference})
                        if i.pdb_reference != pdb_reference:
                            i.pdb_reference = pdb_reference
                            i.save()

            # protein anomalies
            anomaly_entry = self.xtal_anomalies[s.protein_conformation.protein.parent.entry_name]
            segment_codes = {'1':'TM1','12':'ICL1','2':'TM2','23':'ECL1','3':'TM3','34':'ICL2','4':'TM4','5':'TM5','6':'TM6','7':'TM7'}
            all_bulges, all_constrictions = OrderedDict(), OrderedDict()
            for key, val in anomaly_entry.items():
                if 'x' not in val:
                    continue
                if key[0] not in segment_codes:
                    continue
                segment = segment_codes[key.split('x')[0]]
                if len(key.split('x')[1])==3:
                    try:
                        all_bulges[segment] = all_bulges[segment]+[val]
                    except:
                        all_bulges[segment] = [val]
                else:
                    try:
                        all_constrictions[segment] = all_constrictions[segment]+[val]
                    except:
                        all_constrictions[segment] = [val]

            scheme = s.protein_conformation.protein.residue_numbering_scheme
            if len(all_bulges)>0:
                pa_slug = 'bulge'
                try:
                    pab, created = ProteinAnomalyType.objects.get_or_create(slug=pa_slug, defaults={
                        'name': 'Bulge'})
                    if created:
                        self.logger.info('Created protein anomaly type {}'.format(pab))
                except IntegrityError:
                    pab = ProteinAnomalyType.objects.get(slug=pa_slug)

                for segment, bulges in all_bulges.items():
                    for bulge in bulges:
                        try:
                            gn, created = ResidueGenericNumber.objects.get_or_create(label=bulge,
                                scheme=scheme, defaults={'protein_segment': ProteinSegment.objects.get(
                                slug=segment)})
                            if created:
                                self.logger.info('Created generic number {}'.format(gn))
                        except IntegrityError:
                            gn =  ResidueGenericNumber.objects.get(label=bulge, scheme=scheme)

                        try:
                            pa, created = ProteinAnomaly.objects.get_or_create(anomaly_type=pab,
                                generic_number=gn)
                            if created:
                                self.logger.info('Created protein anomaly {}'.format(pa))
                        except IntegrityError:
                            pa, created = ProteinAnomaly.objects.get(anomaly_type=pab, generic_number=gn)

                        s.protein_anomalies.add(pa)
            if len(all_constrictions)>0:
                pa_slug = 'constriction'
                try:
                    pac, created = ProteinAnomalyType.objects.get_or_create(slug=pa_slug, defaults={
                        'name': 'Constriction'})
                    if created:
                        self.logger.info('Created protein anomaly type {}'.format(pac))
                except IntegrityError:
                    pac = ProteinAnomalyType.objects.get(slug=pa_slug)

                for segment, constrictions in all_constrictions.items():
                    for constriction in constrictions:
                        try:
                            gn, created = ResidueGenericNumber.objects.get_or_create(label=constriction,
                                scheme=scheme, defaults={'protein_segment': ProteinSegment.objects.get(
                                slug=segment)})
                            if created:
                                self.logger.info('Created generic number {}'.format(gn))
                        except IntegrityError:
                            gn =  ResidueGenericNumber.objects.get(label=constriction, scheme=scheme)

                        try:
                            pa, created = ProteinAnomaly.objects.get_or_create(anomaly_type=pac,
                                generic_number=gn)
                            if created:
                                self.logger.info('Created protein anomaly {}'.format(pa))
                        except IntegrityError:
                            pa, created = ProteinAnomaly.objects.get(anomaly_type=pac, generic_number=gn)

                        s.protein_anomalies.add(pa)

            # stabilizing agents, FIXME - redesign this!
            # fusion proteins moved to constructs, use this for G-proteins and other agents?
            aux_proteins = []
            # if 'signaling_protein' in sd and sd['signaling_protein'] and sd['signaling_protein'] != 'None':
            #     aux_proteins.append('signaling_protein')
            if 'auxiliary_protein' in sd and sd['auxiliary_protein'] and sd['auxiliary_protein'] != 'None':
                aux_proteins.append('auxiliary_protein')
            for index in aux_proteins:
                if isinstance(sd[index], list):
                    aps = sd[index]
                else:
                    aps = [sd[index]]
                for aux_protein in aps:
                    aux_protein_slug = slugify(aux_protein)[:50]
                    try:
                        sa, created = StructureStabilizingAgent.objects.get_or_create(
                            slug=aux_protein_slug, defaults={'name': aux_protein})
                    except IntegrityError:
                        sa = StructureStabilizingAgent.objects.get(slug=aux_protein_slug)
                    s.stabilizing_agents.add(sa)

            # save structure
            s.save()

            #Delete previous interaction data to prevent errors.
            ResidueFragmentInteraction.objects.filter(structure_ligand_pair__structure=s).delete()
            #Remove previous Rotamers/Residues to prepare repopulate
            Fragment.objects.filter(structure=s).delete()
            Rotamer.objects.filter(structure=s).delete()
            Residue.objects.filter(protein_conformation=s.protein_conformation).delete()

            d = {}

            try:
                current = time.time()
                #protein = Protein.objects.filter(entry_name=s.protein_conformation).get()
                d = fetch_pdb_info(sd['pdb'],con)
                #delete before adding new
                #Construct.objects.filter(name=d['construct_crystal']['pdb_name']).delete()
                # add_construct(d)
                end = time.time()
                diff = round(end - current,1)
                self.logger.info('construction calculations done for {}. {} seconds.'.format(
                            s.protein_conformation.protein.entry_name, diff))
            except Exception as msg:
                print(msg)
                print('ERROR WITH CONSTRUCT FETCH {}'.format(sd['pdb']))
                self.logger.error('ERROR WITH CONSTRUCT FETCH for {}'.format(sd['pdb']))

            try:
                current = time.time()
                self.create_rotamers(s,pdb_path,d)
                # residue_errors = sbc.check_rotamers(s.pdb_code.index)
                # if len(residue_errors)>0:
                #     raise Exception('Error with rotamer check: {}'.format(residue_errors))
                end = time.time()
                diff = round(end - current,1)
                self.logger.info('Create resides/rotamers done for {}. {} seconds.'.format(
                            s.protein_conformation.protein.entry_name, diff))
            except Exception as msg:
                print(msg)
                print('ERROR WITH ROTAMERS {}'.format(sd['pdb']))
                self.logger.error('Error with rotamers for {}'.format(sd['pdb']))

            try:
                s.protein_conformation.generate_sites()
            except:
                pass

            if self.run_contactnetwork:
                try:
                    current = time.time()
                    self.build_contact_network(s,sd['pdb'])
                    end = time.time()
                    diff = round(end - current,1)
                    self.logger.info('Create contactnetwork done for {}. {} seconds.'.format(
                                s.protein_conformation.protein.entry_name, diff))
                except Exception as msg:
                    print(msg)
                    print('ERROR WITH CONTACTNETWORK {}'.format(sd['pdb']))
                    self.logger.error('Error with contactnetwork for {}'.format(sd['pdb']))

                try:
                    current = time.time()
                    mypath = '/tmp/interactions/results/' + sd['pdb'] + '/output'
                    # if not os.path.isdir(mypath):
                    #     #Only run calcs, if not already in temp
                    runcalculation(sd['pdb'],peptide_chain)

                    parsecalculation(sd['pdb'],False)
                    end = time.time()
                    diff = round(end - current,1)
                    self.logger.info('Interaction calculations done for {}. {} seconds.'.format(
                                s.protein_conformation.protein.entry_name, diff))
                except Exception as msg:
                    try:
                        current = time.time()
                        mypath = '/tmp/interactions/results/' + sd['pdb'] + '/output'
                        # if not os.path.isdir(mypath):
                        #     #Only run calcs, if not already in temp
                        runcalculation(sd['pdb'], peptide_chain)

                        parsecalculation(sd['pdb'],False)
                        end = time.time()
                        diff = round(end - current,1)
                        self.logger.info('Interaction calculations done (again) for {}. {} seconds.'.format(
                                    s.protein_conformation.protein.entry_name, diff))
                    except Exception as msg:

                        print(msg)
                        print('ERROR WITH INTERACTIONS {}'.format(sd['pdb']))
                        self.logger.error('Error parsing interactions output for {}'.format(sd['pdb']))




                    # print('{} done'.format(sd['pdb']))
