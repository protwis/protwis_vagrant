import hashlib
import itertools
import json
import re
import time
import pandas as pd
import urllib

from random import SystemRandom
from copy import deepcopy
from collections import defaultdict, OrderedDict

from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.views.generic import TemplateView, DetailView

from django.db.models import Q, Count, Subquery, OuterRef
from django.views.decorators.csrf import csrf_exempt

from django.core.cache import cache

from common.views import AbsReferenceSelectionTable, getReferenceTable, getLigandTable, getLigandCountTable
from common.models import ReleaseNotes, WebResource, Publication
from common.phylogenetic_tree import PhylogeneticTreeGenerator
from common.selection import Selection, SelectionItem
from ligand.models import Ligand, LigandVendorLink, BiasedPathways, AssayExperiment, BiasedData, Endogenous_GTP, LigandID
from ligand.functions import OnTheFly, AddPathwayData
from protein.models import Protein, ProteinFamily
from interaction.models import StructureLigandInteraction
from mutation.models import MutationExperiment

class LigandTargetSelection(AbsReferenceSelectionTable):
        step = 1
        number_of_steps = 1
        filters = False
        filter_tableselect = False
        family_tree = False
        import_export_box = False
        ligand_js = True

        title = "SELECT A RECEPTOR with ligand assays"
        description = 'Select a receptor in the table (below).' \
            + '\n\nOnce you have selected your receptor, click a green button.'

        selection_boxes = OrderedDict([
            ('reference', True),
            ('targets', False),
            ('segments', False),
        ])

        buttons = {
            'continue': {
                'label': 'Compact (1 row/ligand)',
                'onclick': "submitSelection('/ligand/targets_compact');",
                'color': 'success',
                "sameSize": True,
            },
            'pathway': {
                'label': "Extended (1 row/activity)",
                'onclick': "submitSelection('/ligand/target_detail');",
                'color': 'success',
                "sameSize": True,
            },
        }

        def get_context_data(self, **kwargs):
            """Get context from parent class

            (really only relevant for children of this class, as TemplateView does
            not have any context variables)
            """
            context = super().get_context_data(**kwargs)
            context['table_data'] = getLigandCountTable()

            return context

def LigandDetails(request, ligand_id):
    """
    The details of a ligand record. Lists all the assay experiments for a given ligand.
    """
    ligand_records = AssayExperiment.objects.filter(
        ligand__ids__index=ligand_id
    ).order_by('protein__entry_name')

    record_count = ligand_records.values(
        'protein',
    ).annotate(num_records=Count('protein__entry_name')
               ).order_by('protein__entry_name')

    ligand_data = []

    for record in record_count:
        per_target_data = ligand_records.filter(protein=record['protein'])
        protein_details = Protein.objects.get(pk=record['protein'])
        """
        A dictionary of dictionaries with a list of values.
        Assay_type|->  Standard_type [list of values]
        """
        tmp = defaultdict(lambda: defaultdict(list))
        tmp_count = 0
        for data_line in per_target_data:
            tmp[data_line.assay_type][data_line.value_type].append(
                data_line.standard_activity_value)
            tmp_count += 1

        # Flattened list of lists of dict values
        values = list(itertools.chain(
            *[itertools.chain(*tmp[x].values()) for x in tmp.keys()]))
        # TEMPORARY workaround for handling string values
        values = [float(item) for item in values if float(item)]

        if len(values) > 0:
            ligand_data.append({
                'protein_name': protein_details.entry_name,
                'receptor_family': protein_details.family.parent.name,
                'ligand_type': protein_details.get_protein_family(),
                'class': protein_details.get_protein_class(),
                'record_count': tmp_count,
                'assay_type': ', '.join(tmp.keys()),
                # Flattened list of lists of dict keys:
                'value_types': ', '.join(itertools.chain(*(list(tmp[x]) for x in tmp.keys()))),
                'low_value': min(values),
                'average_value': sum(values) / len(values)
                # 'standard_units': ', '.join(list(set([x.standard_units for x in per_target_data])))
            })

    context = {'ligand_data': ligand_data, 'ligand': ligand_id}

    return render(request, 'ligand_details.html', context)

def TargetDetailsCompact(request, **kwargs):
    cache_key = False
    simple_selection = request.session.get('selection', False)
    selection = Selection()
    if simple_selection:
        selection.importer(simple_selection)
    if selection.reference != []:
        prot_id = [x.item for x in selection.reference]
        cache_key = "lig_compact_protid_" + ",".join(prot_id)
        ps = AssayExperiment.objects.filter(
            protein__in=prot_id, ligand__ids__web_resource__slug='chembl_ligand')

    # if queryset is empty redirect to ligand browser
    if not ps:
        return redirect("ligand_selection")

    if cache_key != False and cache.has_key(cache_key):
        ligand_data = cache.get(cache_key)
    else:
        ps = ps.prefetch_related(
            'protein', 'ligand__ids__web_resource')
        d = {}
        for p in ps:
            if p.ligand not in d:
                d[p.ligand] = {}
            if p.protein not in d[p.ligand]:
                d[p.ligand][p.protein] = []
            d[p.ligand][p.protein].append(p)


        lig_ids = set([record.ligand_id for record in ps])
        purchasable = list(LigandVendorLink.objects.filter(ligand__pk__in=lig_ids).exclude(vendor__name__in=['ZINC', 'ChEMBL',
            'BindingDB', 'SureChEMBL', 'eMolecules', 'MolPort', 'PubChem']).values_list('ligand__pk', flat = True).distinct())

        ligand_data = []
        for lig, records in d.items():

            links = lig.ids.all()
            chembl_id = [x for x in links if x.web_resource.slug ==
                         'chembl_ligand'][0].index

            purchasability = 'No'
            if lig.id in purchasable:
                    purchasability = 'Yes'

            for record, vals in records.items():
                per_target_data = vals
                protein_details = record
                """
                A dictionary of dictionaries with a list of values.
                Assay_type
                |
                ->  Standard_type [list of values]
                """
                tmp = defaultdict(list)
                tmp_count = 0
                for data_line in per_target_data:
                    tmp["Bind" if data_line.assay_type == 'B' else "Funct"].append(
                        data_line.p_activity_value)
                    tmp_count += 1

                # TEMPORARY workaround for handling string values
                values = [float(item) for item in itertools.chain(
                    *tmp.values()) if item != None and item != "None" and float(item)]

                if len(values) > 0:
                    ligand_data.append({
                        'lig_id': lig.id,
                        'ligand_id': chembl_id,
                        'protein_name': protein_details.entry_name,
                        'species': protein_details.species.common_name,
                        'record_count': tmp_count,
                        'assay_type': ', '.join(tmp.keys()),
                        'purchasability': purchasability,
                        # Flattened list of lists of dict keys:
                        'low_value': min(values),
                        'average_value': sum(values) / len(values),
                        'high_value': max(values),
                        # 'standard_units': ', '.join(list(set([x.standard_units for x in per_target_data]))),
                        'smiles': lig.smiles,
                        'mw': lig.mw,
                        'rotatable_bonds': lig.rotatable_bonds,
                        'hdon': lig.hdon,
                        'hacc': lig.hacc,
                        'logp': lig.logp,
                    })
    context = {}
    context['ligand_data'] = ligand_data
    cache.set(cache_key, ligand_data, 60*60*24*7)

    return render(request, 'target_details_compact.html', context)

def TargetDetailsExtended(request, **kwargs):
    simple_selection = request.session.get('selection', False)
    selection = Selection()
    if simple_selection:
        selection.importer(simple_selection)

    cache_key = False
    if 'slug' in kwargs:
        cache_key = "lig_ext_slug_" + kwargs['slug']
        ps = AssayExperiment.objects.filter(
             protein__family__slug=kwargs['slug'], ligand__ids__web_resource__slug='chembl_ligand')

        # Set selection for purchasability page
        prot_ids = list(AssayExperiment.objects.filter(
             protein__family__slug=kwargs['slug'], ligand__ids__web_resource__slug='chembl_ligand').values_list("protein_id", flat = True).distinct())
        proteins = Protein.objects.filter(pk__in=prot_ids)
        for prot in proteins:
            selection.add('targets', 'protein', SelectionItem('protein', prot))
        simple_selection = selection.exporter()
        request.session['selection'] = simple_selection
    else:
        if selection.reference != []:
            prot_id = [x.item for x in selection.reference]
            if len(prot_id) > 0:
                cache_key = "lig_ext_protid_" + ",".join(prot_id)
                ps = AssayExperiment.objects.filter(
                    protein__in=prot_id, ligand__ids__web_resource__slug='chembl_ligand')

    # if queryset is empty redirect to ligand browser
    if not ps and 'slug' not in kwargs:
        return redirect("ligand_selection")

    if cache_key != False and cache.has_key(cache_key):
        ps = cache.get(cache_key)
    else:
        ps = ps.values('value_type',
                       'standard_relation',
                       'standard_activity_value',
                       'assay_description',
                       'assay_type',
                       # 'standard_units',
                       'p_activity_value',
                       'ligand__id',
                       'ligand__ids__index',
                       'protein__species__common_name',
                       'protein__entry_name',
                       'ligand__mw',
                       'ligand__logp',
                       'ligand__rotatable_bonds',
                       'ligand__smiles',
                       'ligand__hdon',
                       'ligand__hacc', 'protein'
                       ).annotate(num_targets=Count('protein__id', distinct=True))

        lig_ids = set([record['ligand__id'] for record in ps])
        purchasable = list(LigandVendorLink.objects.filter(ligand__pk__in=lig_ids).exclude(vendor__name__in=['ZINC', 'ChEMBL',
            'BindingDB', 'SureChEMBL', 'eMolecules', 'MolPort', 'PubChem']).values_list('ligand__pk', flat = True).distinct())
        for record in ps:
            record['purchasability'] = 'Yes' if record['ligand__id'] in purchasable else 'No'

    context = {}
    context['proteins'] = ps
    cache.set(cache_key, ps, 60*60*24*7)

    return render(request, 'target_details.html', context)


def TargetPurchasabilityDetails(request, **kwargs):
    cache_key = False
    simple_selection = request.session.get('selection', False)
    selection = Selection()
    if simple_selection:
        selection.importer(simple_selection)

    if selection.targets != []:
        prot_ids = [str(x.item.id) for x in selection.targets]
        cache_key = "lig_purchasable_" + ",".join(prot_ids)
        ps = AssayExperiment.objects.filter(
            protein__in=prot_ids, ligand__ids__web_resource__slug='chembl_ligand')
        context = {
            'target': ', '.join([x.item.entry_name for x in selection.targets])
        }
    elif selection.reference != []:
        prot_id = [x.item for x in selection.reference]
        cache_key = "lig_purchasable_" + ",".join(prot_id)
        ps = AssayExperiment.objects.filter(
            protein__in=prot_id, ligand__ids__web_resource__slug='chembl_ligand')
        context = {}

    # if queryset is empty redirect to ligand browser
    if not ps:
        return redirect("ligand_selection")

    if cache_key != False and cache.has_key(cache_key):
        purchasable = cache.get(cache_key)
    else:
        ps = ps.values('value_type',
                       'standard_relation',
                       'standard_activity_value',
                       'assay_description',
                       'assay_type',
                       # 'standard_units',
                       'p_activity_value',
                       'ligand__id',
                       'ligand__ids__index',
                       'protein__species__common_name',
                       'protein__entry_name',
                       'ligand_id',
                       'ligand__name',
                       'ligand__mw',
                       'ligand__logp',
                       'ligand__rotatable_bonds',
                       'ligand__smiles',
                       'ligand__hdon',
                       'ligand__hacc',
                       )

        lig_ids = [entry["ligand_id"] for entry in ps]
        vendorlinks = LigandVendorLink.objects.filter(ligand__id__in=lig_ids)\
            .exclude(vendor__name__in=['ZINC', 'ChEMBL', 'BindingDB', 'SureChEMBL', 'eMolecules', 'MolPort', 'PubChem', 'IUPHAR/BPS Guide to PHARMACOLOGY'])\
            .values("ligand_id", "vendor__name", "vendor__url", "url", "external_id")

        vendor_dict = {}
        for link in vendorlinks:
            if link["ligand_id"] not in vendor_dict:
                vendor_dict[link["ligand_id"]] = []
            vendor_dict[link["ligand_id"]].append(link)

        purchasable = []
        for record in ps:
            if record["ligand_id"] in vendor_dict:
                for link in vendor_dict[record["ligand_id"]]:
                    entry = {**record, **link}
                    purchasable.append(entry)

    context['proteins'] = purchasable
    cache.set(cache_key, purchasable, 60*60*24*7)
    return render(request, 'target_purchasability_details.html', context)

