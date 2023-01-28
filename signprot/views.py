#mccabe complexity: ["error", 31]
from django.contrib.postgres.aggregates import ArrayAgg
from django.core.cache import cache
from django.db.models import F, Q, Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView

from common import definitions
from common.diagrams_gpcr import DrawSnakePlot
from common.diagrams_gprotein import DrawGproteinPlot
from common.phylogenetic_tree import PhylogeneticTreeGenerator
from common.views import AbsTargetSelection
from contactnetwork.models import InteractingResiduePair
from mutation.models import MutationExperiment
from protein.models import (Gene, Protein, ProteinAlias, ProteinConformation, ProteinFamily,
                            ProteinCouplings, ProteinSegment)

from residue.models import (Residue, ResidueGenericNumberEquivalent, ResiduePositionSet)
from seqsign.sequence_signature import (SequenceSignature, SignatureMatch)
from signprot.interactions import (get_entry_names, get_generic_numbers, get_ignore_info, get_protein_segments,
                                   get_signature_features, group_signature_features, prepare_signature_match)
from signprot.models import (SignprotBarcode, SignprotComplex, SignprotStructure)
from structure.models import Structure

import json
import time

from collections import Counter, OrderedDict
from copy import deepcopy
from statistics import mean


class BrowseSelection(AbsTargetSelection):
    step = 1
    number_of_steps = 1
    psets = False
    filters = True
    filter_gprotein = True

    type_of_selection = 'browse_gprot'

    description = 'Select a G protein (family) by searching or browsing in the middle. The selection is viewed to' \
                  + ' the right.'
    docs = 'receptors.html'
    target_input = False

    selection_boxes = OrderedDict([
        ('reference', False), ('targets', True),
        ('segments', False),
    ])
    try:
        ppf_g = ProteinFamily.objects.get(slug="100_001")
        # ppf_a = ProteinFamily.objects.get(slug="200_000")
        # pfs = ProteinFamily.objects.filter(parent__in=[ppf_g.id,ppf_a.id])
        pfs = ProteinFamily.objects.filter(parent__in=[ppf_g.id])
        ps = Protein.objects.filter(family__in=[ppf_g])  # ,ppf_a
        tree_indent_level = []
        # action = 'expand'
        # remove the parent family (for all other families than the root of the tree, the parent should be shown)
        # del ppf_g
        # del ppf_a
    except Exception as e:
        pass


class ArrestinSelection(AbsTargetSelection):
    step = 1
    number_of_steps = 1
    psets = False
    filters = True
    filter_gprotein = True

    type_of_selection = 'browse_gprot'

    description = 'Select an Arrestin (family) by searching or browsing in the middle. The selection is viewed to' \
                  + ' the right.'
    docs = 'signalproteins.html'
    target_input = False

    selection_boxes = OrderedDict([
        ('reference', False), ('targets', True),
        ('segments', False),
    ])
    try:
        if ProteinFamily.objects.filter(slug="200_000").exists():
            ppf = ProteinFamily.objects.get(slug="200_000")
            pfs = ProteinFamily.objects.filter(parent=ppf.id)
            ps = Protein.objects.filter(family=ppf)

            tree_indent_level = []
            action = 'expand'
            # remove the parent family (for all other families than the root of the tree, the parent should be shown)
            del ppf
    except Exception as e:
        pass


class TargetSelection(AbsTargetSelection):
    step = 1
    number_of_steps = 1
    filters = False
    psets = False
    target_input = False
    redirect_on_select = True
    type_of_selection = 'ginterface'
    title = 'SELECT TARGET for Gs INTERFACE'
    description = 'Select a reference target by searching or browsing.' \
                  + '\n\nThe Gs interface from adrb2 (PDB: 3SN6) will be superposed onto the selected target.' \
                  + '\n\nAn interaction browser for the adrb2 Gs interface will be given for comparison"'

    # template_name = 'common/targetselection.html'

    selection_boxes = OrderedDict([
        ('reference', False),
        ('targets', True),
        ('segments', False),
    ])

    buttons = {
        'continue': {
            'label': 'Continue to next step',
            'url': '#',
            'color': 'success',
        },
    }


