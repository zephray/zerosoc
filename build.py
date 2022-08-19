#!/usr/bin/env python3

import argparse
import copy
import siliconcompiler
import os
import shutil

from sources import add_sources

from floorplan import generate_core_floorplan, generate_top_floorplan

# Path to 'caravel' repository root.
CARAVEL_ROOT = '/home/wenting/caravel'
DESIGN = 'user_project_wrapper'

def setup_options(chip):
    '''Helper to setup common options for each build.'''
    #chip.set('option', 'loglevel', 'INFO')

    # Prevent us from erroring out on lint warnings during import
    chip.set('option', 'relax', True)
    #chip.set('option', 'quiet', True)

    # hack to work around fact that $readmemh now runs in context of build
    # directory and can't load .mem files using relative paths
    cur_dir = os.path.dirname(os.path.realpath(__file__))
    chip.add('option', 'define', f'MEM_ROOT={cur_dir}')

def configure_core_chip(remote=False):
    chip = siliconcompiler.Chip(DESIGN)

    setup_options(chip)

    chip.set('option', 'frontend', 'systemverilog')
    chip.load_target('skywater130_demo')
    chip.load_flow('mpwflow')
    chip.set('option', 'flow', 'mpwflow')

    chip.set('tool', 'openroad', 'var', 'place', '0', 'place_density', ['0.15'])
    chip.set('tool', 'openroad', 'var', 'route', '0', 'grt_allow_congestion', ['true'])

    chip.set('asic', 'macrolib', ['sky130sram'])
    chip.load_lib('sky130sram')

    # Ignore cells in these libraries during DRC, they violate the rules but are
    # foundry-validated
    for step in ('extspice', 'drc'):
        chip.set('tool', 'magic', 'var', step, '0', 'exclude', ['sky130sram', 'sky130io'])
    chip.set('tool', 'netgen', 'var', 'lvs', '0', 'exclude', ['sky130sram', 'sky130io'])

    # Need to copy library files into build directory for remote run so the
    # server can access them
    if remote:
        stackup = chip.get('asic', 'stackup')
        chip.set('library', 'sky130sram', 'model', 'timing', 'nldm', 'typical', True, field='copy')
        chip.set('library', 'sky130sram', 'model', 'layout', 'lef', stackup, True, field='copy')
        chip.set('library', 'sky130sram', 'model', 'layout', 'gds', stackup, True, field='copy')

        chip.set('option', 'remote', True)

    add_sources(chip)

    chip.clock('user_clock2', period=20)

    chip.add('option', 'define', 'PRIM_DEFAULT_IMPL="prim_pkg::ImplSky130"')
    chip.add('option', 'define', 'RAM_DEPTH=512')

    chip.add('input', 'verilog', f'{CARAVEL_ROOT}/verilog/rtl/defines.v')
    chip.add('input', 'verilog', 'hw/user_project_wrapper.v')

    chip.add('input', 'verilog', 'hw/prim/sky130/prim_sky130_ram_1p.v')
    chip.add('input', 'verilog', 'asic/sky130/ram/sky130_sram_2kbyte_1rw1r_32x512_8.bb.v')

    chip.add('input', 'verilog', 'hw/prim/sky130/prim_sky130_clock_gating.v')

    return chip

def build_core(verify=True, remote=False):
    chip = configure_core_chip(remote)
    stackup = chip.get('asic', 'stackup')

    #generate_core_floorplan(chip)
    #chip.set('model', 'layout', 'lef', stackup, 'asic_core.lef')
    chip.set('input', 'floorplan.def', 'user_project_wrapper_nogrid.def')

    libtype = 'unithd'
    pdk = chip.get('option', 'pdk')
    with open('pdngen_top.tcl', 'w') as pdnf:
        # TODO: Jinja template?
        pdnf.write('''
# Add PDN connections for each voltage domain.
add_global_connection -net vccd1 -pin_pattern "^VPWR$" -power
add_global_connection -net vssd1 -pin_pattern "^VGND$" -ground
add_global_connection -net vccd1 -pin_pattern "^POWER$" -power
add_global_connection -net vssd1 -pin_pattern "^GROUND$" -ground
add_global_connection -net vccd1 -pin_pattern vccd1
add_global_connection -net vssd1 -pin_pattern vssd1
global_connect

set_voltage_domain -name Core -power vccd1 -ground vssd1 -secondary_power {vccd2 vssd2 vdda1 vssa1 vdda2 vssa2}
#set_voltage_domain -name Core -power vccd1 -ground vssd1
define_pdn_grid -name top_grid -voltage_domain Core -starts_with POWER -pins {met4 met5}

add_pdn_stripe -grid top_grid -layer met1 -width 0.48 -pitch 5.44 -spacing 2.24 -offset 0 -starts_with POWER -nets {vccd1 vssd1}
add_pdn_stripe -grid top_grid -layer met4 -width 3.1 -pitch 90 -spacing 41.9 -offset 5 -starts_with POWER -extend_to_core_ring -nets {vccd1 vssd1}
add_pdn_stripe -grid top_grid -layer met5 -width 3.1 -pitch 90 -spacing 41.9 -offset 5 -starts_with POWER -extend_to_core_ring -nets {vccd1 vssd1}
add_pdn_connect -grid top_grid -layers {met1 met4}
add_pdn_connect -grid top_grid -layers {met4 met5}

add_pdn_ring -grid top_grid -layers {met4 met5} -widths {3.1 3.1} -spacings {1.7 1.7} -core_offset {12.45 12.45}
#add_pdn_ring -grid top_grid -layers {met4 met5} -widths {3.1 3.1} -spacings {1.7 1.7} -core_offset {14 14}

define_pdn_grid -macro -default -name macro -voltage_domain Core -halo 3.0 -starts_with POWER -grid_over_boundary
add_pdn_connect -grid macro -layers {met4 met5}

# Done defining commands; generate PDN.
pdngen''')
    chip.set('pdk', pdk, 'aprtech', 'openroad', stackup, libtype, 'pdngen', 'pdngen_top.tcl')

    run_build(chip)

    # Add via definitions to the gate-level netlist.
    shutil.copy(chip.find_result('vg', step='addvias'), f'{DESIGN}.vg')

    if verify:
        run_signoff(chip, 'dfm', 'export')

    return chip

def run_build(chip):
    chip.run()
    chip.summary()

def run_signoff(chip, netlist_step, layout_step):
    gds_path = chip.find_result('gds', step=layout_step)
    netlist_path = chip.find_result('vg', step=netlist_step)

    jobname = chip.get('option', 'jobname')
    chip.set('option', 'jobname', f'{jobname}_signoff')
    chip.set('option', 'flow', 'signoffflow')

    # Hack: workaround the fact that remote runs alter the steplist
    if chip.get('option', 'remote'):
        chip.set('option', 'steplist', [])

    chip.set('input', 'gds', gds_path)
    chip.set('input', 'netlist', netlist_path)

    run_build(chip)

def main():
    build_core(verify=False, remote=False)

if __name__ == '__main__':
    main()