#Biased Effector Family View (handles browser, Emax/Tau RankOder and Emax/Tau PathProfile)
class BiasedSignallingSelection(AbsReferenceSelectionTable):
    step = 1
    number_of_steps = 1
    filters = False
    filter_tableselect = False
    family_tree = False
    import_export_box = False
    subtype = False
    pathway = False
    pathfinder = {'EmaxRankOrder': {'Description': 'The next page shows plots for the ligand bias rank order ΔΔLog(Emax/EC50) by transducer or effector family across publications.' \
                                                    + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                    + '\nBiased ligands have a bias factor ≥ 5.',
                                    'Continue': "submitSelection('/biased_signalling/emax_rankorder');",
                                    'Pathway': "submitSelection('/biased_signalling/emax_rankorder_path_bias');",
                                    'Biased': "submitSelection('/biased_signalling/userselectionbiased_emax_rank_order');"},
                  'TauRankOrder': {'Description': 'The next page shows plots for the ligand bias rank order ΔΔLog(Tau/KA) by transducer or effector family across publications.' \
                                                    + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                    + '\nBiased ligands have a bias factor ≥ 5.',
                                   'Continue': "submitSelection('/biased_signalling/tau_rankorder');",
                                   'Pathway': "submitSelection('/biased_signalling/tau_rankorder_path_bias');",
                                   'Biased': "submitSelection('/biased_signalling/userselectionbiased_tau_rank_order');"},
                  'EmaxPathProfile': {'Description': 'The next page shows plots for the ligand pathway profiles ΔLog(Emax/EC50) by transducer or effector family across publications.' \
                                                     + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                     + '\nBiased ligands have a bias factor ≥ 5.',
                                      'Continue': "submitSelection('/biased_signalling/emax_path_profiles');",
                                      'Pathway': "submitSelection('/biased_signalling/emax_path_profiles_path_bias');",
                                      'Biased': "submitSelection('/biased_signalling/userselectionbiased_emax_path_profile');"},
                  'TauPathProfile': {'Description': 'The next page shows plots for the ligand pathway profiles ΔLog(Tau/KA) by transducer or effector family across publications.' \
                                                     + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                     + '\nBiased ligands have a bias factor ≥ 5.',
                                     'Continue': "submitSelection('/biased_signalling/tau_path_profiles');",
                                     'Pathway': "submitSelection('/biased_signalling/tau_path_profiles_path_bias');",
                                     'Biased': "submitSelection('/biased_signalling/userselectionbiased_tau_path_profile');"},
                  'EmaxRankOrderSubtype': {'Description': 'The next page shows plots for the ligand bias rank order ΔΔLog(Emax/EC50) by transducer or effector family across publications.' \
                                                            + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                            + '\nBiased ligands have a bias factor ≥ 5.',
                                           'Continue': "submitSelection('/biased_signalling/subtype_emax_rankorder');",
                                           'Pathway': "submitSelection('/biased_signalling/subtype_emax_rankorder_path_bias');",
                                           'Biased': "submitSelection('/biased_signalling/userselectionbiasedsubtype_emax_rank_order');"},
                  'TauRankOrderSubtype': {'Description': 'The next page shows plots for the ligand bias rank order ΔΔLog(Tau/KA) by transducer or effector family across publications.' \
                                                         + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                         + '\nBiased ligands have a bias factor ≥ 5.',
                                          'Continue': "submitSelection('/biased_signalling/subtype_tau_rankorder');",
                                          'Pathway': "submitSelection('/biased_signalling/subtype_tau_rankorder_path_bias');",
                                          'Biased': "submitSelection('/biased_signalling/userselectionbiasedsubtype_tau_rank_order');"},
                  'EmaxPathProfileSubtype': {'Description': 'The next page shows plots for the ligand pathway profiles ΔLog(Emax/EC50) by transducer or effector family across publications.' \
                                                            + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                            + '\nBiased ligands have a bias factor ≥ 5.',
                                             'Continue': "submitSelection('/biased_signalling/subtype_emax_path_profiles');",
                                             'Pathway': "submitSelection('/biased_signalling/subtype_emax_path_profiles_path_bias');",
                                             'Biased': "submitSelection('/biased_signalling/userselectionbiasedsubtype_emax_path_profile');"},
                  'TauPathProfileSubtype': {'Description': 'The next page shows plots for the ligand pathway profiles ΔLog(Tau/KA) by transducer or effector family across publications.' \
                                                            + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                            + '\nBiased ligands have a bias factor ≥ 5.',
                                            'Continue': "submitSelection('/biased_signalling/subtype_tau_path_profiles');",
                                            'Pathway': "submitSelection('/biased_signalling/subtype_tau_path_profiles_path_bias');",
                                            'Biased': "submitSelection('/biased_signalling/userselectionbiasedsubtype_tau_path_profile');"},
                  'Browser': {'Description': 'The next page shows ligands biased for a transducer or effector family (e.g. G protein, arrestin, GRK, ERK etc.).' \
                                                    + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                    + '\nBiased ligands have a bias factor ≥ 5.',
                              'Continue': "submitSelection('/biased_signalling/biased');",
                              'Pathway': "submitSelection('/biased_signalling/pathwaybiased');",
                              'Biased': "submitSelection('/biased_signalling/userselectionbiased');"},
                  'BrowserSubtype': {'Description': 'The next page shows ligands biased for a transducer or effector family (e.g. G protein, arrestin, GRK, ERK etc.).' \
                                                    + '\nPathway-preference is not bias as no reference ligand is used. See the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                    + '\nBiased ligands have a bias factor ≥ 5.',
                                     'Continue': "submitSelection('/biased_signalling/biasedsubtypes');",
                                     'Pathway': "submitSelection('/biased_signalling/pathwaybiasedsubtypes');",
                                     'Biased': "submitSelection('/biased_signalling/userselectionbiasedsubtype');"},
                  'BrowserPathway': {'Description': 'The next page shows ligands preferring a transducer or effector family (e.g. G protein, arrestin, GRK, ERK etc.).' \
                                                    + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                    + '\nBiased ligands have a bias factor ≥ 5.',
                                     'Continue': "submitSelection('/biased_signalling/pathwaypreference');"},
                  'EmaxRankOrderPathway': {'Description': 'The next page shows plots for the ligand bias rank order ΔLog(Emax/EC50) by transducer or effector family across publications.' \
                                                            + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                            + '\nBiased ligands have a bias factor ≥ 5.',
                                           'Continue': "submitSelection('/biased_signalling/path_preference_emax_rankorder');"},
                  'EmaxPathProfilePathway': {'Description': 'The next page shows plots for the ligand pathway profiles Log(Emax/EC50) by transducer or effector family across publications.' \
                                                            + '\nPhysiology-bias, pathway-bias and benchmark-bias is explained in the article <a href="https://bpspubs.onlinelibrary.wiley.com/doi/abs/10.1111/bph.15811" target="_blank">Community Guidelines for GPCR Ligand Bias</a>.' \
                                                            + '\nBiased ligands have a bias factor ≥ 5.',
                                            'Continue': "submitSelection('/biased_signalling/path_preference_emax_path_profiles');"}
                }
    way = 'EmaxRankOrder'
    title = "SELECT A RECEPTOR in the table and click a green button"
    description = pathfinder[way]['Description']
    # description = 'Select a receptor in the table (below).' \
    #     + ' \n\nand click the green button (upper right).'
    selection_boxes = OrderedDict([
        ('reference', True),
        ('targets', False),
        ('segments', False),
    ])

    buttons = {
        "pathway": {
            "label": "Pathway-biased ligands<br>(balanced reference ligand)",
            'onclick': pathfinder[way]['Pathway'],
            'color': 'success',
            "sameSize": True,
        },
        'continue': {
            'label': 'Physiology-biased ligands<br>(endogenous agonist reference)',
            'onclick': pathfinder[way]['Continue'],
            'color': 'success',
            'invisible': 'No',
            "sameSize": True,
        },
        "biased": {
            "label": "Biased ligands<br>(other reference ligand)",
            'onclick': pathfinder[way]['Biased'],
            "color": 'success',
            'invisible': 'No',
            "sameSize": True,
        },
    }


    def get_context_data(self, **kwargs):
        """Get context from parent class

        (really only relevant for children of this class, as TemplateView does
        not have any context variables)
        """
        context = super().get_context_data(**kwargs)

        context['buttons']['continue']['onclick'] = context['pathfinder'][context['way']]['Continue']
        context['description'] = context['pathfinder'][context['way']]['Description']
        # get selection from session and add to context
        if context['subtype']: #subtype define all three buttons
            context['table_data'] = getReferenceTable("no", "yes")
            context['buttons']['pathway']['onclick'] = context['pathfinder'][context['way']]['Pathway']
            context['buttons']['biased']['onclick'] = context['pathfinder'][context['way']]['Biased']
            context['buttons']['pathway']['invisible'] = "No"
            context['buttons']['biased']['invisible'] = "No"
        elif context['pathway']: #pathway define only continue button, delete others
            context['table_data'] = getReferenceTable("yes", "no")
            context['buttons']['continue']['label'] = 'Pathway-preferring ligands<br>(no reference)'
            context['buttons']['pathway']['invisible'] = "Yes"
            context['buttons']['biased']['invisible'] = "Yes"
        else: #not subtype not pathway, define all three buttons
            context['table_data'] = getReferenceTable("no", "no")
            context['buttons']['pathway']['onclick'] = context['pathfinder'][context['way']]['Pathway']
            context['buttons']['biased']['onclick'] = context['pathfinder'][context['way']]['Biased']
            context['buttons']['pathway']['invisible'] = "No"
            context['buttons']['biased']['invisible'] = "No"

        return context

#Biased Effector Family Browser (Ligand Selection)
class UserBiased(AbsReferenceSelectionTable):

    template_name = 'common/userligandselectiontable.html'
    step = 2
    number_of_steps = 2
    filters = False
    filter_tableselect = False
    import_export_box = False
    family_tree = False
    docs = 'sequences.html#structure-based-alignments'
    title = "SELECT LIGAND to be used as reference for the calculation of bias ligands"
    description = 'Select a ligand in the table (below)\nThen click the green button (right)'
    way = 'Browser'
    subtype = False

    analysis = {
    #Biased Effector Family Browser (Ligand Selection)
    'Browser': "submitSelection('/biased_signalling/userbiased');",
    #Biased Effector Family Emax/EC50 Rank Order (Ligand Selection)
    'EmaxRankOrder': "submitSelection('/biased_signalling/userbiased_emax_rank_order');",
    #Biased Effector Family Tau/KA Rank Order (Ligand Selection)
    'TauRankOrder': "submitSelection('/biased_signalling/userbiased_tau_rank_order');",
    #Biased Effector Family Emax/EC50 Pathway Profiles (Ligand Selection)
    'EmaxPathProfile': "submitSelection('/biased_signalling/userbiased_emax_path_profile');",
    #Biased Effector Family Tau/KA Pathway Profiles (Ligand Selection)
    'TauPathProfile': "submitSelection('/biased_signalling/userbiased_tau_path_profile');",
    #Biased Effector Subtype Browser (Ligand Selection)
    'BrowserSubtype': "submitSelection('/biased_signalling/userbiasedsubtypes');",
    #Biased Effector Subtype Emax/EC50 Rank Order (Ligand Selection)
    'EmaxRankOrderSubtype': "submitSelection('/biased_signalling/userbiasedsubtypes_emax_rank_order');",
    #Biased Effector Subtype Tau/KA Rank Order (Ligand Selection)
    'TauRankOrderSubtype': "submitSelection('/biased_signalling/userbiasedsubtypes_tau_rank_order');",
    #Biased Effector Subtype Emax/EC50 Pathway Profiles (Ligand Selection)
    'EmaxPathProfilesSubtype': "submitSelection('/biased_signalling/userbiasedsubtypes_emax_path_profile');",
    #Biased Effector Subtype Tau/KA Pathway Profiles (Ligand Selection)
    'TauPathProfilesSubtype': "submitSelection('/biased_signalling/userbiasedsubtypes_tau_path_profile');"}

    selection_boxes = OrderedDict([
        ('reference', True),
        ('targets', True),
        ('segments', False),
    ])
    buttons = {
        'continue': {
            'label': 'Calculate bias',
            'onclick': "submitSelection('/biased_signalling/userbiased');",
            'color': 'success',
        },
    }

    def get_context_data(self, **kwargs):
        """Get context from parent class

        (really only relevant for children of this class, as TemplateView does
        not have any context variables)
        """
        protein_ids = []
        context = super().get_context_data(**kwargs)

        # get selection from session and add to context
        # get simple selection from session
        simple_selection = self.request.session.get('selection', False)
        # create full selection and import simple selection (if it exists)
        for target in simple_selection.reference:
            protein_ids.append(target.item)
        if context['subtype']:
            context['table_data'] = getLigandTable(protein_ids[0], "subtype")
        else:
            context['table_data'] = getLigandTable(protein_ids[0], "biased")

        context['buttons']['continue']['onclick'] = context['analysis'][context['way']]
        return context