class CouplingBrowser(TemplateView):
    """
    Class based generic view which serves coupling data between Receptors and G-proteins.
    Data coming from Guide to Pharmacology, Asuka Inuoue and Michel Bouvier at the moment.
    More data might come later from Roth and Strachan TRUPATH biosensor and Neville Lambert.
    :param dataset: ProteinCouplings (see build/management/commands/build_g_proteins.py)
    :return: context
    """

    page = "gprot"
    subunit_filter = "100_001"
    families = ["Gs", "Gi/o", "Gq/11", "G12/13"]
    template_name = "signprot/coupling_browser.html"

    @method_decorator(csrf_exempt)
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        tab_fields, header = self.tab_fields(self.subunit_filter, self.families)

        context['tabfields'] = tab_fields
        context['header'] = header
        flat_list = [item for sublist in header.values() for item in sublist]
        context['subunitheader'] = flat_list
        context['page'] = self.page

        return context

    @staticmethod
    def tab_fields(subunit_filter, families):
        """
        This function returns the required fields for the G-protein families table and the G-protein subtypes table
        which are to be rendered in separate tabs in the same page.

        :return: key.value pairs from dictotemplate dictionary
        keys =id values in ProteinCouplings table.
        values = source, class, family, variant, uniprotid, iupharid, logmaxec50, pec50, emax, stand_dev
        """

        coupling_receptors = list(ProteinCouplings.objects.filter(g_protein__slug__startswith=subunit_filter).values_list("protein__entry_name", flat = True).distinct())

        proteins = Protein.objects.filter(entry_name__in=coupling_receptors, sequence_type__slug='wt',
                                          family__slug__startswith='00').prefetch_related(
                                          'family', 'family__parent__parent__parent', 'web_links')

        couplings = ProteinCouplings.objects.filter(source="GuideToPharma").values_list('protein__entry_name',
                                                                                           'g_protein__name',
                                                                                           'transduction')

        signaling_data = {}
        for pairing in couplings:
            if pairing[0] not in signaling_data:
                signaling_data[pairing[0]] = {}
            signaling_data[pairing[0]][pairing[1]] = pairing[2]

        protein_data = {}
        for prot in proteins:
            protein_data[prot.id] = {}
            protein_data[prot.id]['class'] = prot.family.parent.parent.parent.shorter()
            protein_data[prot.id]['family'] = prot.family.parent.short()
            protein_data[prot.id]['uniprot'] = prot.entry_short()
            protein_data[prot.id]['iuphar'] = prot.family.name.replace('receptor', '').strip()
            protein_data[prot.id]['accession'] = prot.accession
            protein_data[prot.id]['entryname'] = prot.entry_name

            # Add link to GtP
            gtop_links = prot.web_links.filter(web_resource__slug='gtop')
            if len(gtop_links) > 0:
                protein_data[prot.id]['gtp_link'] = gtop_links[0]

            #VARIABLE (arrestins/gprots)
            # gprotein_families = ["Gs", "Gi/o", "Gq/11", "G12/13"]
            for gprotein in families:
                gprotein_clean = slugify(gprotein)
                if prot.entry_name in signaling_data and gprotein in signaling_data[prot.entry_name]:
                    if signaling_data[prot.entry_name][gprotein] == "primary":
                        protein_data[prot.id][gprotein_clean] = "1'"
                    elif signaling_data[prot.entry_name][gprotein] == "secondary":
                        protein_data[prot.id][gprotein_clean] = "2'"
                    else:
                        protein_data[prot.id][gprotein_clean] = "-"
                else:
                    protein_data[prot.id][gprotein_clean] = "-"

        #VARIABLE
        couplings2 = ProteinCouplings.objects.exclude(source="GuideToPharma") \
            .filter(g_protein_subunit__family__slug__startswith=subunit_filter) \
            .order_by("g_protein_subunit__family__slug", "source", "-variant") \
            .prefetch_related('g_protein_subunit__family', 'g_protein')

        #VARIABLE
        coupling_headers = ProteinCouplings.objects.exclude(source="GuideToPharma") \
            .filter(g_protein_subunit__family__slug__startswith=subunit_filter) \
            .order_by("g_protein_subunit__family__slug", "source", "-variant") \
            .values_list("g_protein_subunit__family__name", "g_protein_subunit__family__parent__name", "variant").distinct()

        coupling_header_names = {}
        coupling_reverse_header_names = {}
        coupling_placeholder = {}
        coupling_placeholder2 = {}
        coupling_placeholder3 = {}
        for name in coupling_headers:
            if name[2] == "Regular":
                subname = name[0]
            else:
                subname = name[0] + '<br><span class="couplingvariant">' + name[2] + '</span>'
            if name[1] not in coupling_header_names:
                coupling_header_names[name[1]] = []
                coupling_placeholder3[name[1]] = []
            coupling_reverse_header_names[subname] = name[1]
            if subname not in coupling_header_names[name[1]]:
                coupling_header_names[name[1]].append(subname)
            coupling_placeholder[subname] = "-"
            coupling_placeholder2[subname] = []

        # First create and populate the dictionary for all receptors
        dictotemplate = {}
        sourcenames = set()
        readouts = ["logemaxec50", "pec50", "emax", "std"]
        for protein in proteins:
            dictotemplate[protein.pk] = {}
            dictotemplate[protein.pk]['protein'] = protein_data[protein.pk]
            for listing in ["coupling", "couplingmax"]:
                dictotemplate[protein.pk][listing] = {}
                dictotemplate[protein.pk][listing]['1'] = {}
                for copy_arg in readouts:
                    if listing == "coupling":
                        dictotemplate[protein.pk][listing]['1'][copy_arg] = deepcopy(coupling_placeholder2)
                    else:
                        dictotemplate[protein.pk][listing]['1'][copy_arg] = deepcopy(coupling_placeholder3)
                for empty_arg in ["ligand_id", "ligand_name", "ligand_physiological"]:
                    dictotemplate[protein.pk][listing]['1'][empty_arg] = "-"

        for pair in couplings2:
            if pair.source not in dictotemplate[pair.protein_id]['coupling']:
                ## check the physiological property
                physio = 'Physiological'
                if not pair.physiological_ligand:
                    physio = 'Surrogate'

                sourcenames.add(pair.source)
                dictotemplate[pair.protein_id]['coupling'][pair.source] = {}
                dictotemplate[pair.protein_id]['couplingmax'][pair.source] = {}
                for copy_arg in readouts:
                    dictotemplate[pair.protein_id]['coupling'][pair.source][copy_arg] = coupling_placeholder.copy()
                    dictotemplate[pair.protein_id]['couplingmax'][pair.source][copy_arg] = deepcopy(coupling_placeholder3)
                for listing in ["coupling", "couplingmax"]:
                    dictotemplate[pair.protein_id][listing][pair.source]['ligand_id'] = pair.ligand_id
                    dictotemplate[pair.protein_id][listing][pair.source]['ligand_name'] = pair.ligand.name
                    dictotemplate[pair.protein_id][listing][pair.source]['ligand_physiological'] = physio

            ## check the subunit and family
            if pair.variant == "Regular":
                subunit = pair.g_protein_subunit.family.name
            else:
                subunit = pair.g_protein_subunit.family.name + '<br><span class="couplingvariant">' + pair.variant + '</span>'
            family = coupling_reverse_header_names[subunit]

            # Combine values
            exp_values = {
                "logemaxec50": round(pair.logmaxec50, 1),
                "pec50": round(pair.pec50, 1),
                "emax": round(pair.emax),
                "std": 0}
            if pair.stand_dev != None:
                exp_values["std"] = round(pair.stand_dev, 1)

            for readout in readouts:
                dictotemplate[pair.protein_id]['coupling'][pair.source][readout][subunit] = exp_values[readout]
                dictotemplate[pair.protein_id]['coupling']['1'][readout][subunit].append(exp_values[readout])
                dictotemplate[pair.protein_id]['couplingmax'][pair.source][readout][family].append(exp_values[readout])
                dictotemplate[pair.protein_id]['couplingmax']['1'][readout][family].append(exp_values[readout])

        # Calculate mean values for all subunits for the GPCRdb rows (support 1)
        for prot in dictotemplate:
            for propval in dictotemplate[prot]['coupling']['1']:
                if propval not in ['ligand_id', 'ligand_name', 'ligand_physiological']:
                    for sub in dictotemplate[prot]['coupling']['1'][propval]:
                        valuelist = dictotemplate[prot]['coupling']['1'][propval][sub]
                        fixedlist = [i for i in valuelist if i != 0]

                        if len(valuelist) == 0:
                            dictotemplate[prot]['coupling']['1'][propval][sub] = "-"
                        else:
                            if len(fixedlist) > 0:
                                dictotemplate[prot]['coupling']['1'][propval][sub] = round(mean(fixedlist), 1)
                            else:
                                dictotemplate[prot]['coupling']['1'][propval][sub] = 0.0
                            if propval == "emax":
                                dictotemplate[prot]['coupling']['1'][propval][sub] = round(dictotemplate[prot]['coupling']['1'][propval][sub])


        # Calculate GPCRdb values for the different support levels for the subunits
        dict_name = 'coupling'
        for prot in dictotemplate:
            if dict_name not in dictotemplate[prot]:
                dictotemplate[prot][dict_name] = {}

            for i in range(2, len(sourcenames)+2): #
                dictotemplate[prot][dict_name][str(i)] = {}
            for propval in dictotemplate[prot]['coupling']['1']:
                for i in range(2, len(sourcenames)+2):
                    dictotemplate[prot][dict_name][str(i)][propval] = {}
                if propval not in ['ligand_id', 'ligand_name', 'ligand_physiological']:
                    for sub in dictotemplate[prot]['coupling']['1'][propval]: # use family here instead of sub for families "loop"
                            family = coupling_reverse_header_names[sub]
                            gtp = protein_data[prot][slugify(family)]

                            baseconfidence = dictotemplate[prot]['coupling']['1'][propval][sub]
                            confidence = 0
                            if gtp != "-":
                                confidence += 1
                                if baseconfidence == "-":
                                    baseconfidence = gtp
                                    dictotemplate[prot]['coupling']['1'][propval][sub] = gtp

                            for source in sourcenames:
                                if source in dictotemplate[prot]['coupling'] and dictotemplate[prot]['coupling'][source][propval][sub] != "-":
                                    if dictotemplate[prot]['coupling'][source][propval][sub] > 0:
                                        confidence += 1

                            for i in range(2, len(sourcenames)+2):
                                if confidence >= i:
                                    dictotemplate[prot][dict_name][str(i)][propval][sub] = baseconfidence
                                else:
                                    dictotemplate[prot][dict_name][str(i)][propval][sub] = gtp
                else:
                    for i in range(2, len(sourcenames)+2):
                        dictotemplate[prot][dict_name][str(i)][propval] = dictotemplate[prot]['coupling']['1'][propval]

        # Calculate mean values for all families for the individual sources
        for prot in dictotemplate:
            for source in sourcenames:
                if source in dictotemplate[prot]['couplingmax']:
                    for propval in dictotemplate[prot]['couplingmax'][source]:
                        if propval not in ['ligand_id', 'ligand_name', 'ligand_physiological']:
                            for fam in dictotemplate[prot]['couplingmax'][source][propval]:
                                valuelist = dictotemplate[prot]['couplingmax'][source][propval][fam]
                                fixedlist = [i for i in valuelist if i != 0]
                                if len(fixedlist) == 0:
                                    dictotemplate[prot]['couplingmax'][source][propval][fam] = "-"
                                else:
                                    if propval == "emax":
                                        dictotemplate[prot]['couplingmax'][source][propval][fam] = round(mean(fixedlist))
                                    else:
                                        dictotemplate[prot]['couplingmax'][source][propval][fam] = round(mean(fixedlist), 1)

        # Calculate mean values for all families for the GPCRdb rows (support 1)
        for prot in dictotemplate:
            source = '1'
            for propval in dictotemplate[prot]['couplingmax'][source]:
                if propval not in ['ligand_id', 'ligand_name', 'ligand_physiological']:
                    for fam in dictotemplate[prot]['couplingmax'][source][propval]:
                        # Calculate the mean of the individual source family means
                        valuelist = []
                        for orig_source in sourcenames:
                            if orig_source in dictotemplate[prot]['couplingmax'] and dictotemplate[prot]['couplingmax'][orig_source][propval][fam] != "-":
                                valuelist.append(dictotemplate[prot]['couplingmax'][orig_source][propval][fam])

                        fixedlist = [i for i in valuelist if i != 0]
                        if len(fixedlist) == 0:
                            dictotemplate[prot]['couplingmax'][source][propval][fam] = "-"
                        else:
                            if propval == "emax":
                                dictotemplate[prot]['couplingmax'][source][propval][fam] = round(mean(fixedlist))
                            else:
                                dictotemplate[prot]['couplingmax'][source][propval][fam] = round(mean(fixedlist), 1)

        # Calculate GPCRdb values for the different support levels for all families
        dict_name = 'couplingmax'
        for prot in dictotemplate:
            if dict_name not in dictotemplate[prot]:
                dictotemplate[prot][dict_name] = {}

            for i in range(2, len(sourcenames)+2):
                dictotemplate[prot][dict_name][str(i)] = {}
            for propval in dictotemplate[prot]['couplingmax']['1']:
                for i in range(2, len(sourcenames)+2):
                    dictotemplate[prot][dict_name][str(i)][propval] = {}
                if propval not in ['ligand_id', 'ligand_name', 'ligand_physiological']:
                    for family in dictotemplate[prot]['couplingmax']['1'][propval]:
                            gtp = protein_data[prot][slugify(family)]

                            baseconfidence = dictotemplate[prot]['couplingmax']['1'][propval][family]
                            confidence = 0
                            if gtp != "-":
                                confidence += 1
                                if baseconfidence == "-":
                                    baseconfidence = gtp
                            for source in sourcenames:
                                if source in dictotemplate[prot]['couplingmax'] and dictotemplate[prot]['couplingmax'][source][propval][family] != "-":
                                    if dictotemplate[prot]['couplingmax'][source][propval][family] > 0:
                                        confidence += 1

                            for i in range(2, len(sourcenames)+2):
                                if confidence >= i:
                                    dictotemplate[prot][dict_name][str(i)][propval][family] = baseconfidence
                                else:
                                    dictotemplate[prot][dict_name][str(i)][propval][family] = gtp
                else:
                    for i in range(2, len(sourcenames)+2):
                        dictotemplate[prot][dict_name][str(i)][propval] = dictotemplate[prot]['couplingmax']['1'][propval]

        return dictotemplate, coupling_header_names


