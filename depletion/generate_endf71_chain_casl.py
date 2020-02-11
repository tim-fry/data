#!/usr/bin/env python3

import glob
import os
from zipfile import ZipFile
from collections import OrderedDict, defaultdict
from io import StringIO
from itertools import chain

try:
    import lxml.etree as ET
    _have_lxml = True
except ImportError:
    import xml.etree.ElementTree as ET
    _have_lxml = False

import openmc.data
import openmc.deplete
from openmc._xml import clean_indentation
from openmc.deplete.chain import _REACTIONS
from openmc.deplete.nuclide import Nuclide, DecayTuple, ReactionTuple, \
    FissionYieldDistribution
from openmc._utils import download

from casl_chain import CASL_CHAIN, UNMODIFIED_DECAY_BR

URLS = [
    'https://www.nndc.bnl.gov/endf/b7.1/zips/ENDF-B-VII.1-neutrons.zip',
    'https://www.nndc.bnl.gov/endf/b7.1/zips/ENDF-B-VII.1-decay.zip',
    'https://www.nndc.bnl.gov/endf/b7.1/zips/ENDF-B-VII.1-nfy.zip'
]

def main():
    if os.path.isdir('./decay') and os.path.isdir('./nfy') and os.path.isdir('./neutrons'):
        endf_dir = '.'
    elif 'OPENMC_ENDF_DATA' in os.environ:
        endf_dir = os.environ['OPENMC_ENDF_DATA']
    else:
        for url in URLS:
            basename = download(url)
            with ZipFile(basename, 'r') as zf:
                print('Extracting {}...'.format(basename))
                zf.extractall()
        endf_dir = '.'

    decay_files = glob.glob(os.path.join(endf_dir, 'decay', '*.endf'))
    fpy_files = glob.glob(os.path.join(endf_dir, 'nfy', '*.endf'))
    neutron_files = glob.glob(os.path.join(endf_dir, 'neutrons', '*.endf'))

    # Create a Chain
    chain = openmc.deplete.Chain()

    print('Reading ENDF nuclear data from "{}"...'.format(os.path.abspath(endf_dir)))

    # Create dictionary mapping target to filename
    print('Processing neutron sub-library files...')
    reactions = {}
    for f in neutron_files:
        evaluation = openmc.data.endf.Evaluation(f)
        nuc_name = evaluation.gnd_name
        if nuc_name in CASL_CHAIN:
            reactions[nuc_name] = {}
            for mf, mt, nc, mod in evaluation.reaction_list:
                # Q value for each reaction is given in MF=3
                if mf == 3:
                    file_obj = StringIO(evaluation.section[3, mt])
                    openmc.data.endf.get_head_record(file_obj)
                    q_value = openmc.data.endf.get_cont_record(file_obj)[1]
                    reactions[nuc_name][mt] = q_value

    # Determine what decay and FPY nuclides are available
    print('Processing decay sub-library files...')
    decay_data = {}
    for f in decay_files:
        decay_obj = openmc.data.Decay(f)
        nuc_name = decay_obj.nuclide['name']
        if nuc_name in CASL_CHAIN:
            decay_data[nuc_name] = decay_obj

    for nuc_name in CASL_CHAIN:
        if nuc_name not in decay_data:
            print('WARNING: {} has no decay data!'.format(nuc_name))

    print('Processing fission product yield sub-library files...')
    fpy_data = {}
    for f in fpy_files:
        fpy_obj = openmc.data.FissionProductYields(f)
        name = fpy_obj.nuclide['name']
        if name in CASL_CHAIN:
            fpy_data[name] = fpy_obj

    print('Creating depletion_chain...')
    missing_daughter = []
    missing_rx_product = []
    missing_fpy = []

    for idx, parent in enumerate(sorted(decay_data, key=openmc.data.zam)):
        data = decay_data[parent]

        nuclide = Nuclide()
        nuclide.name = parent

        chain.nuclides.append(nuclide)
        chain.nuclide_dict[parent] = idx

        if not CASL_CHAIN[parent][0] and \
           not data.nuclide['stable'] and data.half_life.nominal_value != 0.0:
            nuclide.half_life = data.half_life.nominal_value
            nuclide.decay_energy = sum(E.nominal_value for E in
                                       data.average_energies.values())
            sum_br = 0.0
            for mode in data.modes:
                decay_type = ','.join(mode.modes)
                if mode.daughter in decay_data:
                    target = mode.daughter
                else:
                    missing_daughter.append((parent, mode))
                    continue

                # Append decay mode
                br = mode.branching_ratio.nominal_value
                nuclide.decay_modes.append(DecayTuple(decay_type, target, br))

            # Ensure sum of branching ratios is unity by slightly modifying last
            # value if necessary
            sum_br = sum(m.branching_ratio for m in nuclide.decay_modes)
            if sum_br != 1.0 and nuclide.decay_modes and parent not in UNMODIFIED_DECAY_BR:
                decay_type, target, br = nuclide.decay_modes.pop()
                br = 1.0 - sum(m.branching_ratio for m in nuclide.decay_modes)
                nuclide.decay_modes.append(DecayTuple(decay_type, target, br))

        # If nuclide has incident neutron data, we need to list what
        # transmutation reactions are possible
        if parent in reactions:
            reactions_available = reactions[parent].keys()
            for name, mts, changes in _REACTIONS:
                if mts & reactions_available:
                    delta_A, delta_Z = changes
                    A = data.nuclide['mass_number'] + delta_A
                    Z = data.nuclide['atomic_number'] + delta_Z
                    daughter = '{}{}'.format(openmc.data.ATOMIC_SYMBOL[Z], A)

                    if name not in chain.reactions:
                        chain.reactions.append(name)

                    if daughter not in decay_data:
                        missing_rx_product.append((parent, name, daughter))
                        daughter = 'Nothing'

                    # Store Q value -- use sorted order so we get summation
                    # reactions (e.g., MT=103) first
                    for mt in sorted(mts):
                        if mt in reactions[parent]:
                            q_value = reactions[parent][mt]
                            break
                    else:
                        q_value = 0.0

                    nuclide.reactions.append(ReactionTuple(
                        name, daughter, q_value, 1.0))

            # Check for fission reactions
            if any(mt in reactions_available for mt in [18, 19, 20, 21, 38]):
                if parent in fpy_data:
                    q_value = reactions[parent][18]
                    nuclide.reactions.append(
                        ReactionTuple('fission', 0, q_value, 1.0))

                    if 'fission' not in chain.reactions:
                        chain.reactions.append('fission')
                else:
                    missing_fpy.append(parent)

        if parent in fpy_data:
            fpy = fpy_data[parent]

            if fpy.energies is not None:
                yield_energies = fpy.energies
            else:
                yield_energies = [0.0]

            yield_data = {}
            for E, table_yd, table_yc in zip(yield_energies, fpy.independent, fpy.cumulative):
                yields = defaultdict(float)
                for product in table_yd:
                    if product in decay_data:
                        # identifier
                        ifpy = CASL_CHAIN[product][2]
                        # 1 for independent
                        if ifpy == 1:
                            if product not in table_yd:
                                print('No independent fission yields found for {} in {}'.format(product, parent))
                            else:
                                yields[product] += table_yd[product].nominal_value
                        # 2 for cumulative
                        elif ifpy == 2:
                            if product not in table_yc:
                                print('No cumulative fission yields found for {} in {}'.format(product, parent))
                            else:
                                yields[product] += table_yc[product].nominal_value
                        # 3 for special treatment with weight fractions
                        elif ifpy == 3:
                            for name_i, weight_i, ifpy_i in CASL_CHAIN[product][3]:
                                if name_i not in table_yd:
                                    print('No fission yields found for {} in {}'.format(name_i, parent))
                                else:
                                    if ifpy_i == 1:
                                        yields[product] += weight_i * table_yd[name_i].nominal_value
                                    elif ifpy_i == 2:
                                        yields[product] += weight_i * table_yc[name_i].nominal_value

                yield_data[E] = yields

            nuclide.yield_data = FissionYieldDistribution(yield_data)

    # Display warnings
    if missing_daughter:
        print('The following decay modes have daughters with no decay data:')
        for parent, mode in missing_daughter:
            print('  {} -> {} ({})'.format(parent, mode.daughter, ','.join(mode.modes)))
        print('')

    if missing_rx_product:
        print('The following reaction products have no decay data:')
        for vals in missing_rx_product:
            print('{} {} -> {}'.format(*vals))
        print('')

    if missing_fpy:
        print('The following fissionable nuclides have no fission product yields:')
        for parent in missing_fpy:
            print('  ' + parent)
        print('')

    chain.export_to_xml('chain_casl.xml')


if __name__ == '__main__':
    main()