class BiasedSignallingOnTheFlyCalculation(TemplateView):
    #set a global variable for different pages
    page = "rankorder"
    label = "emax"
    template_name = "otf_biased_rank_orders.html"
    subtype = False
    pathway = False
    balanced = False
    user = False

    @staticmethod
    def jitter_tooltip(page, pathway, ligand, value, headers, prefix='', small_data=None, large_data=None, small_ref=None):
        #small and large data has to structured
        #small --> pathway/value/value
        #large --> pathway1/delta/value/value pathway2/delta/value/value
        ref_small = ''
        small = ''
        large = ''
        head = "<b>Compound Name:</b> " + str(ligand) + \
               "<br><b>Plotted Value:</b> " + str(value) + \
               "<hr class='solid'>"
        if small_data:
            #small table to show reference ligand or single datapoint
            small =  "<table>" + \
                     "      <tr>" + \
                     "        <th>" + str(small_data[3]) + "</th>" + \
                     "        <th>" + headers[0] + "</th>" + \
                     "        <th>" + headers[1] + "</th>" + \
                     "      </tr>" + \
                     "      <tr>" + \
                     "        <td>" + str(small_data[0]) + "</td>" + \
                     "        <td>" + str(small_data[1]) + "</td>" + \
                     "        <td>" + str(small_data[2]) + "</td>" + \
                     "      </tr>" + \
                     "</table>" + \
                     "<hr class='solid'>"
        if small_ref:
            #small table to show reference ligand or single datapoint
            ref_small =  "<table>" + \
                         "      <tr>" + \
                         "        <th>" + str(small_ref[3]) + "</th>" + \
                         "        <th>" + headers[0] + "</th>" + \
                         "        <th>" + headers[1] + "</th>" + \
                         "      </tr>" + \
                         "      <tr>" + \
                         "        <td>" + str(small_ref[0]) + "</td>" + \
                         "        <td>" + str(small_ref[1]) + "</td>" + \
                         "        <td>" + str(small_ref[2]) + "</td>" + \
                         "      </tr>" + \
                         "</table>"
        if large_data:
            #large table showing also Δ data
            large =  "<table>" + \
                     "      <tr>" + \
                     "        <th>" + str(ligand) + "</th>" + \
                     "        <th>" + prefix + "Log(" + headers[0] + "/" + headers[1] + ") </th>" + \
                     "        <th>" + headers[0] + "</th>" + \
                     "        <th>" + headers[1] + "</th>" + \
                     "      </tr>" + \
                     "      <tr>" + \
                     "        <td>" + str(large_data[0]) + "</td>" + \
                     "        <td>" + str(large_data[1]) + "</td>" + \
                     "        <td>" + str(large_data[2]) + "</td>" + \
                     "        <td>" + str(large_data[3]) + "</td>" + \
                     "      </tr>" + \
                     "      <tr>" + \
                     "        <td>" + str(large_data[4]) + "</td>" + \
                     "        <td>" + str(large_data[5]) + "</td>" + \
                     "        <td>" + str(large_data[6]) + "</td>" + \
                     "        <td>" + str(large_data[7]) + "</td>" + \
                     "      </tr>" + \
                     "</table>" + \
                     "<hr class='solid'>"

        if pathway:
            #no reference values
            if page == "rankorder":
                tip = head + large
                #dot plots without reference values
            else:
                tip = head + small
                #line charts without reference values
        else:
            #reference values required
            if page == "rankorder":
                tip = head + large + small
                #dot plots with reference values
            else:
                tip = head + small + ref_small
                #line chart wtih reference values
        return tip
        #line charts with reference values

    @staticmethod
    def create_rgb_color(): #, name, power): # pseudo-randomization function
        # h = hash( name + str(power) ) # hash string and int together
        # if h < 0: # ensure positive number
        #     h = h * -1
        # random.seed(h) # set the seed to use for randomization
        cryptogen = SystemRandom()          # added cryptography random number creation
        output = cryptogen.randrange(0,225) # 225 instead of 255 to avoid all very pale colors
        return output

    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)
        simple_selection = self.request.session.get('selection', False)
        #I know it's a for cycle, but it should be just one element
        #since it's coming from a reference
        for item in simple_selection.reference:
            receptor = item.item

        if self.user:
            for ligand in simple_selection.targets:
                self.user = ligand.item

        rec_name = list(Protein.objects.filter(id=receptor)
                                        .values_list('entry_name',
                                                     'name'))

        tooltip_dict =  {'G12/13': 'G<sub>12/13</sub>',
                         'Gi/o': 'G<sub>i/o</sub>',
                         'Gq/11': 'G<sub>q/11</sub>',
                         'Gs': 'G<sub>s</sub>',
                         'Arrestin': 'Arrestin',
                         'ERK': 'ERK'}

        sign_prot_conversion = {'-' : 'None',
                                None : 'None',
                                'arrestin-2 (b-arrestin-1)': 'Arrestin 2 (&beta;<sub>1</sub>)',
                                'arrestin-3 (b-arrestin-2)': 'Arrestin 3 (&beta;<sub>2</sub>)',
                                'g11': 'G<sub>11</sub>',
                                'g12': 'G<sub>12</sub>',
                                'g13': 'G<sub>13</sub>',
                                'g14': 'G<sub>14</sub>',
                                'g15': 'G<sub>15</sub>',
                                'g16': 'G<sub>16</sub>',
                                'gaqi5': 'G&alpha;qi5',
                                'gaqδ6i4myr': 'G&alpha;qδ6i4myr',
                                'gi1': 'G<sub>i1</sub>',
                                'gi2': 'G<sub>i2</sub>',
                                'gi3': 'G<sub>i3</sub>',
                                'goa': 'G<sub>o&alpha;</sub>',
                                'gob': 'G<sub>o&beta;</sub>',
                                'golf': 'G<sub>olfactory</sub>',
                                'gq': 'G<sub>q</sub>',
                                'gs': 'G<sub>s</sub>',
                                'gz': 'G<sub>z</sub>',
                                'minigi': 'Mini-G'}

        if self.pathway:
            prefix = ''
        else:
            prefix = 'Δ'

        data = OnTheFly(int(receptor), self.label, subtype=self.subtype, pathway=self.pathway, user=self.user, balanced=self.balanced)
        #### added code
        flat_data = {}
        for key, value in data.items():
            for row_key, data_dict in value.items():
                #filtering out non compared
                if (len(data_dict) > 33) and ('Pathway Rank' in data_dict.keys()):
                    flat_data[row_key] = data_dict
        ####
        upgrade_value = ["High activity", "High activity (Potency and Emax)", "Full agonism"]
        downgrade_value = ["Low activity", "No activity", "Inverse agonism/antagonism"]
        # exclude_list = ["Agonism", "Partial agonism", "Medium activity"]
        list_of_ligands = []
        list_of_publications = []
        full_data = {}
        full_ligands = {}
        SpiderOptions = {}
        jitterDict = {}
        jitterPlot = {}
        jitterLegend = {}
        Colors = {}
        pathway_nr = {}
        labels_dict = {}

        if self.pathway:
            delta_tk_key = 'log(Tau/KA)'
            delta_ee_key = 'log(Emax/EC50)'
            deltadelta_tk_key = 'Delta_log(Tau/KA)'
            deltadelta_ee_key = 'Delta_log(Emax/EC50)'
        else:
            delta_tk_key = 'Delta_log(Tau/KA)'
            delta_ee_key = 'Delta_log(Emax/EC50)'
            deltadelta_tk_key = 'DeltaDelta_log(Tau/KA)'
            deltadelta_ee_key = 'DeltaDelta_log(Emax/EC50)'


        for row in flat_data:
            result = flat_data[row]
            try:
                reference_ligand = result['Reference_ligand']
            except KeyError:
                reference_ligand = result['ligand_name']

            if self.pathway:
                if result['Pathway Rank'] == 'P1':
                    reference_path = tooltip_dict[result['primary_effector_family']]
                else:
                    reference_path = tooltip_dict[result['P1']]

            if self.subtype:
                reference_path = sign_prot_conversion[result['primary_effector_subtype']] #reference subtype

            if (self.pathway == False) and (self.subtype == False):
                if result['Pathway Rank'] == 'P1':
                    reference_path = tooltip_dict[result['primary_effector_family']]
                else:
                    reference_path = tooltip_dict[result['P1']]

            #checking the value to plot
            #based on the landing page
            if self.label == 'emax':
                try:
                    single_delta = result[delta_ee_key]
                except KeyError:
                    single_delta = None
                try:
                    double_delta = result[deltadelta_ee_key]
                except KeyError:
                    double_delta = None
                emax_tau = result["Emax"]
                try:
                    EC50_ka = '{:0.2e}'.format(result["EC50"])
                except TypeError:
                    EC50_ka = result["EC50"]
                EC50_sign = result['EC50_sign'] #Remember these parameters for additional info
                Emax_sign = result['Emax_sign'] #Remember these parameters for additional info
                components = ['Emax', 'EC50']
                if set(['Reference_Emax', 'Reference_EC50']).issubset(set(result.keys())):
                    reference_emax_tau = result['Reference_Emax']
                    try:
                        reference_EC50_ka = '{:0.2e}'.format(result['Reference_EC50'])
                    except TypeError:
                        reference_EC50_ka = result['Reference_EC50']
                else:
                    reference_emax_tau = 'NA'
                    reference_EC50_ka = 'NA'
            else:
                try:
                    single_delta = result[delta_tk_key]
                except KeyError:
                    single_delta = None
                try:
                    double_delta = result[deltadelta_tk_key]
                except KeyError:
                    double_delta = None
                emax_tau = "NA" #need to be updated IF datacolumn for TAU will be added
                EC50_ka = "NA"  #need to be updated IF datacolumn for KA will be added
                EC50_sign = '=' #Assign defaulted as '=' because they are not used in case of Tau/KA display
                Emax_sign = '=' #Assign defaulted as '=' because they are not used in case of Tau/KA display
                components = ['Tau', 'KA']
                reference_emax_tau = "NA" #need to be updated IF datacolumn for TAU will be added
                reference_EC50_ka = "NA"  #need to be updated IF datacolumn for KA will be added

            #fixing ligand name (hash hash baby)
            lig_name = result["ligand_name"]
            if result['ligand_name'][0].isdigit():
                lig_name = "Ligand-"+result['ligand_name']
            lig_name = lig_name.capitalize()
            hashed_lig_name = 'L' + hashlib.md5((str(result['ligand_id'])).encode('utf-8')).hexdigest()
            # replace second white space with closing and opening tspan for svg

            journal_name = result['journal']
            if result['journal']:
                if ' ' in result['journal']:
                    journal_name  = re.sub(r'(\s\S*?)\s', r'\1 closeTSI openTSI ', result['journal'])
            else:
                journal_name = "Not listed"
            if result['authors'] == None:
                authors = "Authors not listed, (" + str(result['pub_year']) + ')'
                shortAuthors = "Authors not listed, (" + str(result['pub_year']) + ')'
                jitterAuthors = 'openTS ' + shortAuthors + ' closeTS openTS ' + journal_name + ' closeTS openTS (' + str(result['pub_year']) + ') closeTS'
                labels_dict[jitterAuthors] = shortAuthors
            else:
                authors = result['authors'].split(',')[0] + ', et al., ' + str(result['journal']) + ', (' + str(result['pub_year']) + ')'
                shortAuthors = result['authors'].split(',')[0] + ', et al.'
                jitterAuthors = 'openTS ' + shortAuthors + ' closeTS openTS ' + journal_name + ' closeTS openTS (' + str(result['pub_year']) + ') closeTS'
                labels_dict[jitterAuthors] = shortAuthors

            list_of_ligands.append(tuple((hashed_lig_name, lig_name)))
            list_of_publications.append(authors)
            #start parsing the data to create the big dictionary
            if single_delta == None:               #∆Emax / ∆Tau flex
                value = 0
            else:
                value = float(single_delta)

            try:
                DD = [float(double_delta), "REAL"]
            except (ValueError, TypeError):
                DD = [0, double_delta]

            if result['primary_effector_family'] not in jitterPlot.keys():
                jitterLegend[result['primary_effector_family']] = []
                jitterPlot[result['primary_effector_family']] = []


            if jitterAuthors not in jitterDict.keys():
                jitterDict[jitterAuthors] =  {}

            if lig_name not in jitterDict[jitterAuthors].keys():
                jitterDict[jitterAuthors][lig_name] = {}

            if result['Pathway Rank'] == 'P2':
                try:
                    jitterDict[jitterAuthors][lig_name]['2nd_Pathway'] = tooltip_dict[result['primary_effector_family']]
                    jitterDict[jitterAuthors][lig_name]['2nd_Pathway_delta'] = value
                    jitterDict[jitterAuthors][lig_name]['2nd_Pathway_emax_tau'] = emax_tau
                    jitterDict[jitterAuthors][lig_name]['2nd_Pathway_EC50_KA'] = EC50_ka
                except KeyError:
                    jitterDict[jitterAuthors][lig_name]['2nd_Pathway'] = result['primary_effector_family']
                    jitterDict[jitterAuthors][lig_name]['2nd_Pathway_delta'] = value
                    jitterDict[jitterAuthors][lig_name]['2nd_Pathway_emax_tau'] = emax_tau
                    jitterDict[jitterAuthors][lig_name]['2nd_Pathway_EC50_KA'] = EC50_ka
                jitterDict[jitterAuthors][lig_name]['deltadelta'] = DD
                jitterDict[jitterAuthors][lig_name]['signalling_prot'] = result['primary_effector_subtype']
                jitterDict[jitterAuthors][lig_name]['EC50_sign'] = EC50_sign
                jitterDict[jitterAuthors][lig_name]['Emax_sign'] = Emax_sign


            if result["Pathway Rank"] == 'P1':
                jitterDict[jitterAuthors][lig_name]["Pathway"] = result['primary_effector_family']
                jitterDict[jitterAuthors][lig_name]["delta"] = value
                jitterDict[jitterAuthors][lig_name]["Emax_Tau"] = emax_tau
                jitterDict[jitterAuthors][lig_name]["EC50_KA"] = EC50_ka

            tooltip_info = BiasedSignallingOnTheFlyCalculation.jitter_tooltip(self.page, self.pathway, lig_name, value, components,
                                                          small_data=[result['primary_effector_family'], emax_tau, EC50_ka, lig_name],
                                                          small_ref=[reference_path, reference_emax_tau, reference_EC50_ka, reference_ligand])

            # initialization of the dictionary for new publication
            if result['doi'] not in full_data.keys():
                full_ligands[result['doi']] = []
                full_data[result['doi']] = {"Authors": authors,
                                            "Journal": result['journal'],
                                            "Year": result['pub_year'],
                                            "Endogenous": reference_ligand,
                                            "Ligand": [lig_name],
                                            "Qualitative": [result['qualitative_activity']],
                                            "Data": [{"name": hashed_lig_name,
                                                      "PathwaysData":
                                                            [{"Pathway": result['primary_effector_family'],
                                                              "value": [value, "REAL"],
                                                              "tooltip": tooltip_info}]}]}
                SpiderOptions[authors] = {}
                SpiderOptions[authors][hashed_lig_name] = {"Data":[[{'axis':result['primary_effector_family'],
                                                                     'value':value}]],
                                                             "Options": {'levels': 4,
                                                                         'maxValue': 4,
                                                                         'roundStrokes': False,
                                                                         'title': lig_name}}

            #new ligand pushed into existing publication
            if hashed_lig_name not in [d['name'] for d in full_data[result['doi']]["Data"]]:
                full_data[result['doi']]["Ligand"].append(lig_name)
                full_data[result['doi']]["Qualitative"].append(result['qualitative_activity'])
                full_data[result['doi']]["Data"].append(
                                            {"name": hashed_lig_name,
                                             "PathwaysData":
                                                [{"Pathway": result['primary_effector_family'],
                                                  "value": [value,"REAL"],
                                                  "tooltip": tooltip_info}]})

                SpiderOptions[authors][hashed_lig_name] = {"Data":[[{'axis':result['primary_effector_family'],
                                                                     'value':value}]],
                                                             "Options": {'levels': 4,
                                                                         'maxValue': 4,
                                                                         'roundStrokes': False,
                                                                         'title': lig_name}}
            else:
                ID = [d['name'] for d in full_data[result['doi']]["Data"]].index(hashed_lig_name)
                if result['primary_effector_family'] not in [d["Pathway"] for d in full_data[result['doi']]["Data"][ID]["PathwaysData"]]:
                    full_data[result['doi']]["Data"][ID]["PathwaysData"].append(
                                            {"Pathway": result['primary_effector_family'],
                                             "value": [value, "REAL"],
                                             "tooltip": tooltip_info})
                    SpiderOptions[authors][hashed_lig_name]["Data"][0].append({'axis':result['primary_effector_family'],
                                                                               'value':value})

        for item in full_data.keys():
            vals = []
            for name in full_data[item]["Data"]:
                to_fix = [subdict['value'][0] for subdict in name['PathwaysData']]
                vals = vals + to_fix
            MIN = min(vals) - 1
            MAX = max(vals) + 1
            for name in full_data[item]["Data"]:
                to_fix = [subdict['value'][0] for subdict in name['PathwaysData']]
                indices = [i for i, x in enumerate(to_fix) if x == 0]
                quality = full_data[item]["Qualitative"][full_data[item]["Data"].index(name)]
                if quality in upgrade_value:
                    try:
                        for i in indices:
                            name["PathwaysData"][i]["value"] = [MAX,"ARTIFICIAL"]
                            name["PathwaysData"][i]["tooltip"] = BiasedSignallingOnTheFlyCalculation.jitter_tooltip(self.page, self.pathway, lig_name, value, components,
                                                                                                small_data=[result['primary_effector_family'], emax_tau, 'High', lig_name],
                                                                                                small_ref=[reference_path, reference_emax_tau, reference_EC50_ka, reference_ligand])
                    except ValueError:
                        continue
                if quality in downgrade_value:
                    try:
                        for i in indices:
                            name["PathwaysData"][i]["value"] = [MIN,"ARTIFICIAL"]
                            name["PathwaysData"][i]["tooltip"] = BiasedSignallingOnTheFlyCalculation.jitter_tooltip(self.page, self.pathway, lig_name, value, components,
                                                                                                small_data=[result['primary_effector_family'], emax_tau, 'Low', lig_name],
                                                                                                small_ref=[reference_path, reference_emax_tau, reference_EC50_ka, reference_ligand])
                    except ValueError:
                        continue

        #The big dictionary is created, now it needs to be ordered
        #the publication with more pathway (or tied) should be first

        #From this, reorder the original one
        for key in full_data.keys():
            for item in full_data[key]["Data"]:
                if len(item['PathwaysData']) not in pathway_nr.keys():
                    pathway_nr[len(item['PathwaysData'])] = []
                pathway_nr[len(item['PathwaysData'])].append(tuple((key, len(full_data[key]["Data"]))))
        #get the number of pathways available
        keys = sorted(list(pathway_nr.keys()))[::-1]
        #starting from the highest value
        sorted_full_data = {}
        for value in keys:
            pairs = sorted(list(set(pathway_nr[value])), key=lambda x: x[1], reverse=True)
            for item in pairs:
                sorted_full_data[item[0]] = deepcopy(full_data[item[0]])
                # test_data[item[0]] = deepcopy(full_data[item[0]])
                max_values = []
                for name in full_data[item[0]]['Data']:
                    max_values.append(tuple((full_data[item[0]]['Data'].index(name), max([subdict['value'] for subdict in name['PathwaysData']]), name['name'])))
                max_values = sorted(max_values, key=lambda x: x[1], reverse=True)
                sorted_full_data[item[0]]["Data"] = [] #need to be sorted_full_data
                for couple in max_values:
                    sorted_full_data[item[0]]["Data"].append(full_data[item[0]]["Data"][couple[0]]) #need to be sorted_full_data
                    if tuple((couple[2], full_data[item[0]]["Ligand"][couple[0]])) not in full_ligands[item[0]]:
                        full_ligands[item[0]].append(tuple((couple[2], full_data[item[0]]["Ligand"][couple[0]])))
        #now the sorted dict is done, we can clear cache the og one
        del full_data

        for pub in jitterDict.keys():
            for ligand in jitterDict[pub]:
                try:
                    if ligand not in Colors.keys():
                        color = '#%02x%02x%02x' % (BiasedSignallingOnTheFlyCalculation.create_rgb_color(), BiasedSignallingOnTheFlyCalculation.create_rgb_color(), BiasedSignallingOnTheFlyCalculation.create_rgb_color())
                        Colors[ligand] = color
                    little = [reference_path, reference_emax_tau, reference_EC50_ka, reference_ligand]
                    if self.subtype:
                        big = [sign_prot_conversion[jitterDict[pub][ligand]["signalling_prot"]], jitterDict[pub][ligand]['delta'], jitterDict[pub][ligand]['Emax_Tau'], jitterDict[pub][ligand]['EC50_KA'],
                               sign_prot_conversion[jitterDict[pub][ligand]["signalling_prot"]], jitterDict[pub][ligand]['2nd_Pathway_delta'], jitterDict[pub][ligand]['2nd_Pathway_emax_tau'], jitterDict[pub][ligand]['2nd_Pathway_EC50_KA']]
                    else:
                        big = [jitterDict[pub][ligand]["Pathway"], jitterDict[pub][ligand]['delta'], jitterDict[pub][ligand]['Emax_Tau'], jitterDict[pub][ligand]['EC50_KA'],
                               jitterDict[pub][ligand]['2nd_Pathway'], jitterDict[pub][ligand]['2nd_Pathway_delta'], jitterDict[pub][ligand]['2nd_Pathway_emax_tau'], jitterDict[pub][ligand]['2nd_Pathway_EC50_KA']]
                    if (jitterDict[pub][ligand]['deltadelta'][1] == 'High Bias') or (jitterDict[pub][ligand]['deltadelta'][1] == 'Full Bias'):
                        tooltip = BiasedSignallingOnTheFlyCalculation.jitter_tooltip(self.page, self.pathway, ligand, jitterDict[pub][ligand]['deltadelta'][1], components, prefix, small_data=little, large_data=big)
                    else:
                        tooltip = BiasedSignallingOnTheFlyCalculation.jitter_tooltip(self.page, self.pathway, ligand, jitterDict[pub][ligand]['deltadelta'][0], components, prefix, small_data=little, large_data=big)
                    jitterPlot[jitterDict[pub][ligand]["Pathway"]].append([pub, jitterDict[pub][ligand]['deltadelta'][0], Colors[ligand], ligand, jitterDict[pub][ligand]['deltadelta'][1], tooltip, jitterDict[pub][ligand]['EC50_sign'], jitterDict[pub][ligand]['Emax_sign']])
                    jitterLegend[jitterDict[pub][ligand]["Pathway"]].append(tuple((ligand, jitterDict[pub][ligand]['deltadelta'][0])))
                except KeyError:
                    continue
                jitterLegend[jitterDict[pub][ligand]["Pathway"]] = sorted(list(set(jitterLegend[jitterDict[pub][ligand]["Pathway"]])), key=lambda x: x[1], reverse=True)

        #addressing qualitative points in the ∆∆ data (full bias/high bias/none)
        for pathway in jitterPlot.keys():
            highest = 0
            for datapoint in jitterPlot[pathway]:
                if datapoint[1] > highest:
                    highest = datapoint[1]
            change = {'None' : 0, "High Bias": highest + 1, "Full Bias": highest + 1.5}
            for datapoint in jitterPlot[pathway]:
                if datapoint[4] in change:
                    datapoint[1] = change[datapoint[4]]

        jitterPlot = {k: v for k, v in jitterPlot.items() if len(v) > 0}

        for key in jitterLegend.keys():
            jitterLegend[key] = list(dict.fromkeys([name[0] for name in jitterLegend[key]]))[:20]

        context['column_dict'] = json.dumps(labels_dict)
        context['pathway'] = str(self.pathway)
        context['label'] = self.label
        context['page'] = self.page
        context['scatter_legend'] = json.dumps(jitterLegend)
        context['colors'] = json.dumps(Colors)
        context['scatter'] = json.dumps(jitterPlot)
        context['spider'] = json.dumps(SpiderOptions)
        context['all_ligands'] = list(set(list_of_ligands))
        context['all_publications'] = list(set(list_of_publications))
        context['query'] = rec_name[0][0]
        context['IUPHAR'] = rec_name[0][1]
        context['full_data'] = json.dumps(sorted_full_data)
        context['full_ligands'] = json.dumps(full_ligands)
        return context