def CouplingProfiles(request, render_part="both", signalling_data="empty"):
    name_of_cache = 'coupling_profiles_' + signalling_data

    context = cache.get(name_of_cache)
    # NOTE cache disabled for development only!
    # context = None
    if context == None:

        context = OrderedDict()
        # adding info for tree from StructureStatistics View
        tree = PhylogeneticTreeGenerator()
        class_a_data = tree.get_tree_data(ProteinFamily.objects.get(name='Class A (Rhodopsin)'))
        context['tree_class_a_options'] = deepcopy(tree.d3_options)
        context['tree_class_a_options']['anchor'] = 'tree_class_a'
        context['tree_class_a_options']['leaf_offset'] = 50
        context['tree_class_a_options']['label_free'] = []
        whole_class_a = class_a_data.get_nodes_dict(None)
        # section to remove Orphan from Class A tree and apply to a different tree
        for item in whole_class_a['children']:
            if item['name'] == 'Orphan':
                orphan_data = OrderedDict([('name', ''), ('value', 3000), ('color', ''), ('children',[item])])
                whole_class_a['children'].remove(item)
                break
        context['tree_class_a'] = json.dumps(whole_class_a)
        class_b1_data = tree.get_tree_data(ProteinFamily.objects.get(name__startswith='Class B1 (Secretin)'))
        context['tree_class_b1_options'] = deepcopy(tree.d3_options)
        context['tree_class_b1_options']['anchor'] = 'tree_class_b1'
        context['tree_class_b1_options']['branch_trunc'] = 60
        context['tree_class_b1_options']['label_free'] = [1,]
        context['tree_class_b1'] = json.dumps(class_b1_data.get_nodes_dict(None))
        class_b2_data = tree.get_tree_data(ProteinFamily.objects.get(name__startswith='Class B2 (Adhesion)'))
        context['tree_class_b2_options'] = deepcopy(tree.d3_options)
        context['tree_class_b2_options']['anchor'] = 'tree_class_b2'
        context['tree_class_b2_options']['label_free'] = [1,]
        context['tree_class_b2'] = json.dumps(class_b2_data.get_nodes_dict(None))
        class_c_data = tree.get_tree_data(ProteinFamily.objects.get(name__startswith='Class C (Glutamate)'))
        context['tree_class_c_options'] = deepcopy(tree.d3_options)
        context['tree_class_c_options']['anchor'] = 'tree_class_c'
        context['tree_class_c_options']['branch_trunc'] = 50
        context['tree_class_c_options']['label_free'] = [1,]
        context['tree_class_c'] = json.dumps(class_c_data.get_nodes_dict(None))
        class_f_data = tree.get_tree_data(ProteinFamily.objects.get(name__startswith='Class F (Frizzled)'))
        context['tree_class_f_options'] = deepcopy(tree.d3_options)
        context['tree_class_f_options']['anchor'] = 'tree_class_f'
        context['tree_class_f_options']['label_free'] = [1,]
        context['tree_class_f'] = json.dumps(class_f_data.get_nodes_dict(None))
        class_t2_data = tree.get_tree_data(ProteinFamily.objects.get(name='Class T (Taste 2)'))
        context['tree_class_t2_options'] = deepcopy(tree.d3_options)
        context['tree_class_t2_options']['anchor'] = 'tree_class_t2'
        context['tree_class_t2_options']['label_free'] = [1,]
        context['tree_class_t2'] = json.dumps(class_t2_data.get_nodes_dict(None))
        # definition of the class a orphan tree
        context['tree_orphan_options'] = deepcopy(tree.d3_options)
        context['tree_orphan_options']['anchor'] = 'tree_orphan'
        context['tree_orphan_options']['label_free'] = [1,]
        context['tree_orphan_a'] = json.dumps(orphan_data)
        # end copied section from StructureStatistics View
        # gprot_id = ProteinGProteinPair.objects.all().values_list('g_protein_id', flat=True).order_by('g_protein_id').distinct()
        coupling_gproteins = list(ProteinCouplings.objects.filter(g_protein__slug__startswith="100").values_list("g_protein_id", flat = True).distinct())
        gproteins = ProteinFamily.objects.filter(id__in=coupling_gproteins).exclude(name__startswith="GPa1")
        arrestins = ProteinCouplings.objects.filter(g_protein__slug__startswith="200").values_list('g_protein_subunit', flat=True).order_by('g_protein_subunit').distinct()
        arrestin_prots = list(Protein.objects.filter(family__slug__startswith="200", species__id=1, sequence_type__slug='wt').values_list("pk","name"))
        arrestin_translate = {}
        for arr in arrestin_prots:
            arrestin_translate[arr[0]] = arr[1]

        slug_translate = {'001': "ClassA", '002': "ClassB1", '003': "ClassB2", '004': "ClassC", '006': "ClassF", '007': "ClassT"}
        key_translate ={'Gs':"G<sub>s</sub>", 'Gi/o':"G<sub>i/o</sub>",
                        'Gq/11':"G<sub>q/11</sub>", 'G12/13':"G<sub>12/13</sub>",
                        'Beta-arrestin-1':"&beta;-Arrestin<sub>1</sub>", 'Beta-arrestin-2':"&beta;-Arrestin<sub>2</sub>"}
        selectivitydata_gtp_plus = {}
        receptor_dictionary = []
        if signalling_data == "gprot":
            table = {'Class':[], 'Gs': [], 'Gio': [], 'Gq11': [], 'G1213': [], 'Total': []}
        else: #here there may be the need of a elif if more signalling proteins will be added
            table = {'Class':[], 'Betaarrestin1': [], 'Betaarrestin2': [], 'Total': []}
        for slug in slug_translate.keys():
            tot = 0
            txttot = ''
            fam = str(ProteinFamily.objects.get(slug=(slug)))
            table['Class'].append(fam.replace('Class',''))
            jsondata_gtp_plus = {}
            if signalling_data == "gprot":
                for gp in gproteins:
                    # Collect GTP
                    gtp_couplings = list(ProteinCouplings.objects.filter(protein__family__slug__startswith=slug, source="GuideToPharma", g_protein=gp)\
                                    .order_by("protein__entry_name")\
                                    .values_list("protein__entry_name", flat=True)\
                                    .distinct())
                    # Other coupling data with logmaxec50 greater than 0
                    other_couplings = list(ProteinCouplings.objects.filter(protein__family__slug__startswith=slug)\
                                    .exclude(source="GuideToPharma")
                                    .filter(g_protein=gp, logmaxec50__gt=0)\
                                    .order_by("protein__entry_name")\
                                    .values_list("protein__entry_name").distinct()\
                                    .annotate(num_sources=Count("source", distinct=True)))

                    # Initialize selectivity array
                    processed_receptors = []
                    key = str(gp).split(' ')[0]
                    jsondata_gtp_plus[key] = []
                    for coupling in other_couplings:
                        receptor_name = coupling[0]
                        receptor_dictionary.append(receptor_name)
                        receptor_only = receptor_name.split('_')[0].upper()
                        count = coupling[1] + (1 if receptor_name in gtp_couplings else 0)

                        # Data from at least two sources:
                        if count >= 2:
                            # Add to selectivity data (for tree)
                            if receptor_only not in selectivitydata_gtp_plus:
                                selectivitydata_gtp_plus[receptor_only] = []

                            if key not in selectivitydata_gtp_plus[receptor_only]:
                                selectivitydata_gtp_plus[receptor_only].append(key)

                            # Add to json data for Venn diagram
                            jsondata_gtp_plus[key].append(str(receptor_name) + '\n')
                            processed_receptors.append(receptor_name)

                    unique_gtp_plus = set(gtp_couplings) - set(processed_receptors)
                    for receptor_name in unique_gtp_plus:
                        receptor_dictionary.append(receptor_name)
                        receptor_only = receptor_name.split('_')[0].upper()
                        if receptor_only not in selectivitydata_gtp_plus:
                            selectivitydata_gtp_plus[receptor_only] = []

                        if key not in selectivitydata_gtp_plus[receptor_only]:
                            selectivitydata_gtp_plus[receptor_only].append(key)

                        jsondata_gtp_plus[key].append(str(receptor_name) + '\n')

                    tot += len(jsondata_gtp_plus[key])
                    txttot = ' '.join([txttot,' '.join(jsondata_gtp_plus[key]).replace('\n','')])

                    if len(jsondata_gtp_plus[key]) == 0:
                        jsondata_gtp_plus.pop(key, None)
                        table[key.replace('/','')].append((0,''))
                    else:
                        table[key.replace('/','')].append((len(jsondata_gtp_plus[key]), ' '.join(jsondata_gtp_plus[key]).replace('\n','')))
                        jsondata_gtp_plus[key] = ''.join(jsondata_gtp_plus[key])

                tot = len(list(set(txttot.split(' ')))) -1
                table['Total'].append((tot,txttot))
            else: #here may need and elif if other signalling proteins will be added
                for arr in arrestins:
                    # arrestins?
                    arrestin_couplings = list(ProteinCouplings.objects.filter(protein__family__slug__startswith=slug, g_protein_subunit=arr)\
                                    .filter(logmaxec50__gt=0)\
                                    .order_by("protein__entry_name")\
                                    .values_list("protein__entry_name", flat=True)\
                                    .distinct())

                    key = arrestin_translate[arr]
                    jsondata_gtp_plus[key] = []
                    for coupling in arrestin_couplings:
                        receptor_name = coupling
                        receptor_dictionary.append(receptor_name)
                        receptor_only = receptor_name.split('_')[0].upper()
                        if receptor_only not in selectivitydata_gtp_plus:
                            selectivitydata_gtp_plus[receptor_only] = []

                        if key not in selectivitydata_gtp_plus[receptor_only]:
                            selectivitydata_gtp_plus[receptor_only].append(key)

                        # Add to json data for Venn diagram
                        jsondata_gtp_plus[key].append(str(receptor_name) + '\n')

                    tot += len(jsondata_gtp_plus[key])
                    txttot = ' '.join([txttot,' '.join(jsondata_gtp_plus[key]).replace('\n','')])

                    if len(jsondata_gtp_plus[key]) == 0:
                        jsondata_gtp_plus.pop(key, None)
                        table[key.replace('-','')].append((0,''))
                    else:
                        table[key.replace('-','')].append((len(jsondata_gtp_plus[key]), ' '.join(jsondata_gtp_plus[key]).replace('\n','')))
                        jsondata_gtp_plus[key] = ''.join(jsondata_gtp_plus[key])

                tot = len(list(set(txttot.split(' ')))) -1
                table['Total'].append((tot,txttot))

            for item in key_translate:
                try:
                    jsondata_gtp_plus[key_translate[item]] = jsondata_gtp_plus.pop(item)
                except KeyError:
                    continue

            context[slug_translate[slug]+"_gtp_plus"] = jsondata_gtp_plus
            context[slug_translate[slug]+"_gtp_plus_keys"] = list(jsondata_gtp_plus.keys())

        for key in list(table.keys())[1:]:
            table[key].append((sum([pair[0] for pair in table[key]]),' '.join([pair[1] for pair in table[key]])+' '))
        # context["selectivitydata"] = selectivitydata
        context["selectivitydata_gtp_plus"] = selectivitydata_gtp_plus
        context["table"] = table

        # Collect receptor information
        receptor_panel = Protein.objects.filter(entry_name__in=receptor_dictionary)\
                                .prefetch_related("family", "family__parent__parent__parent")

        receptor_dictionary = {}
        for p in receptor_panel:
            # Collect receptor data
            rec_class = p.family.parent.parent.parent.short().split(' ')[0]
            rec_ligandtype = p.family.parent.parent.short()
            rec_family = p.family.parent.short()
            rec_uniprot = p.entry_short()
            rec_iuphar = p.family.name.replace("receptor", '').replace("<i>","").replace("</i>","").strip()
            receptor_dictionary[rec_uniprot] = [rec_class, rec_ligandtype, rec_family, rec_uniprot, rec_iuphar]

        whole_receptors = Protein.objects.prefetch_related("family", "family__parent__parent__parent").filter(sequence_type__slug="wt", family__slug__startswith="00")
        whole_rec_dict = {}
        for rec in whole_receptors:
            rec_uniprot = rec.entry_short()
            rec_iuphar = rec.family.name.replace("receptor", '').replace("<i>","").replace("</i>","").strip()
            whole_rec_dict[rec_uniprot] = [rec_iuphar]

        context["whole_receptors"] = json.dumps(whole_rec_dict)
        context["receptor_dictionary"] = json.dumps(receptor_dictionary)

    cache.set(name_of_cache, context, 60 * 60 * 24 * 7)  # seven days timeout on cache
    context["render_part"] = render_part
    context["signalling_data"] = signalling_data

    return render(request,
                  'signprot/coupling_profiles.html',
                  context
    )

def GProteinTree(request):
    return CouplingProfiles(request, "tree", "gprot")

def GProteinVenn(request):
    return CouplingProfiles(request, "venn", "gprot")

def ArrestinTree(request):
    return CouplingProfiles(request, "tree", "arrestin")

def ArrestinVenn(request):
    return CouplingProfiles(request, "venn", "arrestin")

#@cache_page(60*60*24*7)
def familyDetail(request, slug):
    # get family
    pf = ProteinFamily.objects.get(slug=slug)
    # get family list
    ppf = pf
    families = [ppf.name]
    while ppf.parent.parent:
        families.append(ppf.parent.name)
        ppf = ppf.parent
    families.reverse()

    # number of proteins
    proteins = Protein.objects.filter(family__slug__startswith=pf.slug, sequence_type__slug='wt')
    no_of_proteins = proteins.count()
    no_of_human_proteins = Protein.objects.filter(family__slug__startswith=pf.slug, species__id=1,
                                                  sequence_type__slug='wt').count()

    # get structures of this family
    structures = SignprotStructure.objects.filter(protein__family__slug__startswith=slug)
    complex_structures = SignprotComplex.objects.filter(protein__family__slug__startswith=slug)

    mutations = MutationExperiment.objects.filter(protein__in=proteins).prefetch_related('residue__generic_number',
                                                                                         'exp_qual', 'ligand')

    mutations_list = {}
    for mutation in mutations:
        if not mutation.residue.generic_number: continue  # cant map those without display numbers
        if mutation.residue.generic_number.label not in mutations_list: mutations_list[
            mutation.residue.generic_number.label] = []
        if mutation.ligand:
            ligand = mutation.ligand.name
        else:
            ligand = ''
        if mutation.exp_qual:
            qual = mutation.exp_qual.qual
        else:
            qual = ''
        mutations_list[mutation.residue.generic_number.label].append(
            [mutation.foldchange, ligand.replace("'", "\\'"), qual])

    interaction_list = {}  ###FIXME - always empty
    try:
        pc = ProteinConformation.objects.get(protein__family__slug=slug, protein__sequence_type__slug='consensus')
    except ProteinConformation.DoesNotExist:
        try:
            pc = ProteinConformation.objects.get(protein__family__slug=slug, protein__species_id=1,
                                                 protein__sequence_type__slug='wt')
        except:
            try:
                pc = ProteinConformation.objects.filter(protein__family__slug=slug, protein__sequence_type__slug='wt').first()
            except:
                return HttpResponse("No consensus was generated for this protein family")
    
    p = pc.protein
    residues = Residue.objects.filter(protein_conformation=pc).order_by('sequence_number').prefetch_related(
        'protein_segment', 'generic_number', 'display_generic_number')

    jsondata = {}
    jsondata_interaction = {}
    for r in residues:
        if r.generic_number:
            if r.generic_number.label in mutations_list:
                jsondata[r.sequence_number] = [mutations_list[r.generic_number.label]]
            if r.generic_number.label in interaction_list:
                jsondata_interaction[r.sequence_number] = interaction_list[r.generic_number.label]

    # process residues and return them in chunks of 10
    # this is done for easier scaling on smaller screens
    chunk_size = 10
    r_chunks = []
    r_buffer = []
    last_segment = False
    border = False
    title_cell_skip = 0
    for i, r in enumerate(residues):
        # title of segment to be written out for the first residue in each segment
        segment_title = False

        # keep track of last residues segment (for marking borders)
        if r.protein_segment.slug != last_segment:
            last_segment = r.protein_segment.slug
            border = True

        # if on a border, is there room to write out the title? If not, write title in next chunk
        if i == 0 or (border and len(last_segment) <= (chunk_size - i % chunk_size)):
            segment_title = True
            border = False
            title_cell_skip += len(last_segment)  # skip cells following title (which has colspan > 1)

        if i and i % chunk_size == 0:
            r_chunks.append(r_buffer)
            r_buffer = []

        r_buffer.append((r, segment_title, title_cell_skip))

        # update cell skip counter
        if title_cell_skip > 0:
            title_cell_skip -= 1
    if r_buffer:
        r_chunks.append(r_buffer)

    context = {'pf': pf, 'families': families, 'structures': structures, 'no_of_proteins': no_of_proteins,
               'no_of_human_proteins': no_of_human_proteins, 'mutations': mutations, 'r_chunks': r_chunks,
               'chunk_size': chunk_size, 'p': p, 'complex_structures': complex_structures}

    return render(request,
                  'signprot/family_details.html',
                  context
                  )