class LigandStatistics(TemplateView):
    """
    Per class statistics of known ligands.
    """

    template_name = 'ligand_statistics.html'
    page = 'ligands' #either ligand_bias or pathway_pref OR subtype (NEW PAGE)

    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)
        lig_count_dict = {}

        if self.page == 'ligands':
            assays_lig = list(AssayExperiment.objects.all().values(
                'protein__family__parent__parent__parent__name').annotate(c=Count('ligand', distinct=True)))
            for a in assays_lig:
                lig_count_dict[a['protein__family__parent__parent__parent__name']] = a['c']
        else:
            if self.page == 'ligand_bias':
                assays_lig = list(BiasedData.objects
                    .filter(physiology_biased__isnull=False)
                    .values('receptor_id__family__parent__parent__parent__name')
                    .annotate(c=Count('ligand_id', distinct=True)))
                assays_balanced_lig = list(BiasedData.objects
                    .filter(pathway_biased__isnull=False)
                    .values('receptor_id__family__parent__parent__parent__name')
                    .annotate(c=Count('ligand_id', distinct=True)))
            elif self.page == 'pathway_pref':
                assays_lig = list(BiasedData.objects
                    .filter(pathway_preferred__isnull=False)
                    .values('receptor_id__family__parent__parent__parent__name')
                    .annotate(c=Count('ligand_id', distinct=True)))
            elif self.page == 'subtype':
                assays_lig = list(BiasedData.objects
                    .filter(subtype_biased__isnull=False)
                    .values('receptor_id__family__parent__parent__parent__name')
                    .annotate(c=Count('ligand_id', distinct=True)))
                assays_balanced_lig = list(BiasedData.objects
                    .filter(pathway_subtype_biased__isnull=False)
                    .values('receptor_id__family__parent__parent__parent__name')
                    .annotate(c=Count('ligand_id', distinct=True)))
            for a in assays_lig:
                lig_count_dict[a['receptor_id__family__parent__parent__parent__name']] = a['c']

        target_count_dict = {}

        assays_target = list(AssayExperiment.objects.all().values(
            'protein__family__parent__parent__parent__name').annotate(c=Count('protein__family', distinct=True)))

        for a in assays_target:
            target_count_dict[a['protein__family__parent__parent__parent__name']] = a['c']

        prot_count_dict = {}

        proteins_count = list(Protein.objects.all().values(
            'family__parent__parent__parent__name').annotate(c=Count('family', distinct=True)))

        for pf in proteins_count:
            prot_count_dict[pf['family__parent__parent__parent__name']] = pf['c']

        if self.page == 'ligands':
            classes = ProteinFamily.objects.filter(
                slug__in=['001', '002', '003', '004', '005', '006', '007'])  # ugly but fast
        else:
            classes = ProteinFamily.objects.filter(
                slug__in=['001', '002', '003', '004', '006', '007'])  # ugly but fast

        ligands = []

        for fam in classes:
            if fam.name in lig_count_dict:
                lig_count = lig_count_dict[fam.name]
                target_count = target_count_dict[fam.name]
            else:
                lig_count = 0
                target_count = 0
            prot_count = prot_count_dict[fam.name]
            ligands.append({
                'name': fam.name.replace('Class', ''),
                'num_ligands': lig_count,
                'avg_num_ligands': lig_count / prot_count,
                'target_percentage': target_count / prot_count * 100,
                'target_count': target_count
            })

        lig_count_total = sum([x['num_ligands'] for x in ligands])

        prot_count_total = Protein.objects.filter(
            family__slug__startswith='00').all().distinct('family').count()

        target_count_total = sum([x['target_count'] for x in ligands])

        lig_total = {
            'num_ligands': lig_count_total,
            'avg_num_ligands': lig_count_total / prot_count_total,
            'target_percentage': target_count_total / prot_count_total * 100,
            'target_count': target_count_total
        }

        context['ligands_total'] = lig_total
        context['ligands_by_class'] = ligands
        context['release_notes'] = ReleaseNotes.objects.all()[0]

        tree = PhylogeneticTreeGenerator()
        class_a_data = tree.get_tree_data(ProteinFamily.objects.get(name='Class A (Rhodopsin)'))
        context['class_a_options'] = deepcopy(tree.d3_options)
        context['class_a_options']['anchor'] = 'class_a'
        context['class_a_options']['leaf_offset'] = 50
        context['class_a_options']['label_free'] = []
        # section to remove Orphan from Class A tree and apply to a different tree
        whole_class_a = class_a_data.get_nodes_dict(self.page)
        for item in whole_class_a['children']:
            if item['name'] == 'Orphan':
                orphan_data = OrderedDict(
                    [('name', ''), ('value', 3000), ('color', ''), ('children', [item])])
                whole_class_a['children'].remove(item)
                break
        context['class_a'] = json.dumps(whole_class_a)
        class_b1_data = tree.get_tree_data(
            ProteinFamily.objects.get(name__startswith='Class B1 (Secretin)'))
        context['class_b1_options'] = deepcopy(tree.d3_options)
        context['class_b1_options']['anchor'] = 'class_b1'
        context['class_b1_options']['branch_trunc'] = 60
        context['class_b1_options']['label_free'] = [1, ]
        context['class_b1'] = json.dumps(
            class_b1_data.get_nodes_dict(self.page))
        class_b2_data = tree.get_tree_data(
            ProteinFamily.objects.get(name__startswith='Class B2 (Adhesion)'))
        context['class_b2_options'] = deepcopy(tree.d3_options)
        context['class_b2_options']['anchor'] = 'class_b2'
        context['class_b2_options']['label_free'] = [1, ]
        context['class_b2'] = json.dumps(
            class_b2_data.get_nodes_dict(self.page))
        class_c_data = tree.get_tree_data(
            ProteinFamily.objects.get(name__startswith='Class C (Glutamate)'))
        context['class_c_options'] = deepcopy(tree.d3_options)
        context['class_c_options']['anchor'] = 'class_c'
        context['class_c_options']['branch_trunc'] = 50
        context['class_c_options']['label_free'] = [1, ]
        context['class_c'] = json.dumps(class_c_data.get_nodes_dict(self.page))
        class_f_data = tree.get_tree_data(
            ProteinFamily.objects.get(name__startswith='Class F (Frizzled)'))
        context['class_f_options'] = deepcopy(tree.d3_options)
        context['class_f_options']['anchor'] = 'class_f'
        context['class_f_options']['label_free'] = [1, ]
        context['class_f'] = json.dumps(class_f_data.get_nodes_dict(self.page))
        class_t2_data = tree.get_tree_data(
            ProteinFamily.objects.get(name__startswith='Class T (Taste 2)'))
        context['class_t2_options'] = deepcopy(tree.d3_options)
        context['class_t2_options']['anchor'] = 'class_t2'
        context['class_t2_options']['label_free'] = [1, ]
        context['class_t2'] = json.dumps(
            class_t2_data.get_nodes_dict(self.page))
        # definition of the class a orphan tree
        context['orphan_options'] = deepcopy(tree.d3_options)
        context['orphan_options']['anchor'] = 'orphan'
        context['orphan_options']['label_free'] = [1, ]
        context['orphan'] = json.dumps(orphan_data)

        whole_receptors = Protein.objects.prefetch_related(
            "family", "family__parent__parent__parent")
        whole_rec_dict = {}
        for rec in whole_receptors:
            rec_uniprot = rec.entry_short()
            rec_iuphar = rec.family.name.replace("receptor", '').replace(
                "<i>", "").replace("</i>", "").strip()
            if (rec_iuphar[0].isupper()) or (rec_iuphar[0].isdigit()):
                whole_rec_dict[rec_uniprot] = [rec_iuphar]
            else:
                whole_rec_dict[rec_uniprot] = [rec_iuphar.capitalize()]

        context["whole_receptors"] = json.dumps(whole_rec_dict)

        #Adding section for addressing the data for tree against balanced reference ONLY for ligand_bias page
        if self.page == 'ligand_bias':
            tree = PhylogeneticTreeGenerator()
            class_a_data_bal = tree.get_tree_data(ProteinFamily.objects.get(name='Class A (Rhodopsin)'))
            context['class_a_options_bal'] = deepcopy(tree.d3_options)
            context['class_a_options_bal']['anchor'] = 'class_a_bal'
            context['class_a_options_bal']['leaf_offset'] = 50
            context['class_a_options_bal']['label_free'] = []
            # section to remove Orphan from Class A tree and apply to a different tree
            whole_class_a_bal = class_a_data_bal.get_nodes_dict(self.page+'_bal')
            for item in whole_class_a_bal['children']:
                if item['name'] == 'Orphan':
                    orphan_data_bal = OrderedDict(
                        [('name', ''), ('value', 3000), ('color', ''), ('children', [item])])
                    whole_class_a_bal['children'].remove(item)
                    break
            context['class_a_bal'] = json.dumps(whole_class_a_bal)
            class_b1_data_bal = tree.get_tree_data(
                ProteinFamily.objects.get(name__startswith='Class B1 (Secretin)'))
            context['class_b1_options_bal'] = deepcopy(tree.d3_options)
            context['class_b1_options_bal']['anchor'] = 'class_b1_bal'
            context['class_b1_options_bal']['branch_trunc'] = 60
            context['class_b1_options_bal']['label_free'] = [1, ]
            context['class_b1_bal'] = json.dumps(
                class_b1_data_bal.get_nodes_dict(self.page+'_bal'))
            class_b2_data_bal = tree.get_tree_data(
                ProteinFamily.objects.get(name__startswith='Class B2 (Adhesion)'))
            context['class_b2_options_bal'] = deepcopy(tree.d3_options)
            context['class_b2_options_bal']['anchor'] = 'class_b2_bal'
            context['class_b2_options_bal']['label_free'] = [1, ]
            context['class_b2_bal'] = json.dumps(
                class_b2_data_bal.get_nodes_dict(self.page+"_bal"))
            class_c_data_bal = tree.get_tree_data(
                ProteinFamily.objects.get(name__startswith='Class C (Glutamate)'))
            context['class_c_options_bal'] = deepcopy(tree.d3_options)
            context['class_c_options_bal']['anchor'] = 'class_c_bal'
            context['class_c_options_bal']['branch_trunc'] = 50
            context['class_c_options_bal']['label_free'] = [1, ]
            context['class_c_bal'] = json.dumps(class_c_data_bal.get_nodes_dict(self.page+"_bal"))
            class_f_data_bal = tree.get_tree_data(
                ProteinFamily.objects.get(name__startswith='Class F (Frizzled)'))
            context['class_f_options_bal'] = deepcopy(tree.d3_options)
            context['class_f_options_bal']['anchor'] = 'class_f_bal'
            context['class_f_options_bal']['label_free'] = [1, ]
            context['class_f_bal'] = json.dumps(class_f_data_bal.get_nodes_dict(self.page+"_bal"))
            class_t2_data_bal = tree.get_tree_data(
                ProteinFamily.objects.get(name__startswith='Class T (Taste 2)'))
            context['class_t2_options_bal'] = deepcopy(tree.d3_options)
            context['class_t2_options_bal']['anchor'] = 'class_t2_bal'
            context['class_t2_options_bal']['label_free'] = [1, ]
            context['class_t2_bal'] = json.dumps(
                class_t2_data_bal.get_nodes_dict(self.page+"_bal"))
            # definition of the class a orphan tree
            context['orphan_options_bal'] = deepcopy(tree.d3_options)
            context['orphan_options_bal']['anchor'] = 'orphan_bal'
            context['orphan_options_bal']['label_free'] = [1, ]
            context['orphan_bal'] = json.dumps(orphan_data_bal)

        ##### END COPIED SECTION #####

        if self.page == 'ligands':
            context["render"] = "not_bias"

        else:
            if self.page == 'ligand_bias':
                context["render"] = "bias"
                circle_data = BiasedData.objects.filter(physiology_biased__isnull=False).values_list(
                              "physiology_biased", "receptor_id__entry_name", "ligand_id").order_by(
                              "physiology_biased", "receptor_id__entry_name", "ligand_id").distinct(
                              "physiology_biased", "receptor_id__entry_name", "ligand_id")
                circle_data_bal = BiasedData.objects.filter(pathway_biased__isnull=False).values_list(
                              "pathway_biased", "receptor_id__entry_name", "ligand_id").order_by(
                              "pathway_biased", "receptor_id__entry_name", "ligand_id").distinct(
                              "pathway_biased", "receptor_id__entry_name", "ligand_id")
            elif self.page == 'pathway_pref':
                context["render"] = "pathway"
                circle_data = BiasedData.objects.filter(pathway_preferred__isnull=False).values_list(
                              "pathway_preferred", "receptor_id__entry_name", "ligand_id").order_by(
                              "pathway_preferred", "receptor_id__entry_name", "ligand_id").distinct(
                              "pathway_preferred", "receptor_id__entry_name", "ligand_id")
            elif self.page == 'subtype':
                context["render"] = "subtype"
                circle_data = BiasedData.objects.filter(subtype_biased__isnull=False).values_list(
                              "subtype_biased", "receptor_id__entry_name", "ligand_id").order_by(
                              "subtype_biased", "receptor_id__entry_name", "ligand_id").distinct(
                              "subtype_biased", "receptor_id__entry_name", "ligand_id")
                circle_data_bal = BiasedData.objects.filter(pathway_subtype_biased__isnull=False).values_list(
                              "pathway_subtype_biased", "receptor_id__entry_name", "ligand_id").order_by(
                              "pathway_subtype_biased", "receptor_id__entry_name", "ligand_id").distinct(
                              "pathway_subtype_biased", "receptor_id__entry_name", "ligand_id")

            circles = {}
            label_converter = {'Arrestin-2': "β-Arr",
                               'Arrestin-3': "β-Arr 2",
                               'Gaq/i-chimera': "Gq/i-chim",
                               'Minigi': "Mini-Gi"}
            endpoint = 0
            for data in circle_data:
                # if data[1].split('_')[1] == 'human':
                key = data[1].split('_')[0].upper()
                val = data[0].split(' (')[0].capitalize()
                if val in label_converter.keys():
                    val = label_converter[val]
                if key not in circles.keys():
                    circles[key] = {}
                if val not in circles[key].keys():
                    circles[key][val] = 1
                circles[key][val] += 1
                if circles[key][val] > endpoint:
                    endpoint = circles[key][val]

            context["circles_data"] = json.dumps(circles)
            context["endpoint"] = endpoint
            #Addressing section for Balanced Reference ligands and pathway biased
            if self.page in ['subtype', 'ligand_bias']:
                bal_ligands = []
                bal_lig_count_dict = {}

                for a in assays_balanced_lig:
                    bal_lig_count_dict[a['receptor_id__family__parent__parent__parent__name']] = a['c']

                for fam in classes:
                    if fam.name in bal_lig_count_dict:
                        lig_count = bal_lig_count_dict[fam.name]
                        target_count = target_count_dict[fam.name]
                    else:
                        lig_count = 0
                        target_count = 0
                    prot_count = prot_count_dict[fam.name]
                    bal_ligands.append({
                        'name': fam.name.replace('Class', ''),
                        'num_ligands': lig_count,
                        'avg_num_ligands': lig_count / prot_count,
                        'target_percentage': target_count / prot_count * 100,
                        'target_count': target_count
                    })

                bal_lig_count_total = sum([x['num_ligands'] for x in bal_ligands])

                target_count_total = sum([x['target_count'] for x in bal_ligands])

                bal_lig_total = {
                    'num_ligands': bal_lig_count_total,
                    'avg_num_ligands': bal_lig_count_total / prot_count_total,
                    'target_percentage': target_count_total / prot_count_total * 100,
                    'target_count': target_count_total
                }

                context['bal_ligands_total'] = bal_lig_total
                context['bal_ligands_by_class'] = bal_ligands

                circles_bal = {}
                endpoint_bal = 0
                # print(circle_data_bal)
                for data in circle_data_bal:
                    # if data[1].split('_')[1] == 'human':
                    key = data[1].split('_')[0].upper()
                    val = data[0].split(' (')[0].capitalize()
                    if val in label_converter.keys():
                        val = label_converter[val]
                    if key not in circles_bal.keys():
                        circles_bal[key] = {}
                    if val not in circles_bal[key].keys():
                        circles_bal[key][val] = 1
                    circles_bal[key][val] += 1
                    if circles_bal[key][val] > endpoint_bal:
                        endpoint_bal = circles_bal[key][val]

                context["circles_bal_data"] = json.dumps(circles_bal)
                context["endpoint_bal"] = endpoint_bal

                #Adding options and data for pathway biased plots to context
                whole_class_a_bal = class_a_data.get_nodes_dict(self.page+'_bal')
                HeatMapData_bal = whole_class_a_bal
                HeatMapData_bal = OrderedDict([('Class A', v) if k == 'children' else (k, v) for k, v in HeatMapData_bal.items()])
                HeatMapData_bal['Class B1'] = class_b1_data.get_nodes_dict(self.page+'_bal')['children']
                HeatMapData_bal['Class B2'] = class_b2_data.get_nodes_dict(self.page+'_bal')['children']
                HeatMapData_bal['Class C'] = class_c_data.get_nodes_dict(self.page+'_bal')['children']
                HeatMapData_bal['Class F'] = class_f_data.get_nodes_dict(self.page+'_bal')['children']
                HeatMapData_bal['Class T2'] = class_t2_data.get_nodes_dict(self.page+'_bal')['children']
                context['HeatMapData_bal'] = json.dumps(HeatMapData_bal)


            HeatMapData = whole_class_a
            HeatMapData = OrderedDict([('Class A', v) if k == 'children' else (k, v) for k, v in HeatMapData.items()])
            HeatMapData['Class B1'] = class_b1_data.get_nodes_dict(self.page)['children']
            HeatMapData['Class B2'] = class_b2_data.get_nodes_dict(self.page)['children']
            HeatMapData['Class C'] = class_c_data.get_nodes_dict(self.page)['children']
            HeatMapData['Class F'] = class_f_data.get_nodes_dict(self.page)['children']
            HeatMapData['Class T2'] = class_t2_data.get_nodes_dict(self.page)['children']
            context['HeatMapData'] = json.dumps(HeatMapData)

            #######################################################################################
            ##################### Setting up data for the Heatmap calculation #####################
            CSS_COLORS = {
                "Adhesion receptors": 'Crimson',
                "Alicarboxylic acid receptors": 'Red',
                "Aminergic receptors": 'OrangeRed',
                "Amino acid receptors": 'Orange',
                "Ion receptors": 'GoldenRod',
                "Lipid receptors": 'Gold',
                "Melatonin receptors": 'Yellow',
                "Nucleotide receptors": 'YellowGreen',
                "Orphan receptors": 'Gold',
                "Other": 'Green',
                "Olfactory receptors": 'Black',
                "Peptide receptors": 'SkyBlue',
                "Protein receptors": 'SteelBlue',
                "Sensory receptors": 'Indigo',
                "Steroid receptors": 'Purple',
                "Class B2 (Adhesion)": 'LawnGreen',
                "Class B1 (Secretin)": 'MediumBlue',
                "Class C (Glutamate)": 'DarkGoldenRod',
                "Class C Orphans": 'DarkGoldenRod',
                "Class A orphans": 'DarkSalmon',
                "Class A (Rhodopsin)": 'Violet',
                "Class F (Frizzled)": 'Teal',
                "Other GPCR orphans": "Grey",
                "Class T (Taste 2)": 'MediumPurple',
                }
            heatmap_receptors = Protein.objects.filter(family__slug__startswith='00', species_id=1).exclude(
                                              family__slug__startswith='005').prefetch_related(
                                              "family", "family__parent", "family__parent__parent", "family__parent__parent__parent")
            MasterDict = {}
            color_cache = {}
            for rec in heatmap_receptors:
                if 'CONSENSUS' in rec.entry_short():
                    continue
                if (rec.entry_short()[0].isdigit()) and (rec.entry_short()[0] != '5'):
                    continue
                if rec.family.name.startswith('Class') or rec.family.name.startswith('Other'):
                    continue
                if rec.family.parent.name.startswith('Class'):
                    class_name = rec.family.parent.name.split(' (')[0]
                    class_color = CSS_COLORS[rec.family.parent.name]
                    lig_type_color = "NA"
                    lig_type_name = "NA"
                    rec_family_color = "NA"
                    rec_family_name = "NA"
                if rec.family.parent.parent.name.startswith('Class') or rec.family.parent.parent.name.startswith('Orphan'):
                    class_name = rec.family.parent.parent.name.split(' (')[0]
                    class_color = CSS_COLORS[rec.family.parent.parent.name]
                    lig_type_color = CSS_COLORS[rec.family.parent.name]
                    lig_type_name = rec.family.parent.name
                    if rec.family.name not in color_cache:
                        color_cache[rec.family.name] = '#%02x%02x%02x' % (BiasedSignallingOnTheFlyCalculation.create_rgb_color(), BiasedSignallingOnTheFlyCalculation.create_rgb_color(), BiasedSignallingOnTheFlyCalculation.create_rgb_color())
                    rec_family_color = color_cache[rec.family.name]
                    rec_family_name = rec.family.name
                if rec.family.parent.parent.parent.name.startswith('Class'):
                    class_name = rec.family.parent.parent.parent.name.split(' (')[0]
                    class_color = CSS_COLORS[rec.family.parent.parent.parent.name]
                    lig_type_color = CSS_COLORS[rec.family.parent.parent.name]
                    lig_type_name = rec.family.parent.parent.name
                    if rec.family.parent.name not in color_cache:
                        color_cache[rec.family.parent.name] = '#%02x%02x%02x' % (BiasedSignallingOnTheFlyCalculation.create_rgb_color(), BiasedSignallingOnTheFlyCalculation.create_rgb_color(), BiasedSignallingOnTheFlyCalculation.create_rgb_color())
                    rec_family_color = color_cache[rec.family.parent.name]
                    rec_family_name = rec.family.parent.name

                rec_uniprot = rec.entry_short()
                rec_iuphar_long = "<tspan>" + rec.family.name.capitalize().replace("<sub>", '</tspan><tspan baseline-shift = "sub">').replace("</sub>", '</tspan><tspan>').replace("<i>", '</tspan><tspan font-style = "italic">').replace("</i>", '</tspan><tspan>').strip() + "</tspan>"
                rec_iuphar_short = rec_iuphar_long.replace("-adrenoceptor", '').replace("receptor", '').replace("Long-wave-sensitive",'LWS').replace("Medium-wave-sensitive",'MWS').replace("Short-wave-sensitive",'SWS').replace("Olfactory", 'OLF').replace("Calcitonin -like", 'CLR').strip()

                MasterDict[rec_uniprot] = {'UniProt': rec_uniprot,
                                           'IUPHAR Short': rec_iuphar_short,
                                           'IUPHAR Long': rec_iuphar_long,
                                           'Receptor Family': rec_family_color,
                                           'Ligand Type': lig_type_color,
                                           'Class': class_color,
                                           'Class Name': class_name,
                                           'Ligand Type Name': lig_type_name,
                                           'Receptor Family Name': rec_family_name}
                context['MasterDict'] = json.dumps(MasterDict)

        return context