@cache_page(60 * 60 * 24 * 7)
def Ginterface(request, protein=None):
    residuelist = Residue.objects.filter(protein_conformation__protein__entry_name=protein).prefetch_related(
        'protein_segment', 'display_generic_number', 'generic_number')
    SnakePlot = DrawSnakePlot(
        residuelist, "Class A (Rhodopsin)", protein, nobuttons=1)

    # TEST
    gprotein_residues = Residue.objects.filter(protein_conformation__protein__entry_name='gnaz_human').prefetch_related(
        'protein_segment', 'display_generic_number', 'generic_number')
    gproteinplot = DrawGproteinPlot(
        gprotein_residues, "Gprotein", protein)

    crystal = Structure.objects.get(pdb_code__index="3SN6")
    aa_names = definitions.AMINO_ACID_GROUP_NAMES_OLD
    names_aa = dict(zip(aa_names.values(), aa_names.keys()))
    names_aa['Polar (S/T)'] = 'pol_short'
    names_aa['Polar (N/Q/H)'] = 'pol_long'

    residues_browser = [
        {'pos': 135, 'aa': 'I', 'gprotseg': "H5", 'segment': 'TM3', 'ligand': 'Gs', 'type': aa_names['hp'],
         'gpcrdb': '3.54x54', 'gpnum': 'G.H5.16', 'gpaa': 'Q384', 'availability': 'interacting'},
        {'pos': 136, 'aa': 'T', 'gprotseg': "H5", 'segment': 'TM3', 'ligand': 'Gs', 'type': 'Polar (S/T)',
         'gpcrdb': '3.55x55', 'gpnum': 'G.H5.12', 'gpaa': 'R380', 'availability': 'interacting'},
        {'pos': 139, 'aa': 'F', 'gprotseg': "H5", 'segment': 'ICL2', 'ligand': 'Gs', 'type': 'Aromatic',
         'gpcrdb': '34.51x51', 'gpnum': 'G.H5.8', 'gpaa': 'F376', 'availability': 'interacting'},
        {'pos': 139, 'aa': 'F', 'gprotseg': "S1", 'segment': 'ICL2', 'ligand': 'Gs', 'type': 'Aromatic',
         'gpcrdb': '34.51x51', 'gpnum': 'G.S1.2', 'gpaa': 'H41', 'availability': 'interacting'},
        {'pos': 141, 'aa': 'Y', 'gprotseg': "H5", 'segment': 'ICL2', 'ligand': 'Gs', 'type': 'Aromatic',
         'gpcrdb': '34.53x53', 'gpnum': 'G.H5.19', 'gpaa': 'H387', 'availability': 'interacting'},
        {'pos': 225, 'aa': 'E', 'gprotseg': "H5", 'segment': 'TM5', 'ligand': 'Gs', 'type': 'Negative charge',
         'gpcrdb': '5.64x64', 'gpnum': 'G.H5.12', 'gpaa': 'R380', 'availability': 'interacting'},
        {'pos': 225, 'aa': 'E', 'gprotseg': "H5", 'segment': 'TM5', 'ligand': 'Gs', 'type': 'Negative charge',
         'gpcrdb': '5.64x64', 'gpnum': 'G.H5.16', 'gpaa': 'Q384', 'availability': 'interacting'},
        {'pos': 229, 'aa': 'Q', 'gprotseg': "H5", 'segment': 'TM5', 'ligand': 'Gs', 'type': 'Polar (N/Q/H)',
         'gpcrdb': '5.68x68', 'gpnum': 'G.H5.13', 'gpaa': 'D381', 'availability': 'interacting'},
        {'pos': 229, 'aa': 'Q', 'gprotseg': "H5", 'segment': 'TM5', 'ligand': 'Gs', 'type': 'Polar (N/Q/H)',
         'gpcrdb': '5.68x68', 'gpnum': 'G.H5.16', 'gpaa': 'Q384', 'availability': 'interacting'},
        {'pos': 229, 'aa': 'Q', 'gprotseg': "H5", 'segment': 'TM5', 'ligand': 'Gs', 'type': 'Polar (N/Q/H)',
         'gpcrdb': '5.68x68', 'gpnum': 'G.H5.17', 'gpaa': 'R385', 'availability': 'interacting'},
        {'pos': 274, 'aa': 'T', 'gprotseg': "H5", 'segment': 'TM6', 'ligand': 'Gs', 'type': 'Polar (S/T)',
         'gpcrdb': '6.36x36', 'gpnum': 'G.H5.24', 'gpaa': 'E392', 'availability': 'interacting'},
        {'pos': 328, 'aa': 'R', 'gprotseg': "H5", 'segment': 'TM7', 'ligand': 'Gs', 'type': 'Positive charge',
         'gpcrdb': '7.55x55', 'gpnum': 'G.H5.24', 'gpaa': 'E392', 'availability': 'interacting'},
        {'pos': 232, 'aa': 'K', 'segment': 'TM5', 'ligand': 'Gs', 'type': 'Positive charge', 'gpcrdb': '5.71x71',
         'gprotseg': "H5", 'gpnum': 'G.H5.13', 'gpaa': 'D381', 'availability': 'interacting'}]

    # accessible_gn = ['3.50x50', '3.53x53', '3.54x54', '3.55x55', '34.50x50', '34.51x51', '34.53x53', '34.54x54', '5.61x61', '5.64x64', '5.65x65', '5.67x67', '5.68x68', '5.71x71', '5.72x72', '5.74x74', '5.75x75', '6.29x29', '6.32x32', '6.33x33', '6.36x36', '6.37x37', '7.55x55', '8.48x48', '8.49x49']

    accessible_gn = ['3.50x50', '3.53x53', '3.54x54', '3.55x55', '3.56x56', '34.50x50', '34.51x51', '34.52x52',
                     '34.53x53', '34.54x54', '34.55x55', '34.56x56', '34.57x57', '5.61x61', '5.64x64', '5.65x65',
                     '5.66x66', '5.67x67', '5.68x68', '5.69x69', '5.71x71', '5.72x72', '5.74x74', '5.75x75', '6.25x25',
                     '6.26x26', '6.28x28', '6.29x29', '6.32x32', '6.33x33', '6.36x36', '6.37x37', '6.40x40', '7.55x55',
                     '7.56x56', '8.47x47', '8.48x48', '8.49x49', '8.51x51']

    exchange_table = OrderedDict([('hp', ('V', 'I', 'L', 'M')),
                                  ('ar', ('F', 'H', 'W', 'Y')),
                                  ('pol_short', ('S', 'T')),  # Short/hydroxy
                                  ('pol_long', ('N', 'Q', 'H')),  # Amino-like (both donor and acceptor
                                  ('neg', ('D', 'E')),
                                  ('pos', ('K', 'R'))])

    interacting_gn = []

    accessible_pos = list(
        residuelist.filter(display_generic_number__label__in=accessible_gn).values_list('sequence_number', flat=True))

    # Which of the Gs interacting_pos are conserved?
    GS_none_equivalent_interacting_pos = []
    GS_none_equivalent_interacting_gn = []

    for interaction in residues_browser:
        interacting_gn.append(interaction['gpcrdb'])
        gs_b2_interaction_type_long = (
            next((item['type'] for item in residues_browser if item['gpcrdb'] == interaction['gpcrdb']), None))

        interacting_aa = residuelist.filter(display_generic_number__label__in=[interaction['gpcrdb']]).values_list(
            'amino_acid', flat=True)

        if interacting_aa:
            interaction['aa'] = interacting_aa[0]
            pos = \
                residuelist.filter(display_generic_number__label__in=[interaction['gpcrdb']]).values_list(
                    'sequence_number',
                    flat=True)[0]
            interaction['pos'] = pos

            feature = names_aa[gs_b2_interaction_type_long]

            if interacting_aa[0] not in exchange_table[feature]:
                GS_none_equivalent_interacting_pos.append(pos)
                GS_none_equivalent_interacting_gn.append(interaction['gpcrdb'])

    GS_equivalent_interacting_pos = list(
        residuelist.filter(display_generic_number__label__in=interacting_gn).values_list('sequence_number', flat=True))

    gProteinData = ProteinCouplings.objects.filter(protein__entry_name=protein)

    primary = []
    secondary = []

    for entry in gProteinData:
        if entry.transduction == 'primary':
            primary.append((entry.g_protein.name.replace("Gs", "G<sub>s</sub>").replace("Gi", "G<sub>i</sub>").replace(
                "Go", "G<sub>o</sub>").replace("G11", "G<sub>11</sub>").replace("G12", "G<sub>12</sub>").replace("G13",
                                                                                                                 "G<sub>13</sub>").replace(
                "Gq", "G<sub>q</sub>").replace("G", "G&alpha;"), entry.g_protein.slug))
        elif entry.transduction == 'secondary':
            secondary.append((
                entry.g_protein.name.replace("Gs", "G<sub>s</sub>").replace("Gi", "G<sub>i</sub>").replace(
                    "Go", "G<sub>o</sub>").replace("G11", "G<sub>11</sub>").replace("G12",
                                                                                    "G<sub>12</sub>").replace(
                    "G13", "G<sub>13</sub>").replace("Gq", "G<sub>q</sub>").replace("G", "G&alpha;"),
                entry.g_protein.slug))

    return render(request,
                  'signprot/ginterface.html',
                  {'pdbname': '3SN6',
                   'snakeplot': SnakePlot,
                   'gproteinplot': gproteinplot,
                   'crystal': crystal,
                   'interacting_equivalent': GS_equivalent_interacting_pos,
                   'interacting_none_equivalent': GS_none_equivalent_interacting_pos,
                   'accessible': accessible_pos,
                   'residues': residues_browser,
                   'mapped_protein': protein,
                   'interacting_gn': GS_none_equivalent_interacting_gn,
                   'primary_Gprotein': set(primary),
                   'secondary_Gprotein': set(secondary)}
                  )

def ajaxInterface(request, slug, **response_kwargs):
    name_of_cache = 'ajaxInterface_' + slug

    jsondata = cache.get(name_of_cache)

    if jsondata == None:

        p = Protein.objects.filter(entry_name=slug).get()

        if p.family.slug.startswith('200'):
            rsets = ResiduePositionSet.objects.get(name="Arrestin interface")
        else:
            rsets = ResiduePositionSet.objects.get(name="Gprotein Barcode")

        jsondata = {}
        for x, residue in enumerate(rsets.residue_position.all()):
            try:
                pos = str(list(Residue.objects.filter(protein_conformation__protein__entry_name=slug,
                                                      display_generic_number__label=residue.label))[0])
            except:
                print("Protein has no residue position at", residue.label)
            a = pos[1:]

            jsondata[a] = [5, 'Receptor interface position', residue.label]

        jsondata = json.dumps(jsondata)

    cache.set(name_of_cache, jsondata, 60 * 60 * 24 * 2)  # two days timeout on cache

    response_kwargs['content_type'] = 'application/json'

    return HttpResponse(jsondata, **response_kwargs)

def ajaxBarcode(request, slug, cutoff, **response_kwargs):
    name_of_cache = 'ajaxBarcode_' + slug + cutoff

    jsondata = cache.get(name_of_cache)

    if jsondata == None:
        jsondata = {}

        selectivity_pos = list(
            SignprotBarcode.objects.filter(protein__entry_name=slug, seq_identity__gte=cutoff).values_list(
                'residue__display_generic_number__label', flat=True))

        conserved = list(SignprotBarcode.objects.filter(protein__entry_name=slug, paralog_score__gte=cutoff,
                                                        seq_identity__gte=cutoff).prefetch_related(
            'residue__display_generic_number').values_list('residue__display_generic_number__label', flat=True))

        na_data = list(
            SignprotBarcode.objects.filter(protein__entry_name=slug, seq_identity=0, paralog_score=0).values_list(
                'residue__display_generic_number__label', flat=True))

        all_positions = Residue.objects.filter(protein_conformation__protein__entry_name=slug).prefetch_related(
            'display_generic_number')

        for res in all_positions:
            cgn = str(res.generic_number)
            res = str(res.sequence_number)
            if cgn in conserved:
                jsondata[res] = [0, 'Conserved', cgn]
            elif cgn in selectivity_pos and cgn not in conserved:
                jsondata[res] = [1, 'Selectivity determining', cgn]
            elif cgn in na_data:
                jsondata[res] = [3, 'NA', cgn]
            else:
                jsondata[res] = [2, 'Evolutionary neutral', cgn]

        jsondata = json.dumps(jsondata)
        response_kwargs['content_type'] = 'application/json'

        cache.set(name_of_cache, jsondata, 60 * 60 * 24 * 2)  # two days timeout on cache

    return HttpResponse(jsondata, **response_kwargs)

@cache_page(60 * 60 * 24 * 7)
def StructureInfo(request, pdbname):
    """
    Show structure details
    """

    protein = Protein.objects.filter(signprotstructure__pdb_code__index=pdbname).first()
    crystal = SignprotStructure.objects.filter(pdb_code__index=pdbname).first()

    return render(request,
                  'signprot/structure_info.html',
                  {'pdbname': pdbname,
                   'protein': protein,
                   'crystal': crystal}
                  )

# @cache_page(60*60*24*2)
def signprotdetail(request, slug):
    # get protein

    slug = slug.lower()
    p = Protein.objects.prefetch_related('web_links__web_resource').get(entry_name=slug, sequence_type__slug='wt')

    # Redirect to protein page
    if p.family.slug.startswith("00"):
        return redirect("/protein/"+slug)

    # get family list
    pf = p.family
    families = [pf.name]
    while pf.parent.parent:
        families.append(pf.parent.name)
        pf = pf.parent
    families.reverse()

    # get protein aliases
    aliases = ProteinAlias.objects.filter(protein=p).values_list('name', flat=True)

    # get genes
    genes = Gene.objects.filter(proteins=p).values_list('name', flat=True)
    gene = ""
    alt_genes = ""
    if len(gene) > 0:
        gene = genes[0]
        alt_genes = genes[1:]

    # get structures of this signal protein
    structures = SignprotStructure.objects.filter(protein=p)
    complex_structures = SignprotComplex.objects.filter(protein=p)

    # mutations
    mutations = MutationExperiment.objects.filter(protein=p)

    # get residues
    pc = ProteinConformation.objects.get(protein=p)

    residues = Residue.objects.filter(protein_conformation=pc).order_by('sequence_number').prefetch_related(
        'protein_segment', 'generic_number', 'display_generic_number')

    # process residues and return them in chunks of 10
    # this is done for easier scaling on smaller screens
    chunk_size = 10
    r_chunks = []
    r_buffer = []
    last_segment = False
    border = False
    title_cell_skip = 0
    for i, r in enumerate(residues):
        # title of segment to be written out for the first residue in each segment
        segment_title = False

        # keep track of last residues segment (for marking borders)
        if r.protein_segment.slug != last_segment:
            last_segment = r.protein_segment.slug
            border = True

        # if on a border, is there room to write out the title? If not, write title in next chunk
        if i == 0 or (border and len(last_segment) <= (chunk_size - i % chunk_size)):
            segment_title = True
            border = False
            title_cell_skip += len(last_segment)  # skip cells following title (which has colspan > 1)

        if i and i % chunk_size == 0:
            r_chunks.append(r_buffer)
            r_buffer = []

        r_buffer.append((r, segment_title, title_cell_skip))

        # update cell skip counter
        if title_cell_skip > 0:
            title_cell_skip -= 1
    if r_buffer:
        r_chunks.append(r_buffer)
    context = {'p': p, 'families': families, 'r_chunks': r_chunks, 'chunk_size': chunk_size, 'aliases': aliases,
               'gene': gene, 'alt_genes': alt_genes, 'structures': structures, 'complex_structures': complex_structures,
               'mutations': mutations}

    return render(request,
                  'signprot/signprot_details.html',
                  context
                  )