# Biased pathways part


class PathwayExperimentEntryView(DetailView):
    context_object_name = 'experiment'
    model = BiasedPathways
    template_name = 'biased_pathways_data.html'


@csrf_exempt
def test_link(request):
    request.session['ids'] = ''
    if request.POST.get('action') == 'post':
        request.session.modified = True
        data = request.POST.get('ids')
        data = filter(lambda char: char not in " \"?.!/;:[]", data)
        datum = "".join(data)
        request.session['ids'] = datum

    return HttpResponse(request)


class BiasVendorBrowser(TemplateView):

    template_name = 'biased_ligand_vendor.html'

    def get_context_data(self, **kwargs):
        # try:
        context = dict()
        datum = self.request.session.get('ids')
        rd = list()
        for i in datum.split(','):
            ligand = Ligand.objects.get(id=i)
            links = LigandVendorLink.objects.filter(ligand=ligand).exclude(vendor__name__in=['ZINC', 'ChEMBL', 'BindingDB', 'SureChEMBL', 'eMolecules', 'MolPort', 'PubChem'])
            for x in links:
                temp = {}
                temp['ligand'] = ligand
                temp['url'] = x.url
                temp['vendor_id'] = x.external_id
                temp['vendor'] = x.vendor
                rd.append(temp)

        context['data'] = rd

        return context

class LigandInformationView(TemplateView):
    template_name = 'ligand_info.html'

    def get_context_data(self, *args, **kwargs):
        context = super(LigandInformationView, self).get_context_data(**kwargs)
        ligand_id = self.kwargs['pk']
        ligand_data = Ligand.objects.get(id=ligand_id)
        endogenous_ligands =  Endogenous_GTP.objects.all().values_list("ligand_id", flat=True)
        assay_data = list(AssayExperiment.objects.filter(ligand=ligand_id).prefetch_related(
            'ligand', 'protein', 'protein__family',
            'protein__family__parent', 'protein__family__parent__parent__parent',
            'protein__family__parent__parent', 'protein__family', 'protein__species'))
        context = dict()
        structures = LigandInformationView.get_structure(ligand_data)
        ligand_data = LigandInformationView.process_ligand(ligand_data, endogenous_ligands)
        assay_data = LigandInformationView.process_assay(assay_data)
        mutations = LigandInformationView.get_mutations(ligand_data)
        # if int(ligand_id) in endogenous_ligands:
        #     endo_data = list(Endogenous_GTP.objects.filter(ligand=ligand_id).prefetch_related(
        #     'ligand', 'receptor', 'receptor__family',
        #     'receptor__family__parent', 'receptor__family__parent__parent__parent',
        #     'receptor__family__parent__parent', 'receptor__species'))
        #     endo_values = LigandInformationView.process_endo(endo_data)
        #     assay_data = assay_data + endo_values
        context.update({'structure': structures})
        context.update({'ligand': ligand_data})
        context.update({'assay': assay_data})
        context.update({'mutations': mutations})
        return context

    @staticmethod
    def get_structure(ligand):
        return_list = list()
        structures = list(
            StructureLigandInteraction.objects.filter(ligand=ligand))
        for i in structures:
            structure_dict = dict()
            structure_dict['structure_pdb'] = i.structure.pdb_code.index
            return_list.append(structure_dict)
        return return_list

    @staticmethod
    def get_mutations(ligand):
        return_set = set()
        return_list = list()
        mutations = list(
            MutationExperiment.objects.filter(ligand_id=ligand['ligand_id']).only('protein').order_by("protein__name"))
        for i in mutations:
            if i.protein.family_id in return_set:
                pass
            else:
                return_list.append({"id":i.protein.family_id, "name": i.protein.short()})
                return_set.add(i.protein.family_id)

        return return_list

    @staticmethod
    def process_assay(assays):
        return_dict = dict()
        for i in assays:
            name = str(i.protein) + '_' + str(i.source)
            assay_type = i.value_type
            if i.source == 'Guide to Pharmacology':
                data_value = i.p_activity_ranges
            else:
                data_value = float(i.p_activity_value)

            if name in return_dict:
                if assay_type in return_dict[name]['data_type'].keys():
                    return_dict[name]['data_type'][assay_type].append(data_value)
                else:
                    return_dict[name]['data_type'][assay_type] = [data_value]
            else:
                return_dict[name] = dict()
                return_dict[name]['data_type'] = dict()
                return_dict[name]['data_type'][assay_type] = [data_value]
                return_dict[name]['receptor_gtp'] = i.protein.short()
                return_dict[name]['receptor_uniprot'] = i.protein.entry_short()
                return_dict[name]['receptor_species'] = i.protein.species.common_name
                return_dict[name]['receptor_family'] = i.protein.family.parent.short()
                return_dict[name]['receptor_class'] = i.protein.family.parent.parent.parent.shorter()
                return_dict[name]['source'] = i.source

        for item in return_dict.keys():
            for assay_type in return_dict[item]['data_type'].keys():
                if return_dict[item]['source'] == 'Guide to Pharmacology':
                    return_dict[item]['data_type'][assay_type] = LigandInformationView.return_splitted_ranges(return_dict[item]['data_type'][assay_type])
                else:
                    return_dict[item]['data_type'][assay_type] = LigandInformationView.get_min_max_values(return_dict[item]['data_type'][assay_type])
    	#Unpacking
        unpacked = dict()
        for key in return_dict.keys():
            for data_type in return_dict[key]['data_type'].keys():
                label = '_'.join([key,data_type])
                unpacked[label] = deepcopy(return_dict[key])
                unpacked[label]['type'] = data_type if data_type.startswith('p') or data_type.startswith('P') or data_type == '-' else 'p'+data_type
                unpacked[label]['min'] = return_dict[key]['data_type'][data_type][0]
                unpacked[label]['avg'] = return_dict[key]['data_type'][data_type][1]
                unpacked[label]['max'] = return_dict[key]['data_type'][data_type][2]
                unpacked[label]['source'] = return_dict[key]['source']
                unpacked[label].pop('data_type', None)

        return list(unpacked.values())

    @staticmethod
    def return_splitted_ranges(value):
        if len(value) == 1:
            split_value = value[0].split('|')
            try: #check if it is not a None
                avg = float(split_value[1])
            except ValueError: #if it's None default to '-'
                avg = '-'
            try: #check if it is not a None
                minimum = round(float(split_value[0]), 2)
            except ValueError: #if it's None default to '-'
                minimum = avg

            try: #check if it is not a None
                maximum = float(split_value[2])
            except ValueError:#if it's None default to 0.00
                maximum = avg

            if avg == 0.00:
                avg = (minimum + maximum)/2

            if avg != "-":
                output = [minimum, round(avg, 2), round(maximum, 2)]
            else:
                output = ['-', '-', '-']
        else:
            minimum = []
            average = []
            maximum = []
            for record in value:
                split_record = record.split('|')
                if split_record[0] != 'None':
                    minimum.append(float(record.split('|')[0]))
                if split_record[1] != 'None':
                    average.append(float(record.split('|')[1]))
                if split_record[2] != 'None':
                    maximum.append(float(record.split('|')[2]))
            try:
                minimum = round(min(minimum), 2)
            except ValueError:
                minimum = '-'

            if len(average) > 0:
                avg = sum(average) / len(average)
            else:
                avg = "-"

            try:
                maximum = max(maximum)
            except ValueError:
                maximum = avg

            if minimum == '-':
                minimum = avg

            if avg != "-":
                output = [minimum, round(avg, 2), round(maximum, 2)]
            else:
                output = ['-', '-', '-']
        return output

    @staticmethod
    def get_min_max_values(value):
        maximum = max(value)
        minimum = min(value)
        avg = sum(value) / len(value)
        output = [round(minimum, 2), round(avg, 2), round(maximum, 2)]
        return output

    @staticmethod
    def process_ligand(ligand_data, endogenous_ligands):
        img_setup_smiles = "<img style=\"height: 80%; width: 80%;;\" src=\"https://cactus.nci.nih.gov/chemical/structure/{}/image\">"
        ld = dict()
        ld['ligand_id'] = ligand_data.id
        ld['ligand_name'] = ligand_data.name
        ld['ligand_smiles'] = ligand_data.smiles
        ld['ligand_inchikey'] = ligand_data.inchikey
        try:
            ld['type'] = ligand_data.ligand_type.name.replace('-',' ').capitalize()
        except:
            ld['type'] = None
        ld['rotatable'] = ligand_data.rotatable_bonds
        ld['sequence'] = ligand_data.sequence
        ld['hacc'] = ligand_data.hacc
        ld['hdon'] = ligand_data.hdon
        ld['logp'] = ligand_data.logp
        ld['mw'] = ligand_data.mw
        ld['labels'] = LigandInformationView.get_labels(ligand_data, endogenous_ligands, ld['type'])
        ld['wl'] = list()

        if ligand_data.smiles is not None and (ld['mw'] is None or ld['mw'] < 800):
            ld['picture'] = img_setup_smiles.format(urllib.parse.quote(ligand_data.smiles))
        else:
            # "No image available" SVG (source: https://commons.wikimedia.org/wiki/File:No_image_available.svg)
            ld['picture'] = "<img style=\"max-height: 300px; max-width: 400px;\" src=\"data:image/svg+xml;base64,PD94bWwgdmVyc2lvbj0iMS4wIj8+CjxzdmcgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIgogICAgIGhlaWdodD0iMzAwcHgiIHdpZHRoPSIzMDBweCIKICAgICB2ZXJzaW9uPSIxLjEiCiAgICAgdmlld0JveD0iLTMwMCAtMzAwIDYwMCA2MDAiCiAgICAgZm9udC1mYW1pbHk9IkJpdHN0cmVhbSBWZXJhIFNhbnMsTGliZXJhdGlvbiBTYW5zLCBBcmlhbCwgc2Fucy1zZXJpZiIKICAgICBmb250LXNpemU9IjcyIgogICAgIHRleHQtYW5jaG9yPSJtaWRkbGUiID4KICAKICA8Y2lyY2xlIHN0cm9rZT0iI0FBQSIgc3Ryb2tlLXdpZHRoPSIxMCIgcj0iMjgwIiBmaWxsPSIjRkZGIi8+CiAgPHRleHQgc3R5bGU9ImZpbGw6IzQ0NDsiPgogICAgPHRzcGFuIHg9IjAiIHk9Ii04Ij5OTyBJTUFHRTwvdHNwYW4+PHRzcGFuIHg9IjAiIHk9IjgwIj5BVkFJTEFCTEU8L3RzcGFuPgogIDwvdGV4dD4KPC9zdmc+==\">"
        #Sorting links if ligand is endogenous
        if ligand_data.id in endogenous_ligands:
            sorted_list = ['Guide To Pharmacology', 'DrugBank', 'Drug Central', 'ChEMBL_compound_ids', 'PubChem']
            to_be_sorted = {}
            for i in ligand_data.ids.all():
                to_be_sorted[i.web_resource.name] = {'name': i.web_resource.name, "link": str(i)}
            tmp = sorted(to_be_sorted.items(), key=lambda pair: sorted_list.index(pair[0]))
            for i in tmp:
                ld['wl'].append(i[1])
        else:
            for i in ligand_data.ids.all():
                ld['wl'].append({'name': i.web_resource.name, "link": str(i)})
        return ld

    # @staticmethod
    # def process_endo(endo_data):
    #     return_dict = dict()
    #     for i in endo_data:
    #         name = str(i.receptor)
    #         return_dict[name] = dict()
    #         return_dict[name]['data_type'] = dict()
    #         return_dict[name]['data_type']['pEC50'] = i.pec50.split(' | ')
    #         return_dict[name]['data_type']['pIC50'] = i.pic50.split(' | ')
    #         return_dict[name]['data_type']['pKi'] = i.pKi.split(' | ')
    #         return_dict[name]['receptor_gtp'] = i.receptor.short()
    #         return_dict[name]['receptor_uniprot'] = i.receptor.entry_short()
    #         return_dict[name]['receptor_species'] = i.receptor.species.common_name
    #         return_dict[name]['receptor_family'] = i.receptor.family.parent.short()
    #         return_dict[name]['receptor_class'] = i.receptor.family.parent.parent.parent.short()
    #
    # 	#Unpacking
    #     unpacked = dict()
    #     for key in return_dict.keys():
    #         for data_type in return_dict[key]['data_type'].keys():
    #             label = '_'.join([key,data_type])
    #             unpacked[label] = deepcopy(return_dict[key])
    #             unpacked[label]['type'] = data_type
    #             unpacked[label]['min'] = float(return_dict[key]['data_type'][data_type][0]) if return_dict[key]['data_type'][data_type][0] != 'None' else ''
    #             unpacked[label]['avg'] = float(return_dict[key]['data_type'][data_type][1]) if return_dict[key]['data_type'][data_type][1] != 'None' else ''
    #             unpacked[label]['max'] = float(return_dict[key]['data_type'][data_type][2]) if return_dict[key]['data_type'][data_type][2] != 'None' else ''
    #             unpacked[label]['source'] = 'Guide to Pharmacology'
    #             unpacked[label].pop('data_type', None)
    #
    #     return list(unpacked.values())

    @staticmethod
    def get_labels(ligand_data, endogenous_ligands, label_type):
        endogenous_label = '<img src="https://icon-library.com/images/icon-e/icon-e-17.jpg" title="Endogenous ligand from GtoP" width="20" height="20"></img>'
        surrogate_label = '<img src="https://icon-library.com/images/letter-s-icon/letter-s-icon-15.jpg"' + \
                          ' title="Surrogate ligand" width="20" height="20"></img>'
        drug_label = '<img src="https://icon-library.com/images/drugs-icon/drugs-icon-7.jpg" title="Approved drug" width="20" height="20"></img>'
        #trial_label = '<img src="https://icon-library.com/images/clinical-trial-icon-2793430_960_720_7492.png" title="Drug in clinical trial" width="20" height="20"></img>'
        small_molecule_label = '<img src="https://icon-library.com/images/282dfa029c.png" title="Small molecule" width="20" height="20"></img>'
        peptide_label = '<img src="https://media.istockphoto.com/vectors/protein-structure-molecule-3d-icon-vector' + \
                        '-id1301952426?k=20&m=1301952426&s=612x612&w=0&h=a3ik50-faiP2BqiB7wMP3s_rVZyzPl9yHNQy7Rg89aE=" ' + \
                        'title="Peptide" width="20" height="20"></img>'
        antibody_label = '<img src="https://icon-library.com/images/2018/2090572_antibody-antibody-hd-png-download.png" ' + \
                         'title="Antibody" width="20" height="20"></img>'
        label = ''
        #creating a parallel dict to keep info for labelings
        #but also addressing info in new table structure of ligand info page
        label_dict = {}
        #Endogenous OR Surrogate
        if ligand_data.id in endogenous_ligands:
            label += endogenous_label
            label_dict['endogenous'] = 'Yes'
        else:
            label += surrogate_label
            label_dict['endogenous'] = 'No'
        #Drug or Trial
        sources = [i.web_resource.name for i in ligand_data.ids.all()]
        drug_banks = ['DrugBank', 'Drug Central']
        if any(value in sources for value in drug_banks):
            label += drug_label
            label_dict['approved'] = 'Yes'
        else:
            label_dict['approved'] = 'No'
        #Small molecule, Peptide or Antibody
        if label_type == 'Small molecule':
            label += small_molecule_label
        elif label_type == 'Peptide':
            label += peptide_label
        elif label_type == 'Antibody':
            label += antibody_label
        #returning the dict for sake of table structure
        return label_dict