def sort_a_by_b(a, b, remove_invalid=False):
    '''Sort one list based on the order of elements from another list'''
    # https://stackoverflow.com/q/12814667
    # a = ['alpha_mock', 'van-der-waals', 'ionic']
    # b = ['ionic', 'aromatic', 'hydrophobic', 'polar', 'van-der-waals', 'alpha_mock']
    # sort_a_by_b(a,b) -> ['ionic', 'van-der-waals', 'alpha_mock']
    if remove_invalid:
        a = [a_elem for a_elem in a if a_elem in b]
    return sorted(a, key=lambda x: b.index(x))

def interface_dataset():
    # correct receptor entry names - the ones with '_a' appended
    complex_objs = SignprotComplex.objects.prefetch_related('structure__protein_conformation__protein')

    # TOFIX: Current workaround is forcing _a to pdb for indicating alpha-subunit
    # complex_names = [complex_obj.structure.protein_conformation.protein.entry_name + '_' + complex_obj.alpha.lower() for
    #                 complex_obj in complex_objs]
    complex_names = [complex_obj.structure.protein_conformation.protein.entry_name + '_a' for
                     complex_obj in complex_objs]

    complex_struc_ids = [co.structure_id for co in complex_objs]
    # protein conformations for those
    prot_conf = ProteinConformation.objects.filter(protein__entry_name__in=complex_names).values_list('id', flat=True)

    interaction_sort_order = [
        "ionic",
        "aromatic",
        "polar",
        "hydrophobic",
        "van-der-waals",
    ]

    # getting all the signal protein residues for those protein conformations
    prot_residues = Residue.objects.filter(
        protein_conformation__in=prot_conf
    ).values_list('id', flat=True)

    interactions = InteractingResiduePair.objects.filter(
        Q(res1__in=prot_residues) | Q(res2__in=prot_residues),
        referenced_structure__in=complex_struc_ids
    ).exclude(
        Q(res1__in=prot_residues) & Q(res2__in=prot_residues)
    ).prefetch_related(
        'interaction__interaction_type',
        'referenced_structure__pdb_code__index',
        'referenced_structure__signprot_complex__protein__entry_name',
        'referenced_structure__protein_conformation__protein__parent__entry_name',
        'res1__amino_acid',
        'res1__sequence_number',
        'res1__generic_number__label',
        'res2__amino_acid',
        'res2__sequence_number',
        'res2__generic_number__label',
    ).order_by(
        'res1__generic_number__label',
        'res2__generic_number__label'
    ).values(
        int_id=F('id'),
        int_ty=ArrayAgg(
            'interaction__interaction_type',
            distinct=True,
            # ordering=interaction_sort_order
        ),
        pdb_id=F('referenced_structure__pdb_code__index'),
        conf_id=F('referenced_structure__protein_conformation_id'),
        gprot=F('referenced_structure__signprot_complex__protein__entry_name'),
        entry_name=F('referenced_structure__protein_conformation__protein__parent__entry_name'),

        rec_aa=F('res1__amino_acid'),
        rec_pos=F('res1__sequence_number'),
        rec_gn=F('res1__generic_number__label'),

        sig_aa=F('res2__amino_acid'),
        sig_pos=F('res2__sequence_number'),
        sig_gn=F('res2__generic_number__label')
    )

    conf_ids = set()
    for i in interactions:
        i['int_ty'] = sort_a_by_b(i['int_ty'], interaction_sort_order)
        conf_ids.update([i['conf_id']])

    return list(conf_ids), list(interactions)

@method_decorator(csrf_exempt)
def AJAX_Interactions(request):
    t1 = time.time()
    selected_pdbs = request.POST.getlist("selected_pdbs[]")
    effector = request.POST.get('effector')
    # selected_pdbs = request.GET.get('selected_pdbs') if request.GET.get('selected_pdbs') != 'false' else False
    # if selected_pdbs is false throw and error and get back
    # correct receptor entry names - the ones with '_a' appended
    if effector == 'G alpha':
        complex_names = [pdb_name.lower() + '_a' for pdb_name in selected_pdbs]
    elif effector == 'A':
        complex_names = [pdb_name.lower() + '_arrestin' for pdb_name in selected_pdbs]
    pdbs_names = [pdb.lower() for pdb in selected_pdbs]
    complex_objs = SignprotComplex.objects.filter(structure__protein_conformation__protein__entry_name__in=pdbs_names).prefetch_related('structure__protein_conformation__protein')
    # fetching the id of the selected structures
    complex_struc_ids = [co.structure_id for co in complex_objs]
    # protein conformations for those
    prot_conf = ProteinConformation.objects.filter(protein__entry_name__in=complex_names).values_list('id', flat=True)
    # correct receptor entry names - the ones with '_a' appended

    interaction_sort_order = [
        "ionic",
        "aromatic",
        "polar",
        "hydrophobic",
        "van-der-waals",
    ]

    # getting all the signal protein residues for those protein conformations
    prot_residues = Residue.objects.filter(
        protein_conformation__in=prot_conf
    ).values_list('id', flat=True)

    interactions = InteractingResiduePair.objects.filter(
        Q(res1__in=prot_residues) | Q(res2__in=prot_residues),
        referenced_structure__in=complex_struc_ids
    ).exclude(
        Q(res1__in=prot_residues) & Q(res2__in=prot_residues)
    ).prefetch_related(
        'interaction__interaction_type',
        'referenced_structure__pdb_code__index',
        'referenced_structure__signprot_complex__protein__entry_name',
        'referenced_structure__protein_conformation__protein__parent__entry_name',
        'res1__amino_acid',
        'res1__sequence_number',
        'res1__generic_number__label',
        'res2__amino_acid',
        'res2__sequence_number',
        'res2__generic_number__label',
    ).order_by(
        'res1__generic_number__label',
        'res2__generic_number__label'
    ).values(
        int_id=F('id'),
        int_ty=ArrayAgg(
            'interaction__interaction_type',
            distinct=True,
            # ordering=interaction_sort_order
        ),
        pdb_id=F('referenced_structure__pdb_code__index'),
        conf_id=F('referenced_structure__protein_conformation_id'),
        gprot=F('referenced_structure__signprot_complex__protein__entry_name'),
        entry_name=F('referenced_structure__protein_conformation__protein__parent__entry_name'),

        rec_aa=F('res1__amino_acid'),
        rec_pos=F('res1__sequence_number'),
        rec_gn=F('res1__generic_number__label'),

        sig_aa=F('res2__amino_acid'),
        sig_pos=F('res2__sequence_number'),
        sig_gn=F('res2__generic_number__label')
    )

    conf_ids = set()
    for i in interactions:
        i['int_ty'] = sort_a_by_b(i['int_ty'], interaction_sort_order)
        conf_ids.update([i['conf_id']])

    prot_conf_ids = list(conf_ids)
    remaining_residues = Residue.objects.filter(
        protein_conformation_id__in=prot_conf_ids,
    ).prefetch_related(
        "protein_conformation",
        "protein_conformation__protein",
        "protein_conformation__structure"
    ).values(
        rec_id=F('protein_conformation__protein__id'),
        name=F('protein_conformation__protein__parent__name'),
        entry_name=F('protein_conformation__protein__parent__entry_name'),
        pdb_id=F('protein_conformation__structure__pdb_code__index'),
        rec_aa=F('amino_acid'),
        rec_gn=F('generic_number__label'),
    ).exclude(
        Q(rec_gn=None)
    )

    t2 = time.time()
    print('AJAX Runtime: {}'.format((t2 - t1) * 1000.0))

    return JsonResponse([list(remaining_residues), list(interactions)], safe=False)