class BiasPathways(TemplateView):
    template_name = 'bias_browser_pathways.html'
    # @cache_page(50000)
    def get_context_data(self, *args, **kwargs):
        content = BiasedPathways.objects.all().prefetch_related(
            'biased_pathway', 'ligand', 'receptor', 'receptor', 'receptor__family',
            'receptor__family__parent', 'receptor__family__parent__parent__parent',
            'receptor__family__parent__parent', 'receptor__species',
            'publication', 'publication__web_link', 'publication__web_link__web_resource',
            'publication__journal')
        context = dict()
        prepare_data = self.process_data(content)
        context.update({'data': prepare_data})

        return context

    def process_data(self, content):
        '''
        Merge BiasedExperiment with its children
        and pass it back to loop through dublicates
        '''
        rd = dict()
        increment = 0
        for instance in content:
            fin_obj = {}
            fin_obj['main'] = instance
            temp = dict()
            temp['experiment_id'] = instance.id
            temp['publication'] = instance.publication
            temp['ligand'] = instance.ligand
            temp['rece'] = instance.chembl
            temp['chembl'] = instance.chembl
            temp['relevance'] = instance.relevance
            temp['signalling_protein'] = instance.signalling_protein

            if instance.receptor:
                temp['receptor'] = instance.receptor
                temp['class'] = instance.receptor.family.parent.parent.parent.name.split(' ')[1]
                temp['uniprot'] = instance.receptor.entry_short
                temp['IUPHAR'] = instance.receptor.name.split(' ', 1)[
                    0].strip()
            else:
                temp['receptor'] = 'Error appeared'
            # at the moment, there is only 1 pathways for every biased_pathway
            # change if more pathways added (logic changed)
            for entry in instance.biased_pathway.all():
                temp['pathway_outcome_high'] = entry.pathway_outcome_high
                temp['pathway_outcome_summary'] = entry.pathway_outcome_summary
                temp['pathway_outcome_detail'] = entry.pathway_outcome_detail
                temp['experiment_pathway_distinction'] = entry.experiment_pathway_distinction
                temp['experiment_system'] = entry.experiment_system
                temp['experiment_outcome_method'] = entry.experiment_outcome_method

            rd[increment] = temp
            increment += 1
        return rd

    '''
    End  of Bias Browser
    '''

################################################################################
################### NEW STUFF ##################################################

def CachedOTFBiasBrowser(request):
    return CachedOTFBiasBrowsers("bias", False, False, request)

def CachedOTFBiasSubtypeBrowser(request):
    return CachedOTFBiasBrowsers("subtype", False, False, request)

def CachedOTFBalancedBrowser(request):
    return CachedOTFBiasBrowsers("bias", False, True, request)

def CachedOTFBalancedSubtypeBrowser(request):
    return CachedOTFBiasBrowsers("subtype", False, True, request)

def CachedOTFPathwayPrefBrowser(request):
    return CachedOTFBiasBrowsers("pathway", False, False, request)

def CachedOTFBiasBrowserUser(request):
    return CachedOTFBiasBrowsers("bias", True, False, request)

def CachedOTFBiasSubtypeBrowserUser(request):
    return CachedOTFBiasBrowsers("subtype", True, False, request)

def CachedOTFBiasBrowsers(browser_type, user_ligand, balanced, request):
    protein_ids = []
    user_ids = []
    try:
        simple_selection = request.session.get('selection', False)
        for target in simple_selection.reference:
            protein_ids.append(target.item)
    except:
        protein_ids = ["NOSELECTION"]

    try:
        for ligand in simple_selection.targets:
            user_ids.append(ligand.item)
    except:
        user_ids = ["NOSELECTION"]
    keygen = protein_ids + user_ids
    cache_key = "OTFBROWSER_" + browser_type + "_" + hashlib.md5("_".join(keygen).encode('utf-8')).hexdigest()
    return_html = cache.get(cache_key)
    return_html = None #testing
    if return_html == None:
        if user_ligand == False:
            if browser_type == "bias":
                if balanced:
                    return_html = OTFBiasBrowser.as_view(protein_id=protein_ids[0], balanced=True)(request).render()
                else:
                    return_html = OTFBiasBrowser.as_view(protein_id=protein_ids[0])(request).render()
            elif browser_type == "subtype":
                if balanced:
                    return_html = OTFBiasBrowser.as_view(protein_id=protein_ids[0], balanced=True, subtype=True)(request).render()
                else:
                    return_html = OTFBiasBrowser.as_view(protein_id=protein_ids[0], subtype=True)(request).render()
            elif browser_type == "pathway":
                return_html = OTFBiasBrowser.as_view(protein_id=protein_ids[0], pathway=True)(request).render()
        else:
            if browser_type == "bias":
                return_html = OTFBiasBrowser.as_view(protein_id=protein_ids[0], user=user_ids[0])(request).render()
            elif browser_type == "subtype":
                return_html = OTFBiasBrowser.as_view(protein_id=protein_ids[0], user=user_ids[0], subtype=True)(request).render()
        cache.set(cache_key, return_html, 60*60*24*7)
    return return_html

################################################################################
################### END NEW STUFF###############################################