def ArrestinInteractionMatrix(request):
    return InteractionMatrix(request, database="arrestin")

def GProteinInteractionMatrix(request):
    return InteractionMatrix(request, database="gprotein")

# @cache_page(60 * 60 * 24 * 7)
def InteractionMatrix(request, database='gprotein'):
    # prot_conf_ids, dataset = interface_dataset()

    if database == 'gprotein':
        gprotein_order = ProteinSegment.objects.filter(proteinfamily='Alpha').values('id', 'slug')
    elif database == 'arrestin':
        arrestin_order = ProteinSegment.objects.filter(proteinfamily='Arrestin').values('id', 'slug')

    receptor_order = ['N', '1', '12', '2', '23', '3', '34', '4', '45', '5', '56', '6', '67', '7', '78', '8', 'C']

    struc = SignprotComplex.objects.prefetch_related(
        'structure__pdb_code',
        'structure__stabilizing_agents',
        'structure__protein_conformation__protein__species',
        'structure__protein_conformation__protein__parent__parent__parent',
        'structure__protein_conformation__protein__family__parent__parent__parent__parent',
        'structure__stabilizing_agents',
        'structure__signprot_complex__protein__family__parent__parent__parent__parent',
    )

    complex_info = []
    for s in struc:
        r = {}
        s = s.structure
        r['pdb_id'] = s.pdb_code.index
        r['name'] = s.protein_conformation.protein.parent.short()
        r['entry_name'] = s.protein_conformation.protein.parent.entry_name
        r['class'] = s.protein_conformation.protein.get_protein_class()
        r['family'] = s.protein_conformation.protein.get_protein_family()
        r['conf_id'] = s.protein_conformation.id
        r['organism'] = s.protein_conformation.protein.species.common_name
        try:
            r['gprot'] = s.get_stab_agents_gproteins()
        except Exception:
            r['gprot'] = ''
        try:
            r['gprot_class'] = s.get_signprot_gprot_family()
        except Exception:
            r['gprot_class'] = ''
        complex_info.append(r)

    # remaining_residues = Residue.objects.filter(
    #     protein_conformation_id__in=prot_conf_ids,
    # ).prefetch_related(
    #     "protein_conformation",
    #     "protein_conformation__protein",
    #     "protein_conformation__structure"
    # ).values(
    #     rec_id=F('protein_conformation__protein__id'),
    #     name=F('protein_conformation__protein__parent__name'),
    #     entry_name=F('protein_conformation__protein__parent__entry_name'),
    #     pdb_id=F('protein_conformation__structure__pdb_code__index'),
    #     rec_aa=F('amino_acid'),
    #     rec_gn=F('generic_number__label'),
    # ).exclude(
    #     Q(rec_gn=None)
    # )

    if database == "gprotein":
        context = {
            'page': database,
            # 'interactions': json.dumps(dataset),
            'interactions_metadata': json.dumps(complex_info),
            # 'non_interactions': json.dumps(list(remaining_residues)),
            'gprot': json.dumps(list(gprotein_order)),
            'receptor': json.dumps(receptor_order),
        }
    elif database == "arrestin":
        context = {
            'page': database,
            # 'interactions': json.dumps(dataset),
            'interactions_metadata': json.dumps(complex_info),
            # 'non_interactions': json.dumps(list(remaining_residues)),
            'gprot': json.dumps(list(arrestin_order)),
            'receptor': json.dumps(receptor_order),
        }

    request.session['signature'] = None
    request.session.modified = True
    return render(request,
                  'signprot/matrix.html',
                  context
                  )

@method_decorator(csrf_exempt)
def IMSequenceSignature(request):
    """Accept set of proteins + generic numbers and calculate the signature for those"""

    pos_set_in = get_entry_names(request)
    ignore_in_alignment = get_ignore_info(request)
    segments = get_protein_segments(request)
    if len(segments) == 0:
        segments = list(ResidueGenericNumberEquivalent.objects.filter(scheme__slug__in=['gpcrdba']))

    # get pos objects
    pos_set = Protein.objects.filter(entry_name__in=pos_set_in).select_related('residue_numbering_scheme', 'species')

    # Calculate Sequence Signature
    signature = SequenceSignature()

    # WHY IS THIS IGNORE USED -> it ignores counting of proteins for residue positions instead of ignoring residue positions
    ignore_in_alignment = {}
    signature.setup_alignments_signprot(segments, pos_set, ignore_in_alignment=ignore_in_alignment)
    signature.calculate_signature_onesided()
    # preprocess data for return
    signature_data = signature.prepare_display_data_onesided()

    # FEATURES AND REGIONS
    feats = [feature for feature in signature_data['a_pos'].features_combo]

    # GET GENERIC NUMBERS
    generic_numbers = get_generic_numbers(signature_data)

    # FEATURE FREQUENCIES
    signature_features = get_signature_features(signature_data, generic_numbers, feats)
    grouped_features = group_signature_features(signature_features)

    # # FEATURE CONSENSUS
    # generic_numbers_flat = list(chain.from_iterable(generic_numbers))
    # sigcons = get_signature_consensus(signature_data, generic_numbers_flat)

    # rec_class = pos_set[0].get_protein_class()

    # dump = {
    #     'rec_class': rec_class,
    #     'signature': signature,
    #     'consensus': signature_data,
    #     }
    # with open('signprot/notebooks/interface_pickles/{}.p'.format(rec_class), 'wb+') as out_file:
    #     pickle.dump(dump, out_file)

    # pass back to front
    res = {
        # 'cons': sigcons,
        'feat_ungrouped': signature_features,
        'feat': grouped_features,
    }

    request.session['signature'] = signature.prepare_session_data()
    request.session.modified = True

    return JsonResponse(res, safe=False)

@method_decorator(csrf_exempt)
def IMSignatureMatch(request):
    '''Take the signature stored in the session and query the db'''
    signature_data = request.session.get('signature')
    ss_pos = get_entry_names(request)
    cutoff = request.POST.get('cutoff')
    effector = request.POST.get('filtering_particle')

    request.session['ss_pos'] = ss_pos
    request.session['cutoff'] = cutoff

    pos_set = Protein.objects.filter(entry_name__in=ss_pos).select_related('residue_numbering_scheme', 'species')\
            .prefetch_related('family')
    pos_set = [protein for protein in pos_set]
    pfam = [protein.family.slug[:3] for protein in pos_set]

    signature_match = SignatureMatch(
        signature_data['common_positions'],
        signature_data['numbering_schemes'],
        signature_data['common_segments'],
        signature_data['diff_matrix'],
        pos_set,
        # pos_set,
        cutoff=0,
        signprot=True
    )

    maj_pfam = Counter(pfam).most_common()[0][0]
    signature_match.score_protein_class(maj_pfam, signprot=True)
    # request.session['signature_match'] = signature_match
    signature_match = {
        'scores': signature_match.protein_report,
        'scores_pos': signature_match.scores_pos,
        # 'scores_neg': signature_match.scores_neg,
        'protein_signatures': signature_match.protein_signatures,
        'signatures_pos': signature_match.signatures_pos,
        # 'signatures_neg': signature_match.signatures_neg,
        'signature_filtered': signature_match.signature_consensus,
        'relevant_gn': signature_match.relevant_gn,
        'relevant_segments': signature_match.relevant_segments,
        'numbering_schemes': signature_match.schemes,
    }

    signature_match_parsed = prepare_signature_match(signature_match, effector)
    return JsonResponse(signature_match_parsed, safe=False)

@method_decorator(csrf_exempt)
def render_IMSigMat(request):
    # signature_match = request.session.get('signature_match')
    signature_data = request.session.get('signature')
    ss_pos = request.session.get('ss_pos')
    #cutoff = request.session.get('cutoff')

    pos_set = Protein.objects.filter(entry_name__in=ss_pos).select_related('residue_numbering_scheme', 'species')
    pos_set = [protein for protein in pos_set]
    pfam = [protein.family.slug[:3] for protein in pos_set]

    signature_match = SignatureMatch(
        signature_data['common_positions'],
        signature_data['numbering_schemes'],
        signature_data['common_segments'],
        signature_data['diff_matrix'],
        pos_set,
        # pos_set,
        cutoff=0,
        signprot=True
    )

    maj_pfam = Counter(pfam).most_common()[0][0]
    signature_match.score_protein_class(maj_pfam, signprot=True)

    response = render(
        request,
        'signprot/signature_match.html',
        {'scores': signature_match}
    )
    return response