'''
Bias browser between families
access data from db, fill empty fields with empty parse_children
'''
class OTFBiasBrowser(TemplateView):
    protein_id = ''
    subtype = False #need to pass these values onto the context
    pathway = False
    balanced = False
    user = False
    rank_method = 'Default'
    template_name = 'otf_bias_browser.html'
    context_object_name = 'data'
    def get_context_data(self, **kwargs):
        if self.user:
            self.user = int(self.user)
        data = OnTheFly(int(self.protein_id), rank_method=self.rank_method, subtype=self.subtype, pathway=self.pathway, user=self.user, balanced=self.balanced)
        browser_columns = ['Class', 'Receptor family', 'UniProt', 'IUPHAR', 'Species',
                           'Reference ligand', 'Tested ligand', '#Vendors', '#Articles', '#Labs',
                           'P1 - Pathway', 'P2 - Pathway', 'P3 - Pathway', 'P4 - Pathway', 'P5 - Pathway',
                           'P1 - Bias factor', 'P2 - Bias factor', 'P3 - Bias factor', 'P4 - Bias factor', 'P5 - Bias factor',
                           'P1 - Subtype', 'P2 - Subtype', 'P3 - Subtype', 'P4 - Subtype', 'P5 - Subtype',
                           'P1-P2 - ΔΔLog(Tau/KA)', 'P1-P3 - ΔΔLog(Tau/KA)', 'P1-P4 - ΔΔLog(Tau/KA)', 'P1-P5 - ΔΔLog(Tau/KA)',
                           'P1-P2 - ΔΔLog(Emax/EC50)', 'P1-P3 - ΔΔLog(Emax/EC50)', 'P1-P4 - ΔΔLog(Emax/EC50)', 'P1-P5 - ΔΔLog(Emax/EC50)',
                           'P1 - Δlog(Tau/KA)', 'P2 - Δlog(Tau/KA)', 'P3 - Δlog(Tau/KA)', 'P4 - Δlog(Tau/KA)', 'P5 - Δlog(Tau/KA)',
                           'P1 - Δlog(Emax/EC50)', 'P2 - Δlog(Emax/EC50)', 'P3 - Δlog(Emax/EC50)', 'P4 - Δlog(Emax/EC50)', 'P5 - Δlog(Emax/EC50)',
                           'P1 - EC50', 'P2 - EC50', 'P3 - EC50', 'P4 - EC50', 'P5 - EC50',
                           'P1 - Emax', 'P2 - Emax', 'P3 - Emax', 'P4 - Emax', 'P5 - Emax',
                           'P1 - Measured molecule 1', 'P2 - Measured molecule 1', 'P3 - Measured molecule 1', 'P4 - Measured molecule 1', 'P5 - Measured molecule 1',
                           'P1 - Measured molecule 2', 'P2 - Measured molecule 2', 'P3 - Measured molecule 2', 'P4 - Measured molecule 2', 'P5 - Measured molecule 2',
                           'P1 - Biological process', 'P2 - Biological process', 'P3 - Biological process', 'P4 - Biological process', 'P5 - Biological process',
                           'P1 - Cell line', 'P2 - Cell lines', 'P3 - Cell line', 'P4 - Cell line', 'P5 - Cell line',
                           'Time resolved', 'Authors', 'DOI/PMID', 'ID', 'pub_link', 'Reference ligand ID']
        if self.pathway:
            browser_columns = ['Class', 'Receptor family', 'UniProt', 'IUPHAR', 'Species',
                               'Ligand', '#Vendors', '#Articles', '#Labs',
                               'P1 - Pathway', 'P2 - Pathway', 'P3 - Pathway', 'P4 - Pathway', 'P5 - Pathway',
                               'P1-P2 - ΔLog(Tau/KA)', 'P1-P3 - ΔLog(Tau/KA)', 'P1-P4 - ΔLog(Tau/KA)', 'P1-P5 - ΔLog(Tau/KA)',
                               'P1-P2 - ΔLog(Emax/EC50)', 'P1-P3 - ΔLog(Emax/EC50)', 'P1-P4 - ΔLog(Emax/EC50)', 'P1-P5 - ΔLog(Emax/EC50)',
                               'P1 - log(Tau/KA)', 'P2 - log(Tau/KA)', 'P3 - log(Tau/KA)', 'P4 - log(Tau/KA)', 'P5 - log(Tau/KA)',
                               'P1 - log(Emax/EC50)', 'P2 - log(Emax/EC50)', 'P3 - log(Emax/EC50)', 'P4 - log(Emax/EC50)', 'P5 - log(Emax/EC50)',
                               'P1 - EC50', 'P2 - EC50', 'P3 - EC50', 'P4 - EC50', 'P5 - EC50',
                               'P1 - Emax', 'P2 - Emax', 'P3 - Emax', 'P4 - Emax', 'P5 - Emax',
                               'P1 - Measured molecule 1', 'P2 - Measured molecule 1', 'P3 - Measured molecule 1', 'P4 - Measured molecule 1', 'P5 - Measured molecule 1',
                               'P1 - Measured molecule 2', 'P2 - Measured molecule 2', 'P3 - Measured molecule 2', 'P4 - Measured molecule 2', 'P5 - Measured molecule 2',
                               'P1 - Biological process', 'P2 - Biological process', 'P3 - Biological process', 'P4 - Biological process', 'P5 - Biological process',
                               'P1 - Cell line', 'P2 - Cell lines', 'P3 - Cell line', 'P4 - Cell line', 'P5 - Cell line',
                               'Time resolved', 'Authors', 'DOI/PMID', 'ID', 'pub_link']

        table = pd.DataFrame(columns=browser_columns)
        #receptor_id
        receptor_info = Protein.objects.filter(id=self.protein_id).values_list("family__parent__parent__parent__name",
                                                                               "family__parent__name",
                                                                               "entry_name",
                                                                               "name")
        # Preprocess ligand vendors
        lig_ids = set([data[pub][key]['ligand_id'] for pub in data for key in data[pub]])
        vendor_output = list(LigandVendorLink.objects.filter(ligand_id__in=lig_ids).values_list("ligand_id").annotate(Count('vendor_id', distinct=True)))
        vendors_dict = {entry[0]:entry[1] for entry in vendor_output}

        # Preprocess ligand_id => articles
        articles_output = list(BiasedData.objects.filter(ligand_id__in=lig_ids).values_list("ligand_id").annotate(Count('publication_id', distinct=True)))
        articles_dict = {entry[0]:entry[1] for entry in articles_output}

        # Preprocess ligand id => labs
        labs_dict = {}
        for authors in BiasedData.objects.filter(ligand_id__in=lig_ids).exclude(publication_id__authors=None).values_list("ligand_id", "publication_id__authors").distinct():
            if authors[0] not in labs_dict:
                labs_dict[authors[0]] = set()
            labs_dict[authors[0]].add(authors[1].split(',')[-1])

        # Preprocess reference ligands
        reference_names = set([data[pub][key]['Reference_ligand'] for pub in data for key in data[pub] if 'Reference_ligand' in data[pub][key].keys()])
        lig_id_name_pair = list(Ligand.objects.filter(name__in=reference_names).values_list("name", "id"))
        lig_id_name_dict = {entry[0]:entry[1] for entry in lig_id_name_pair}

        for pub in data:
            ligands = {}
            data_subset = pd.DataFrame()
            for key in data[pub]:
                if 'Pathway Rank' in data[pub][key].keys():
                    if data[pub][key]['ligand_id'] not in ligands.keys():
                        labs = []
                        ligands[data[pub][key]['ligand_id']] = {}
                        if self.pathway:
                            ligands[data[pub][key]['ligand_id']]['Ligand'] = data[pub][key]['ligand_name']
                        else:
                            ligands[data[pub][key]['ligand_id']]['Tested ligand'] = data[pub][key]['ligand_name']
                        ligands[data[pub][key]['ligand_id']]['ID'] = key #data[pub][key]['ligand_id']
                        ligands[data[pub][key]['ligand_id']]['Species'] = data[pub][key]['species']
                        ligands[data[pub][key]['ligand_id']]['Authors'] = data[pub][key]['authors']
                        ligands[data[pub][key]['ligand_id']]['DOI/PMID'] = data[pub][key]['doi']
                        ligands[data[pub][key]['ligand_id']]['pub_link'] = "https://pubmed.ncbi.nlm.nih.gov/" + data[pub][key]['doi'] if data[pub][key]['doi'].isdigit() else "https://dx.doi.org/" + data[pub][key]['doi']
                        ligands[data[pub][key]['ligand_id']]['#Vendors'] = vendors_dict[data[pub][key]['ligand_id']] if data[pub][key]['ligand_id'] in vendors_dict else 0
                        ligands[data[pub][key]['ligand_id']]['#Articles'] = articles_dict[data[pub][key]['ligand_id']] if data[pub][key]['ligand_id'] in articles_dict else 0
                        ligands[data[pub][key]['ligand_id']]['#Labs'] = len(labs_dict[data[pub][key]['ligand_id']]) if data[pub][key]['ligand_id'] in labs_dict else 0
                        ligands[data[pub][key]['ligand_id']]['Time resolved'] = data[pub][key]['time_resolved']
                    AddPathwayData(ligands[data[pub][key]['ligand_id']], data[pub][key], data[pub][key]['Pathway Rank'], self.pathway)
                if 'Reference_ligand' in data[pub][key].keys():
                    Reference_ligand = data[pub][key]['Reference_ligand']
            for drug in ligands:
                data_subset = data_subset.append(ligands[drug], ignore_index=True)
            data_subset['Class'] = receptor_info[0][0]
            data_subset['Receptor family'] = receptor_info[0][1].strip('receptors')
            data_subset['UniProt'] = receptor_info[0][2].split('_')[0].upper()
            data_subset['IUPHAR'] = receptor_info[0][3]
            if not self.pathway:
                data_subset['Reference ligand'] = Reference_ligand
                data_subset['Reference ligand ID'] = lig_id_name_dict[Reference_ligand]

            table = table.append(data_subset, ignore_index=True)

        table.fillna('', inplace=True)
        context = dict()

        if self.pathway:
            context['Lig_Count'] = len(table['Ligand'].unique())
        else:
            context['Lig_Count'] = len(table['Tested ligand'].unique())
        context['UniProt'] = receptor_info[0][2].split('_')[0].upper()
        context['IUPHAR'] = receptor_info[0][3]
        context['Array'] = table.to_numpy()
        context['Pathway'] = self.pathway
        context['Balanced'] = self.balanced
        return context

class BiasGuidelines(TemplateView):

    template_name = 'bias_guidelines.html'

    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)
        return context

class ReferenceSelection(TemplateView):

    template_name = 'reference_ligand_selection.html'

    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)
        return context

class EndogenousBrowser(TemplateView):

    template_name = 'endogenous_browser.html'

    def get_context_data(self, **kwargs):

        context = super().get_context_data(**kwargs)

        browser_columns = ['Class', 'Receptor family', 'UniProt', 'IUPHAR', 'Species',
                           'Ligand name', 'GtP link', 'GtP Classification', 'Potency Ranking', 'Type',
                           'pEC50 - min', 'pEC50 - mid', 'pEC50 - max',
                           'pKi - min', 'pKi - mid', 'pKi - max', 'Reference', 'ID']

        table = pd.DataFrame(columns=browser_columns)
        #receptor_id
        endogenous_data = Endogenous_GTP.objects.all().values_list(
                            "receptor__family__parent__parent__parent__name", #0 Class
                            "receptor__family__parent__name",                 #1 Receptor Family
                            "receptor__entry_name",                           #2 UniProt
                            "receptor__name",                                 #3 IUPHAR
                            "receptor__species__common_name",                 #4 Species
                            "ligand__name",                                   #5 Ligand
                            "ligand",                                         #6 Ligand ID
                            "endogenous_status",                              #7 Principal/Secondary
                            "potency_ranking",                                #8 Potency Ranking
                            "ligand__ligand_type__name",                      #9 Type
                            "pec50",                                          #10 pEC50 - min - med - max
                            "pKi",                                            #11 pKi - min - med - max
                            "publication__authors",                           #12 Pub Authors
                            "publication__year",                              #13 Pub Year
                            "publication__title",                             #14 Pub Title
                            "publication__journal__name",                     #15 Pub Journal
                            "publication__reference",                         #16 Pub Reference
                            "publication__web_link__index",                   #17 DOI/PMID
                            "receptor").distinct()                            #18 Receptor ID

        gtpidlinks = dict(list(LigandID.objects.filter(web_resource__slug='gtoplig').values_list(
                            "ligand",
                            "index").distinct()))

        matches = []
        publications = {}
        gtplink = 'https://www.guidetopharmacology.org/GRAC/LigandDisplayForward?ligandId={}'
        pub_ref = "<b>{0}. ({1})</b><br />{2}.<br /><i>{3}</i>, <b>{4}</b> [PMID: <a href='{5}'>{6}</a>]<br /><br />"
        for data in endogenous_data:
            pub_link = ''
            ligand_receptor = str(data[6]) + '_' + str(data[18])
            if data[6] not in gtpidlinks.keys():
                continue
            if ligand_receptor not in publications.keys():
                publications[ligand_receptor] = {}
            if data[17]:
                pub_link = "https://pubmed.ncbi.nlm.nih.gov/" + data[17] if data[17].isdigit() else "https://dx.doi.org/" + data[17]
                #skipping publications without info (probably bug in the database)
                if data[13] == None:
                    continue
                #splicing for years so we can then merge later
                if data[13] not in publications[ligand_receptor].keys():
                    publications[ligand_receptor][data[13]] = ''
                publications[ligand_receptor][data[13]] = publications[ligand_receptor][data[13]] + pub_ref.format(data[12],data[13],data[14],data[15],data[16], pub_link, data[17])
        #Cycling through the years to make a single reference string
        for key in publications:
            years = sorted(publications[key].keys())
            refs = ''
            for year in years:
                refs += publications[key][year]
            publications[key] = refs


        for data in endogenous_data:
            if data[6] not in gtpidlinks.keys():
                continue
            pair = str(data[6]) + '_' + str(data[18])
            if pair not in matches:
                matches.append(pair)
                data_subset = {}
                data_subset['Class'] = data[0].replace('Class ', '')                        #0
                data_subset['Receptor family'] = data[1].strip('receptors')                 #1
                data_subset['UniProt'] = data[2].split('_')[0].upper()                      #2
                data_subset['IUPHAR'] = data[3].strip('receptor')                           #3
                data_subset['Species'] = data[4]                                            #4
                data_subset['Ligand name'] = data[5]                                        #5
                data_subset['GtP link'] =  gtplink.format(gtpidlinks[data[6]])              #6
                data_subset['GtP Classification'] = data[7] if data[7] else ""              #7
                data_subset['Potency Ranking'] = str(data[8]) if data[8] else ""            #8
                data_subset['Type'] = data[9].replace('-',' ').capitalize()                 #9
                data_subset['pEC50 - min'] = data[10].split(' | ')[0]                       #10
                data_subset['pEC50 - mid'] = data[10].split(' | ')[1]                       #11
                data_subset['pEC50 - max'] = data[10].split(' | ')[2]                       #12
                data_subset['pKi - min'] = data[11].split(' | ')[0]                         #13
                data_subset['pKi - mid'] = data[11].split(' | ')[1]                         #14
                data_subset['pKi - max'] = data[11].split(' | ')[2]                         #15
                if len(publications[pair]) != 0:
                    data_subset['Reference'] = publications[pair]                           #16
                else:
                    data_subset['Reference'] = 'empty'
                data_subset['ID'] = data[6]                                                 #17

                table = table.append(data_subset, ignore_index=True)

        table.fillna('', inplace=True)
        # context = dict()
        context['Array'] = table.to_numpy()
        return context