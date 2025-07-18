# Copyright 2015 IBM Corp.
# Copyright 2012 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
"""Tests for the IBM Storwize family and SVC volume driver."""

import json
import random
import re
import time
from unittest import mock

import ddt
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_service import loopingcall
from oslo_utils import units
import paramiko

from cinder import context
import cinder.db
from cinder.db.sqlalchemy import models
from cinder import exception
from cinder.i18n import _
from cinder import objects
from cinder.objects import fields
from cinder import ssh_utils
from cinder.tests.unit import fake_constants as fake
from cinder.tests.unit import test
from cinder.tests.unit import utils as testutils
from cinder import utils
from cinder.volume import configuration as conf
from cinder.volume.drivers.ibm.storwize_svc import (
    replication as storwize_rep)
from cinder.volume.drivers.ibm.storwize_svc import storwize_const
from cinder.volume.drivers.ibm.storwize_svc import storwize_svc_common
from cinder.volume.drivers.ibm.storwize_svc import storwize_svc_fc
from cinder.volume.drivers.ibm.storwize_svc import storwize_svc_iscsi
from cinder.volume import group_types
from cinder.volume import qos_specs
from cinder.volume import volume_types
from cinder.volume import volume_utils


SVC_POOLS = ['openstack', 'openstack1']
SVC_SOURCE_CHILD_POOL = 'openstack2'
SVC_TARGET_CHILD_POOL = 'openstack3'

CONF = cfg.CONF


def _get_test_pool(get_all=False):
    if get_all:
        return SVC_POOLS
    else:
        return SVC_POOLS[0]


class StorwizeSVCManagementSimulator(object):
    def __init__(self, pool_name):
        self._flags = {'storwize_svc_volpool_name': pool_name}
        self._volumes_list = {}
        self._hosts_list = {}
        self._mappings_list = {}
        self._fcmappings_list = {}
        self._fcconsistgrp_list = {}
        self._rcrelationship_list = {}
        self._partnership_list = {}
        self._partnershipcandidate_list = {}
        self._rcconsistgrp_list = {}
        self._volumegroup_list = {}
        self._volumegroup_snapshot_list = {}
        self._system_list = {'storwize-svc-sim': {'id': '0123456789ABCDEF',
                                                  'name': 'storwize-svc-sim'},
                             'aux-svc-sim': {'id': 'ABCDEF0123456789',
                                             'name': 'aux-svc-sim'}}
        self._other_pools = {'openstack2': {}, 'openstack3': {}}
        self._next_cmd_error = {
            'lsportip': '',
            'lsip': '',
            'lsfabric': '',
            'lsfcportsetmember': '',
            'lsiscsiauth': '',
            'lsnodecanister': '',
            'mkvdisk': '',
            'lsvdisk': '',
            'lsfcmap': '',
            'prestartfcmap': '',
            'startfcmap': '',
            'rmfcmap': '',
            'lslicense': '',
            'lsguicapabilities': '',
            'lshost': '',
            'lsrcrelationship': '',
            'expandvdisksize': ''
        }
        self._errors = {
            'CMMVC5701E': ('', 'CMMVC5701E No object ID was specified.'),
            'CMMVC6035E': ('', 'CMMVC6035E The action failed as the '
                               'object already exists.'),
            'CMMVC5753E': ('', 'CMMVC5753E The specified object does not '
                               'exist or is not a suitable candidate.'),
            'CMMVC5707E': ('', 'CMMVC5707E Required parameters are missing.'),
            'CMMVC6581E': ('', 'CMMVC6581E The command has failed because '
                               'the maximum number of allowed iSCSI '
                               'qualified names (IQNs) has been reached, '
                               'or the IQN is already assigned or is not '
                               'valid.'),
            'CMMVC5754E': ('', 'CMMVC5754E The specified object does not '
                               'exist, or the name supplied does not meet '
                               'the naming rules.'),
            'CMMVC6071E': ('', 'CMMVC6071E The VDisk-to-host mapping was '
                               'not created because the VDisk is already '
                               'mapped to a host.'),
            'CMMVC5879E': ('', 'CMMVC5879E The VDisk-to-host mapping was '
                               'not created because a VDisk is already '
                               'mapped to this host with this SCSI LUN.'),
            'CMMVC5840E': ('', 'CMMVC5840E The virtual disk (VDisk) was '
                               'not deleted because it is mapped to a '
                               'host or because it is part of a FlashCopy '
                               'or Remote Copy mapping, or is involved in '
                               'an image mode migrate.'),
            'CMMVC6527E': ('', 'CMMVC6527E The name that you have entered '
                               'is not valid. The name can contain letters, '
                               'numbers, spaces, periods, dashes, and '
                               'underscores. The name must begin with a '
                               'letter or an underscore. The name must not '
                               'begin or end with a space.'),
            'CMMVC5871E': ('', 'CMMVC5871E The action failed because one or '
                               'more of the configured port names is in a '
                               'mapping.'),
            'CMMVC5924E': ('', 'CMMVC5924E The FlashCopy mapping was not '
                               'created because the source and target '
                               'virtual disks (VDisks) are different sizes.'),
            'CMMVC6303E': ('', 'CMMVC6303E The create failed because the '
                               'source and target VDisks are the same.'),
            'CMMVC7050E': ('', 'CMMVC7050E The command failed because at '
                               'least one node in the I/O group does not '
                               'support compressed VDisks.'),
            'CMMVC6430E': ('', 'CMMVC6430E The command failed because the '
                               'target and source managed disk groups must '
                               'be different.'),
            'CMMVC6353E': ('', 'CMMVC6353E The command failed because the '
                               'copy specified does not exist.'),
            'CMMVC6446E': ('', 'The command failed because the managed disk '
                               'groups have different extent sizes.'),
            # Catch-all for invalid state transitions:
            'CMMVC5903E': ('', 'CMMVC5903E The FlashCopy mapping was not '
                               'changed because the mapping or consistency '
                               'group is another state.'),
            'CMMVC5709E': ('', 'CMMVC5709E [-%(VALUE)s] is not a supported '
                               'parameter.'),
            'CMMVC5982E': ('', 'CMMVC5982E The operation was not performed '
                               'because it is not valid given the current '
                               'relationship state.'),
            'CMMVC5963E': ('', 'CMMVC5963E No direction has been defined.'),
            'CMMVC5713E': ('', 'CMMVC5713E Some parameters are mutually '
                               'exclusive.'),
            'CMMVC5804E': ('', 'CMMVC5804E The action failed because an '
                               'object that was specified in the command '
                               'does not exist.'),
            'CMMVC6065E': ('', 'CMMVC6065E The action failed as the object '
                               'is not in a group.'),
            'CMMVC9012E': ('', 'CMMVC9012E The copy type differs from other '
                               'copies already in the consistency group.'),
            'CMMVC5951E': ('', 'CMMVC5951E The operation cannot be performed '
                               'because the relationship is not a stand-alone '
                               'relationship.'),
            'CMMVC9201E': ('', 'CMMVC9201E Task failed because volume has a '
                               'copy that is fully allocated and is part of a '
                               'Metro Mirror or Global Mirror relationship.'),
            'CMMVC8587E': ('', 'CMMVC8587E The command failed because the '
                               'volume is fast formatting.'),
            'CMMVC8783E': ('', 'CMMVC8783E The volume copy was not deleted '
                               'because the volume is part of a consistency '
                               'group.'),
            'CMMVC6578E': ('', 'CMMVC6578E The command has failed because '
                               'the iSCSI name is already assigned or is '
                               'not valid.'),
        }
        self._fc_transitions = {'begin': {'make': 'idle_or_copied'},
                                'idle_or_copied': {'prepare': 'preparing',
                                                   'delete': 'end',
                                                   'delete_force': 'end'},
                                'preparing': {'flush_failed': 'stopped',
                                              'wait': 'prepared'},
                                'end': None,
                                'stopped': {'prepare': 'preparing',
                                            'delete_force': 'end'},
                                'prepared': {'stop': 'stopped',
                                             'start': 'copying'},
                                'copying': {'wait': 'idle_or_copied',
                                            'stop': 'stopping'},
                                # Assume the worst case where stopping->stopped
                                # rather than stopping idle_or_copied
                                'stopping': {'wait': 'stopped'},
                                }

        self._fc_cg_transitions = {'begin': {'make': 'empty'},
                                   'empty': {'add': 'idle_or_copied'},
                                   'idle_or_copied': {'prepare': 'preparing',
                                                      'delete': 'end',
                                                      'delete_force': 'end'},
                                   'preparing': {'flush_failed': 'stopped',
                                                 'wait': 'prepared'},
                                   'end': None,
                                   'stopped': {'prepare': 'preparing',
                                               'delete_force': 'end'},
                                   'prepared': {'stop': 'stopped',
                                                'start': 'copying',
                                                'delete_force': 'end',
                                                'delete': 'end'},
                                   'copying': {'wait': 'idle_or_copied',
                                               'stop': 'stopping',
                                               'delete_force': 'end',
                                               'delete': 'end'},
                                   # Assume the case where stopping->stopped
                                   # rather than stopping idle_or_copied
                                   'stopping': {'wait': 'stopped'},
                                   }
        self._rc_transitions = {'inconsistent_stopped':
                                {'start': 'inconsistent_copying',
                                 'stop': 'inconsistent_stopped',
                                 'delete': 'end',
                                 'delete_force': 'end'},
                                'inconsistent_copying': {
                                    'wait': 'consistent_synchronized',
                                    'start': 'inconsistent_copying',
                                    'stop': 'inconsistent_stopped',
                                    'delete': 'end',
                                    'delete_force': 'end'},
                                'consistent_synchronized': {
                                    'start': 'consistent_synchronized',
                                    'stop': 'consistent_stopped',
                                    'stop_access': 'idling',
                                    'delete': 'end',
                                    'delete_force': 'end'},
                                'consistent_copying': {
                                    'start': 'consistent_copying',
                                    'stop': 'consistent_stopped',
                                    'stop_access': 'idling',
                                    'delete': 'end',
                                    'delete_force': 'end'},
                                'consistent_stopped':
                                {'start': 'consistent_synchronized',
                                 'stop': 'consistent_stopped',
                                 'delete': 'end',
                                 'delete_force': 'end'},
                                'end': None,
                                'idling': {
                                    'start': 'inconsistent_copying',
                                    'stop': 'inconsistent_stopped',
                                    'stop_access': 'idling',
                                    'delete': 'end',
                                    'delete_force': 'end'},
                                }
        self._rccg_transitions = {'empty': {'add': 'inconsistent_stopped',
                                            'delete': 'end',
                                            'delete_force': 'end'},
                                  'inconsistent_stopped':
                                      {'start': 'inconsistent_copying',
                                       'stop': 'inconsistent_stopped',
                                       'delete': 'end',
                                       'delete_force': 'end'},
                                  'inconsistent_copying': {
                                      'wait': 'consistent_synchronized',
                                      'start': 'inconsistent_copying',
                                      'stop': 'inconsistent_stopped',
                                      'delete': 'end',
                                      'delete_force': 'end'},
                                  'consistent_synchronized': {
                                      'start': 'consistent_synchronized',
                                      'stop': 'consistent_stopped',
                                      'stop_access': 'idling',
                                      'delete': 'end',
                                      'delete_force': 'end'},
                                  'consistent_stopped':
                                      {'start': 'consistent_synchronized',
                                       'stop': 'consistent_stopped',
                                       'delete': 'end',
                                       'delete_force': 'end'},
                                  'consistent_copying': {
                                      'start': 'consistent_copying',
                                      'stop': 'consistent_stopped',
                                      'stop_access': 'idling',
                                      'delete': 'end',
                                      'delete_force': 'end'},
                                  'end': None,
                                  'idling': {
                                      'start': 'inconsistent_copying',
                                      'stop': 'inconsistent_stopped',
                                      'stop_access': 'idling',
                                      'delete': 'end',
                                      'delete_force': 'end'},
                                  }

    def _state_transition(self, function, fcmap):
        if (function == 'wait' and
                'wait' not in self._fc_transitions[fcmap['status']]):
            return ('', '')

        if fcmap['status'] == 'copying' and function == 'wait':
            if fcmap['copyrate'] != '0':
                if fcmap['progress'] == '0':
                    fcmap['progress'] = '50'
                else:
                    fcmap['progress'] = '100'
                    fcmap['status'] = 'idle_or_copied'
            return ('', '')
        else:
            try:
                curr_state = fcmap['status']
                fcmap['status'] = self._fc_transitions[curr_state][function]
                return ('', '')
            except Exception:
                return self._errors['CMMVC5903E']

    def _fc_cg_state_transition(self, function, fc_consistgrp):
        if (function == 'wait' and
                'wait' not in self._fc_transitions[fc_consistgrp['status']]):
            return ('', '')

        try:
            curr_state = fc_consistgrp['status']
            fc_consistgrp['status'] \
                = self._fc_cg_transitions[curr_state][function]
            return ('', '')
        except Exception:
            return self._errors['CMMVC5903E']

    # Find an unused ID
    @staticmethod
    def _find_unused_id(d):
        ids = []
        for v in d.values():
            ids.append(int(v['id']))
        ids.sort()
        for index, n in enumerate(ids):
            if n > index:
                return str(index)
        return str(len(ids))

    # Check if name is valid
    @staticmethod
    def _is_invalid_name(name):
        if re.match(r'^[a-zA-Z_][\w._-]*$', name):
            return False
        return True

    # Convert argument string to dictionary
    @staticmethod
    def _cmd_to_dict(arg_list):
        no_param_args = [
            'autodelete',
            'bytes',
            'compressed',
            'force',
            'nohdr',
            'nofmtdisk',
            'noconsistgrp',
            'global',
            'access',
            'start',
            'thin',
            'removehostmappings',
            'removefcmaps',
            'removercrelationships',
            'novolumegroup',
            'ignorelegacy'
        ]
        one_param_args = [
            'chapsecret',
            'cleanrate',
            'copy',
            'copyrate',
            'delim',
            'easytier',
            'filtervalue',
            'grainsize',
            'hbawwpn',
            'host',
            'iogrp',
            'iscsiname',
            'mdiskgrp',
            'name',
            'rsize',
            'scsi',
            'size',
            'source',
            'target',
            'unit',
            'vdisk',
            'warning',
            'wwpn',
            'primary',
            'consistgrp',
            'master',
            'aux',
            'cluster',
            'linkbandwidthmbits',
            'backgroundcopyrate',
            'copies',
            'cyclingmode',
            'cycleperiodseconds',
            'masterchange',
            'auxchange',
            'pool',
            'site',
            'buffersize',
            'volumegroup',
            'snapshot'
        ]
        no_or_one_param_args = [
            'autoexpand',
        ]

        # Handle the special case of lsnode which is a two-word command
        # Use the one word version of the command internally
        if arg_list[0] in ('svcinfo', 'svctask'):
            if arg_list[1] == 'lsnode':
                if len(arg_list) > 4:  # e.g. svcinfo lsnode -delim ! <node id>
                    ret = {'cmd': 'lsnode', 'node_id': arg_list[-1]}
                else:
                    ret = {'cmd': 'lsnodecanister'}
            else:
                ret = {'cmd': arg_list[1]}
            arg_list.pop(0)
        else:
            ret = {'cmd': arg_list[0]}

        skip = False
        for i in range(1, len(arg_list)):
            if skip:
                skip = False
                continue
            # Check for a quoted command argument for volumes and strip
            # quotes so that the simulater can match it later. Just
            # match against test naming convensions for now.
            if arg_list[i][0] == '"' and ('volume' in arg_list[i] or
                                          'snapshot' in arg_list[i]):
                arg_list[i] = arg_list[i][1:-1]
            if arg_list[i][0] == '-':
                if arg_list[i][1:] in no_param_args:
                    ret[arg_list[i][1:]] = True
                elif arg_list[i][1:] in one_param_args:
                    ret[arg_list[i][1:]] = arg_list[i + 1]
                    skip = True
                elif arg_list[i][1:] in no_or_one_param_args:
                    if i == (len(arg_list) - 1) or arg_list[i + 1][0] == '-':
                        ret[arg_list[i][1:]] = True
                    else:
                        ret[arg_list[i][1:]] = arg_list[i + 1]
                        skip = True
                else:
                    raise exception.InvalidInput(
                        reason=_('unrecognized argument %s') % arg_list[i])
            else:
                ret['obj'] = arg_list[i]
        return ret

    @staticmethod
    def _print_info_cmd(rows, delim=' ', nohdr=False, **kwargs):
        """Generic function for printing information."""
        if nohdr:
            del rows[0]

        for index in range(len(rows)):
            rows[index] = delim.join(rows[index])
        return ('%s' % '\n'.join(rows), '')

    @staticmethod
    def _print_info_obj_cmd(header, row, delim=' ', nohdr=False):
        """Generic function for printing information for a specific object."""
        objrows = []
        for idx, val in enumerate(header):
            objrows.append([val, row[idx]])

        if nohdr:
            for index in range(len(objrows)):
                objrows[index] = ' '.join(objrows[index][1:])
        for index in range(len(objrows)):
            objrows[index] = delim.join(objrows[index])
        return ('%s' % '\n'.join(objrows), '')

    @staticmethod
    def _convert_bytes_units(bytestr):
        num = int(bytestr)
        unit_array = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
        unit_index = 0

        while num > 1024:
            num = num / 1024
            unit_index += 1

        return '%d%s' % (num, unit_array[unit_index])

    @staticmethod
    def _convert_units_bytes(num, unit):
        unit_array = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
        unit_index = 0

        while unit.lower() != unit_array[unit_index].lower():
            num = num * 1024
            unit_index += 1

        return str(num)

    def _cmd_lslicense(self, **kwargs):
        rows = [None] * 3
        rows[0] = ['used_compression_capacity', '0.08']
        rows[1] = ['license_compression_capacity', '0']
        if self._next_cmd_error['lslicense'] == 'no_compression':
            self._next_cmd_error['lslicense'] = ''
            rows[2] = ['license_compression_enclosures', '0']
        else:
            rows[2] = ['license_compression_enclosures', '1']
        return self._print_info_cmd(rows=rows, **kwargs)

    def _cmd_lsguicapabilities(self, **kwargs):
        rows = [None] * 2
        if self._next_cmd_error['lsguicapabilities'] == 'no_compression':
            self._next_cmd_error['lsguicapabilities'] = ''
            rows[0] = ['license_scheme', '0']
        else:
            rows[0] = ['license_scheme', 'flex']
        rows[1] = ['product_key', storwize_const.DEV_MODEL_SVC]
        return self._print_info_cmd(rows=rows, **kwargs)

    # Print mostly made-up stuff in the correct syntax
    def _cmd_lssystem(self, **kwargs):
        rows = [None] * 4
        rows[0] = ['id', '0123456789ABCDEF']
        rows[1] = ['name', 'storwize-svc-sim']
        rows[2] = ['code_level', '7.2.0.0 (build 87.0.1311291000)']
        rows[3] = ['topology', '']
        return self._print_info_cmd(rows=rows, **kwargs)

    def _cmd_lssystem_aux(self, **kwargs):
        rows = [None] * 4
        rows[0] = ['id', 'ABCDEF0123456789']
        rows[1] = ['name', 'aux-svc-sim']
        rows[2] = ['code_level', '7.2.0.0 (build 87.0.1311291000)']
        rows[3] = ['topology', '']
        return self._print_info_cmd(rows=rows, **kwargs)

    # Print mostly made-up stuff in the correct syntax, assume -bytes passed
    def _cmd_lsmdiskgrp(self, **kwargs):
        pool_num = len(self._flags['storwize_svc_volpool_name'])
        rows = []
        rows.append(['id', 'name', 'status', 'mdisk_count',
                     'vdisk_count', 'capacity', 'extent_size',
                     'free_capacity', 'virtual_capacity', 'used_capacity',
                     'real_capacity', 'overallocation', 'warning',
                     'easy_tier', 'easy_tier_status', 'site_id', 'site_name',
                     'data_reduction'])
        for i in range(pool_num):
            row_data = [str(i + 1),
                        self._flags['storwize_svc_volpool_name'][i], 'online',
                        '1', str(len(self._volumes_list)),
                        '3573412790272', '256', '3529926246400',
                        '1693247906775',
                        '26843545600', '38203734097', '47', '80', 'auto',
                        'inactive', '', '', 'no']
            rows.append(row_data)
        rows.append([str(pool_num + 1), 'openstack2', 'online',
                     '1', '0', '3573412790272', '256',
                     '3529432325160', '1693247906775', '26843545600',
                     '38203734097', '47', '80', 'auto', 'inactive', '', '',
                     'no'])
        rows.append([str(pool_num + 2), 'openstack3', 'offline',
                     '1', '0', '3573412790272', '128',
                     '3529432325160', '1693247906775', '26843545600',
                     '38203734097', '47', '80', 'auto', 'inactive', '', '',
                     'yes'])
        rows.append([str(pool_num + 3), 'hyperswap1', 'online',
                     '1', '0', '3573412790272', '256',
                     '3529432325160', '1693247906775', '26843545600',
                     '38203734097', '47', '80', 'auto', 'inactive', '1',
                     'site1', 'no'])
        rows.append([str(pool_num + 4), 'hyperswap2', 'online',
                     '1', '0', '3573412790272', '128',
                     '3529432325160', '1693247906775', '26843545600',
                     '38203734097', '47', '80', 'auto', 'inactive', '2',
                     'site2', 'no'])
        rows.append([str(pool_num + 5), 'dr_pool1', 'online',
                     '1', '0', '3573412790272', '128', '3529432325160',
                     '1693247906775', '26843545600', '38203734097', '47', '80',
                     'auto', 'inactive', '1', 'site1', 'yes'])
        rows.append([str(pool_num + 6), 'dr_pool2', 'online',
                     '1', '0', '3573412790272', '128', '3529432325160',
                     '1693247906775', '26843545600', '38203734097', '47', '80',
                     'auto', 'inactive', '2', 'site2', 'yes'])
        if 'obj' not in kwargs:
            return self._print_info_cmd(rows=rows, **kwargs)
        else:
            pool_name = kwargs['obj'].strip('\'\"')
            if pool_name == kwargs['obj']:
                raise exception.InvalidInput(
                    reason=_('obj missing quotes %s') % kwargs['obj'])
            elif pool_name in self._flags['storwize_svc_volpool_name']:
                for each_row in rows:
                    if pool_name in each_row:
                        row = each_row
                        break
            elif pool_name == 'openstack2':
                row = rows[-6]
            elif pool_name == 'openstack3':
                row = rows[-5]
            elif pool_name == 'hyperswap1':
                row = rows[-4]
            elif pool_name == 'hyperswap2':
                row = rows[-3]
            elif pool_name == 'dr_pool1':
                row = rows[-2]
            elif pool_name == 'dr_pool2':
                row = rows[-1]
            else:
                return self._errors['CMMVC5754E']
            objrows = []
            for idx, val in enumerate(rows[0]):
                objrows.append([val, row[idx]])
            if 'nohdr' in kwargs:
                for index in range(len(objrows)):
                    objrows[index] = ' '.join(objrows[index][1:])

            if 'delim' in kwargs:
                for index in range(len(objrows)):
                    objrows[index] = kwargs['delim'].join(objrows[index])

            return ('%s' % '\n'.join(objrows), '')

    def _get_mdiskgrp_id(self, mdiskgrp):
        grp_num = len(self._flags['storwize_svc_volpool_name'])
        if mdiskgrp in self._flags['storwize_svc_volpool_name']:
            for i in range(grp_num):
                if mdiskgrp == self._flags['storwize_svc_volpool_name'][i]:
                    return i + 1
        elif mdiskgrp == 'openstack2':
            return grp_num + 1
        elif mdiskgrp == 'openstack3':
            return grp_num + 2
        else:
            return None

    # Print mostly made-up stuff in the correct syntax
    def _cmd_lsnodecanister(self, **kwargs):
        rows = [None] * 3
        rows[0] = ['id', 'name', 'UPS_serial_number', 'WWNN', 'status',
                   'IO_group_id', 'IO_group_name', 'config_node',
                   'UPS_unique_id', 'hardware', 'iscsi_name', 'iscsi_alias',
                   'panel_name', 'enclosure_id', 'canister_id',
                   'enclosure_serial_number', 'site_id']
        rows[1] = ['1', 'node1', '', '123456789ABCDEF0', 'online', '0',
                   'io_grp0',
                   'yes', '123456789ABCDEF0', '100',
                   'iqn.1982-01.com.ibm:1234.sim.node1', '', '01-1', '1', '1',
                   '0123ABC', '1']
        rows[2] = ['2', 'node2', '', '123456789ABCDEF1', 'online', '1',
                   'io_grp0',
                   'no', '123456789ABCDEF1', '100',
                   'iqn.1982-01.com.ibm:1234.sim.node2', '', '01-2', '1', '2',
                   '0123ABC', '2']

        if self._next_cmd_error['lsnodecanister'] == 'header_mismatch':
            rows[0].pop(2)
            self._next_cmd_error['lsnodecanister'] = ''
        if self._next_cmd_error['lsnodecanister'] == 'remove_field':
            for row in rows:
                row.pop(0)
            self._next_cmd_error['lsnodecanister'] = ''

        return self._print_info_cmd(rows=rows, **kwargs)

    # Print information of every single node of SVC
    def _cmd_lsnode(self, **kwargs):
        node_infos = dict()
        node_infos['1'] = r'''id!1
name!node1
port_id!500507680210C744
port_status!active
port_speed!8Gb
port_id!500507680220C744
port_status!active
port_speed!8Gb
'''
        node_infos['2'] = r'''id!2
name!node2
port_id!500507680220C745
port_status!active
port_speed!8Gb
port_id!500507680230C745
port_status!inactive
port_speed!N/A
'''
        node_id = kwargs.get('node_id', None)
        stdout = node_infos.get(node_id, '')
        return stdout, ''

    # Print made up stuff for the ports
    def _cmd_lsportfc(self, **kwargs):
        node_1 = [None] * 7
        node_1[0] = ['id', 'fc_io_port_id', 'port_id', 'type',
                     'port_speed', 'node_id', 'node_name', 'WWPN',
                     'nportid', 'status', 'attachment']
        node_1[1] = ['0', '1', '1', 'fc', '8Gb', '1', 'node1',
                     '5005076802132ADE', '012E00', 'active', 'switch']
        node_1[2] = ['1', '2', '2', 'fc', '8Gb', '1', 'node1',
                     '5005076802232ADE', '012E00', 'active', 'switch']
        node_1[3] = ['2', '3', '3', 'fc', '8Gb', '1', 'node1',
                     '5005076802332ADE', '9B0600', 'active', 'switch']
        node_1[4] = ['3', '4', '4', 'fc', '8Gb', '1', 'node1',
                     '5005076802432ADE', '012A00', 'active', 'switch']
        node_1[5] = ['4', '5', '5', 'fc', '8Gb', '1', 'node1',
                     '5005076802532ADE', '014A00', 'active', 'switch']
        node_1[6] = ['5', '6', '4', 'ethernet', 'N/A', '1', 'node1',
                     '5005076802632ADE', '000000',
                     'inactive_unconfigured', 'none']

        node_2 = [None] * 7
        node_2[0] = ['id', 'fc_io_port_id', 'port_id', 'type',
                     'port_speed', 'node_id', 'node_name', 'WWPN',
                     'nportid', 'status', 'attachment']
        node_2[1] = ['6', '7', '7', 'fc', '8Gb', '2', 'node2',
                     '5005086802132ADE', '012E00', 'active', 'switch']
        node_2[2] = ['7', '8', '8', 'fc', '8Gb', '2', 'node2',
                     '5005086802232ADE', '012E00', 'active', 'switch']
        node_2[3] = ['8', '9', '9', 'fc', '8Gb', '2', 'node2',
                     '5005086802332ADE', '9B0600', 'active', 'switch']
        node_2[4] = ['9', '10', '10', 'fc', '8Gb', '2', 'node2',
                     '5005086802432ADE', '012A00', 'active', 'switch']
        node_2[5] = ['10', '11', '11', 'fc', '8Gb', '2', 'node2',
                     '5005086802532ADE', '014A00', 'active', 'switch']
        node_2[6] = ['11', '12', '12', 'ethernet', 'N/A', '2', 'node2',
                     '5005086802632ADE', '000000',
                     'inactive_unconfigured', 'none']
        node_infos = [node_1, node_2]
        node_id = int(kwargs['filtervalue'].split('=')[1]) - 1

        return self._print_info_cmd(rows=node_infos[node_id], **kwargs)

    def _cmd_lstargetportfc(self, **kwargs):
        ports = [None] * 17
        ports[0] = ['id', 'WWPN', 'WWNN', 'port_id', 'owning_node_id',
                    'current_node_id', 'nportid', 'host_io_permitted',
                    'virtualized', 'fc_io_port_id']
        ports[1] = ['0', '5005076801106CFE', '5005076801106CFE', '1', '1',
                    '1', '042200', 'no', 'no', '']
        ports[2] = ['0', '5005076801996CFE', '5005076801106CFE', '1', '1',
                    '1', '042200', 'yes', 'yes', '']
        ports[3] = ['0', '5005076801206CFE', '5005076801106CFE', '2', '1',
                    '1', '042200', 'no', 'no', '']
        ports[4] = ['0', '5005076801A96CFE', '5005076801106CFE', '2', '1',
                    '1', '042200', 'yes', 'yes', '']
        ports[5] = ['0', '5005076801306CFE', '5005076801106CFE', '3', '1',
                    '', '042200', 'no', 'no', '']
        ports[6] = ['0', '5005076801B96CFE', '5005076801106CFE', '3', '1',
                    '', '042200', 'yes', 'yes', '']
        ports[7] = ['0', '5005076801406CFE', '5005076801106CFE', '4', '1',
                    '', '042200', 'no', 'no', '']
        ports[8] = ['0', '5005076801C96CFE', '5005076801106CFE', '4', '1',
                    '', '042200', 'yes', 'yes', '']
        ports[9] = ['0', '5005076801101806', '5005076801101806', '1', '2',
                    '2', '042200', 'no', 'no', '']
        ports[10] = ['0', '5005076801991806', '5005076801101806', '1', '2',
                     '2', '042200', 'yes', 'yes', '']
        ports[11] = ['0', '5005076801201806', '5005076801101806', '2', '2',
                     '2', '042200', 'no', 'no', '']
        ports[12] = ['0', '5005076801A91806', '5005076801101806', '2', '2',
                     '2', '042200', 'yes', 'yes', '']
        ports[13] = ['0', '5005076801301806', '5005076801101806', '3', '2',
                     '', '042200', 'no', 'no', '']
        ports[14] = ['0', '5005076801B91806', '5005076801101806', '3', '2',
                     '', '042200', 'yes', 'yes', '']
        ports[15] = ['0', '5005076801401806', '5005076801101806', '4', '2',
                     '', '042200', 'no', 'no', '']
        ports[16] = ['0', '5005076801C91806', '5005076801101806', '4', '2',
                     '', '042200', 'yes', 'yes', '']

        if 'filtervalue' in kwargs:
            rows = []
            rows.append(['id', 'WWPN', 'WWNN', 'port_id', 'owning_node_id',
                         'current_node_id', 'nportid', 'host_io_permitted',
                         'virtualized', 'fc_io_port_id'])

            if ':' in kwargs['filtervalue']:
                filter1 = kwargs['filtervalue'].split(':')[0]
                filter2 = kwargs['filtervalue'].split(':')[1]
                value1 = filter1.split('=')[1]
                value2 = filter2.split('=')[1]
                for v in ports:
                    if str(v[5]) == value1 and str(v[7]) == value2:
                        rows.append(v)
            else:
                value = kwargs['filtervalue'].split('=')[1]
                for v in ports:
                    if str(v[5]) == value:
                        rows.append(v)
        else:
            rows = ports
        return self._print_info_cmd(rows=rows, **kwargs)

    # Print mostly made-up stuff in the correct syntax
    def _cmd_lsfcportsetmember(self, **kwargs):
        rows = [None] * 7
        rows[0] = ['id', 'fc_io_port_id', 'portset_id', 'portset_name',
                   'owner_id', 'owner_name']
        rows[1] = ['0', '5', '6', 'portset6', '', '']
        rows[2] = ['1', '5', '64', 'portset64', '', '']
        rows[3] = ['2', '6', '6', 'portset6', '', '']
        rows[4] = ['3', '6', '64', 'portset64', '', '']
        rows[5] = ['4', '7', '64', 'portset64', '', '']
        rows[6] = ['5', '8', '64', 'portset64', '', '']

        if self._next_cmd_error['lsfcportsetmember'] == 'header_mismatch':
            rows[0].pop(2)
            self._next_cmd_error['lsfcportsetmember'] = ''
        if self._next_cmd_error['lsfcportsetmember'] == 'remove_field':
            for row in rows:
                row.pop(1)
            self._next_cmd_error['lsfcportsetmember'] = ''

        return self._print_info_cmd(rows=rows, **kwargs)

    # Print mostly made-up stuff in the correct syntax
    def _cmd_lsip(self, **kwargs):
        ports = [None] * 9
        ports[0] = ['id', 'node_id', 'node_name', 'port_id', 'portset_id',
                    'portset_name', 'IP_address', 'prefix', 'vlan', 'gateway',
                    'owner_id', 'owner_name']
        ports[1] = ['0', '1', 'node1', '5', '0', 'portset0', '1.234.50.11',
                    '24', '1001', '', '', '']
        ports[2] = ['1', '1', 'node1', '6', '4', 'portset4', '1.234.51.11',
                    '24', '1002', '', '', '']
        ports[3] = ['2', '1', 'node1', '7', '5', 'portset5', '1.234.52.11',
                    '24', '1003', '', '', '']
        ports[4] = ['3', '1', 'node1', '8', '6', 'portset6', '1.234.53.11',
                    '24', '1004', '', '', '']
        ports[5] = ['4', '2', 'node2', '5', '0', 'portset0', '1.234.54.11',
                    '24', '1005', '', '', '']
        ports[6] = ['5', '2', 'node2', '6', '4', 'portset4', '1.234.55.11',
                    '24', '1006', '', '', '']
        ports[7] = ['6', '2', 'node2', '7', '5', 'portset5', '1.234.56.11',
                    '24', '1007', '', '', '']
        ports[8] = ['7', '2', 'node2', '8', '6', 'portset6', '1.234.57.11',
                    '24', '1008', '', '', '']

        if 'filtervalue' in kwargs:
            rows = []
            rows.append(['id', 'node_id', 'node_name', 'port_id', 'portset_id',
                         'portset_name', 'IP_address', 'prefix', 'vlan',
                         'gateway', 'owner_id', 'owner_name'])

            value = kwargs['filtervalue'].split('=')[1]
            for v in ports:
                if str(v[5]) == value:
                    rows.append(v)
        else:
            rows = ports

        if self._next_cmd_error['lsip'] == 'header_mismatch':
            rows[0].pop(2)
            self._next_cmd_error['lsip'] = ''
        if self._next_cmd_error['lsip'] == 'remove_field':
            for row in rows:
                row.pop(1)
            self._next_cmd_error['lsip'] = ''
        return self._print_info_cmd(rows=rows, **kwargs)

    # Print mostly made-up stuff in the correct syntax
    def _cmd_lsportip(self, **kwargs):
        if self._next_cmd_error['lsportip'] == 'ip_no_config':
            self._next_cmd_error['lsportip'] = ''
            ip_addr1 = ''
            ip_addr2 = ''
            gw = ''
        else:
            ip_addr1 = '1.234.56.78'
            ip_addr2 = '1.234.56.79'
            ip_addr3 = '1.234.56.80'
            ip_addr4 = '1.234.56.81'
            gw = '1.234.56.1'

        rows = [None] * 17
        rows[0] = ['id', 'node_id', 'node_name', 'IP_address', 'mask',
                   'gateway', 'IP_address_6', 'prefix_6', 'gateway_6', 'MAC',
                   'duplex', 'state', 'speed', 'failover', 'link_state']
        rows[1] = ['1', '1', 'node1', ip_addr1, '255.255.255.0',
                   gw, '', '', '', '01:23:45:67:89:00', 'Full',
                   'online', '1Gb/s', 'no', 'active']
        rows[2] = ['1', '1', 'node1', '', '', '', '', '', '',
                   '01:23:45:67:89:00', 'Full', 'online', '1Gb/s', 'yes', '']
        rows[3] = ['2', '1', 'node1', ip_addr3, '255.255.255.0',
                   gw, '', '', '', '01:23:45:67:89:01', 'Full',
                   'configured', '1Gb/s', 'no', 'active']
        rows[4] = ['2', '1', 'node1', '', '', '', '', '', '',
                   '01:23:45:67:89:01', 'Full', 'unconfigured', '1Gb/s',
                   'yes', 'inactive']
        rows[5] = ['3', '1', 'node1', '', '', '', '', '', '', '', '',
                   'unconfigured', '', 'no', '']
        rows[6] = ['3', '1', 'node1', '', '', '', '', '', '', '', '',
                   'unconfigured', '', 'yes', '']
        rows[7] = ['4', '1', 'node1', '', '', '', '', '', '', '', '',
                   'unconfigured', '', 'no', '']
        rows[8] = ['4', '1', 'node1', '', '', '', '', '', '', '', '',
                   'unconfigured', '', 'yes', '']
        rows[9] = ['1', '2', 'node2', ip_addr2, '255.255.255.0',
                   gw, '', '', '', '01:23:45:67:89:02', 'Full',
                   'online', '1Gb/s', 'no', '']
        rows[10] = ['1', '2', 'node2', '', '', '', '', '', '',
                    '01:23:45:67:89:02', 'Full', 'online', '1Gb/s', 'yes', '']
        rows[11] = ['2', '2', 'node2', ip_addr4, '255.255.255.0',
                    gw, '', '', '', '01:23:45:67:89:03', 'Full',
                    'configured', '1Gb/s', 'no', 'inactive']
        rows[12] = ['2', '2', 'node2', '', '', '', '', '', '',
                    '01:23:45:67:89:03', 'Full', 'unconfigured', '1Gb/s',
                    'yes', '']
        rows[13] = ['3', '2', 'node2', '', '', '', '', '', '', '', '',
                    'unconfigured', '', 'no', '']
        rows[14] = ['3', '2', 'node2', '', '', '', '', '', '', '', '',
                    'unconfigured', '', 'yes', '']
        rows[15] = ['4', '2', 'node2', '', '', '', '', '', '', '', '',
                    'unconfigured', '', 'no', '']
        rows[16] = ['4', '2', 'node2', '', '', '', '', '', '', '', '',
                    'unconfigured', '', 'yes', '']

        if self._next_cmd_error['lsportip'] == 'header_mismatch':
            rows[0].pop(2)
            self._next_cmd_error['lsportip'] = ''
        if self._next_cmd_error['lsportip'] == 'remove_field':
            for row in rows:
                row.pop(1)
            self._next_cmd_error['lsportip'] = ''

        return self._print_info_cmd(rows=rows, **kwargs)

    def _cmd_lsfabric(self, **kwargs):
        if self._next_cmd_error['lsfabric'] == 'no_hosts':
            return ('', '')
        host_name = kwargs['host'].strip('\'\"') if 'host' in kwargs else None
        target_wwpn = kwargs['wwpn'] if 'wwpn' in kwargs else None
        host_infos = []
        for hv in self._hosts_list.values():
            if (not host_name) or (hv['host_name'] == host_name):
                if not target_wwpn or target_wwpn in hv['wwpns']:
                    host_infos.append(hv)
                    break
        if not len(host_infos):
            return ('', '')
        rows = []
        rows.append(['remote_wwpn', 'remote_nportid', 'id', 'node_name',
                     'local_wwpn', 'local_port', 'local_nportid', 'state',
                     'name', 'cluster_name', 'type'])
        for host_info in host_infos:
            for wwpn in host_info['wwpns']:
                rows.append([wwpn, '123456', host_info['id'], 'nodeN',
                             'AABBCCDDEEFF0011', '1', '0123ABC', 'active',
                             host_info['host_name'], '', 'host'])
        if self._next_cmd_error['lsfabric'] == 'header_mismatch':
            rows[0].pop(0)
            self._next_cmd_error['lsfabric'] = ''
        if self._next_cmd_error['lsfabric'] == 'remove_field':
            for row in rows:
                row.pop(0)
            self._next_cmd_error['lsfabric'] = ''
        if self._next_cmd_error['lsfabric'] == 'remove_rows':
            rows = []
        return self._print_info_cmd(rows=rows, **kwargs)

    # Create a vdisk
    def _cmd_mkvdisk(self, **kwargs):
        # We only save the id/uid, name, and size - all else will be made up
        volume_info = {}
        volume_info['id'] = self._find_unused_id(self._volumes_list)
        volume_info['uid'] = ('ABCDEF' * 3) + ('0' * 14) + volume_info['id']

        mdiskgrp = kwargs['mdiskgrp'].strip('\'\"')
        sec_pool = None
        is_mirror_vol = False
        if 'copies' in kwargs:
            # it is a mirror volume
            pool_split = mdiskgrp.split(':')
            if len(pool_split) != 2:
                raise exception.InvalidInput(
                    reason=_('mdiskgrp %s is invalid for mirror '
                             'volume') % kwargs['mdiskgrp'])
            else:
                is_mirror_vol = True
                mdiskgrp = pool_split[0]
                sec_pool = pool_split[1]

        if mdiskgrp == kwargs['mdiskgrp']:
            raise exception.InvalidInput(
                reason=_('mdiskgrp missing quotes %s') % kwargs['mdiskgrp'])
        mdiskgrp_id = self._get_mdiskgrp_id(mdiskgrp)
        sec_pool_id = self._get_mdiskgrp_id(sec_pool)
        volume_info['mdisk_grp_name'] = mdiskgrp
        volume_info['mdisk_grp_id'] = str(mdiskgrp_id)

        if 'name' in kwargs:
            volume_info['name'] = kwargs['name'].strip('\'\"')
        else:
            volume_info['name'] = 'vdisk' + volume_info['id']

        # Assume size and unit are given, store it in bytes
        capacity = int(kwargs['size'])
        unit = kwargs['unit']
        volume_info['capacity'] = self._convert_units_bytes(capacity, unit)
        volume_info['IO_group_id'] = kwargs['iogrp']
        volume_info['IO_group_name'] = 'io_grp%s' % kwargs['iogrp']
        volume_info['RC_name'] = ''
        volume_info['RC_id'] = ''

        if 'easytier' in kwargs:
            if kwargs['easytier'] == 'on':
                volume_info['easy_tier'] = 'on'
            else:
                volume_info['easy_tier'] = 'off'

        if 'rsize' in kwargs:
            volume_info['formatted'] = 'no'
            # Fake numbers
            volume_info['used_capacity'] = '786432'
            volume_info['real_capacity'] = '21474816'
            volume_info['free_capacity'] = '38219264'
            if 'warning' in kwargs:
                volume_info['warning'] = kwargs['warning'].rstrip('%')
            else:
                volume_info['warning'] = '80'
            if 'autoexpand' in kwargs:
                volume_info['autoexpand'] = 'on'
            else:
                volume_info['autoexpand'] = 'off'
            if 'grainsize' in kwargs:
                volume_info['grainsize'] = kwargs['grainsize']
            else:
                volume_info['grainsize'] = '32'
            if 'compressed' in kwargs:
                volume_info['compressed_copy'] = 'yes'
            else:
                volume_info['compressed_copy'] = 'no'
        else:
            volume_info['used_capacity'] = volume_info['capacity']
            volume_info['real_capacity'] = volume_info['capacity']
            volume_info['free_capacity'] = '0'
            volume_info['warning'] = ''
            volume_info['autoexpand'] = ''
            volume_info['grainsize'] = ''
            volume_info['compressed_copy'] = 'no'
            volume_info['formatted'] = 'yes'
            if 'nofmtdisk' in kwargs:
                if kwargs['nofmtdisk']:
                    volume_info['formatted'] = 'no'

        vol_cp = {'id': '0',
                  'status': 'online',
                  'sync': 'yes',
                  'primary': 'yes',
                  'mdisk_grp_id': str(mdiskgrp_id),
                  'mdisk_grp_name': mdiskgrp,
                  'easy_tier': (volume_info[
                      'easy_tier'] if 'easy_tier' in volume_info else 'on'),
                  'compressed_copy': volume_info['compressed_copy']}
        volume_info['copies'] = {'0': vol_cp}
        if is_mirror_vol:
            vol_cp1 = {'id': '1',
                       'status': 'online',
                       'sync': 'yes',
                       'primary': 'no',
                       'mdisk_grp_id': str(sec_pool_id),
                       'mdisk_grp_name': sec_pool,
                       'easy_tier': (volume_info['easy_tier']
                                     if 'easy_tier' in volume_info else 'on'),
                       'compressed_copy': volume_info['compressed_copy']}
            volume_info['copies']['1'] = vol_cp1

        if volume_info['name'] in self._volumes_list:
            return self._errors['CMMVC6035E']
        else:
            self._volumes_list[volume_info['name']] = volume_info
            return ('Virtual Disk, id [%s], successfully created' %
                    (volume_info['id']), '')

    # Delete a vdisk
    def _cmd_rmvdisk(self, **kwargs):
        force = True if 'force' in kwargs else False

        if 'force' not in kwargs and 'force_unmap' in kwargs:
            force_unmap = True
        else:
            force_unmap = False

        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')

        if vol_name not in self._volumes_list:
            return self._errors['CMMVC5753E']

        if not force and not force_unmap:
            for mapping in self._mappings_list.values():
                if mapping['vol'] == vol_name:
                    return self._errors['CMMVC5840E']
            for fcmap in self._fcmappings_list.values():
                if ((fcmap['source'] == vol_name) or
                        (fcmap['target'] == vol_name)):
                    return self._errors['CMMVC5840E']

        del self._volumes_list[vol_name]
        return ('', '')

    def _cmd_expandvdisksize(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')

        # Assume unit is gb
        if 'size' not in kwargs:
            return self._errors['CMMVC5707E']
        size = int(kwargs['size'])

        if vol_name not in self._volumes_list:
            return self._errors['CMMVC5753E']

        vol = self._volumes_list[kwargs['obj']]
        if self._next_cmd_error['expandvdisksize'] == 'fast_formatting':
            if vol['RC_name']:
                rcrel = self._rcrelationship_list[vol['RC_name']]
                if rcrel.get('copy_type', None):
                    return self._errors['CMMVC9201E']
            return self._errors['CMMVC8587E']

        curr_size = int(self._volumes_list[vol_name]['capacity'])
        addition = size * units.Gi
        self._volumes_list[vol_name]['capacity'] = (
            str(curr_size + addition))
        return ('', '')

    def _get_fcmap_info(self, vol_name):
        ret_vals = {
            'fc_id': '',
            'fc_name': '',
            'fc_map_count': '0',
        }
        for fcmap in self._fcmappings_list.values():
            if ((fcmap['source'] == vol_name) or
                    (fcmap['target'] == vol_name)):
                ret_vals['fc_id'] = fcmap['id']
                ret_vals['fc_name'] = fcmap['name']
                ret_vals['fc_map_count'] = '1'
        return ret_vals

    # List information about vdisks
    def _cmd_lsvdisk(self, **kwargs):
        rows = []
        rows.append(['id', 'name', 'IO_group_id', 'IO_group_name',
                     'status', 'mdisk_grp_id', 'mdisk_grp_name',
                     'capacity', 'type', 'FC_id', 'FC_name', 'RC_id',
                     'RC_name', 'vdisk_UID', 'fc_map_count', 'copy_count',
                     'fast_write_state', 'se_copy_count', 'RC_change'])

        for vol in self._volumes_list.values():
            if (('filtervalue' not in kwargs) or
               (kwargs['filtervalue'] == 'name=' + vol['name']) or
               (kwargs['filtervalue'] == 'vdisk_UID=' + vol['uid'])):
                fcmap_info = self._get_fcmap_info(vol['name'])

                if 'bytes' in kwargs:
                    cap = self._convert_bytes_units(vol['capacity'])
                else:
                    cap = vol['capacity']
                rows.append([str(vol['id']), vol['name'],
                             vol['IO_group_id'],
                             vol['IO_group_name'], 'online', '0',
                             _get_test_pool(),
                             cap, 'striped',
                             fcmap_info['fc_id'], fcmap_info['fc_name'],
                             '', '', vol['uid'],
                             fcmap_info['fc_map_count'], '1', 'empty',
                             '1', 'no'])
        if 'obj' not in kwargs:
            return self._print_info_cmd(rows=rows, **kwargs)
        else:
            if kwargs['obj'] not in self._volumes_list:
                return self._errors['CMMVC5754E']
            vol = self._volumes_list[kwargs['obj']]
            fcmap_info = self._get_fcmap_info(vol['name'])
            cap = vol['capacity']
            cap_u = vol['used_capacity']
            cap_r = vol['real_capacity']
            cap_f = vol['free_capacity']
            if 'bytes' not in kwargs:
                for item in [cap, cap_u, cap_r, cap_f]:
                    item = self._convert_bytes_units(item)
            rows = []

            rows.append(['id', str(vol['id'])])
            rows.append(['name', vol['name']])
            rows.append(['IO_group_id', vol['IO_group_id']])
            rows.append(['IO_group_name', vol['IO_group_name']])
            rows.append(['status', 'online'])
            rows.append(['capacity', cap])
            rows.append(['formatted', vol['formatted']])
            rows.append(['mdisk_id', ''])
            rows.append(['mdisk_name', ''])
            rows.append(['FC_id', fcmap_info['fc_id']])
            rows.append(['FC_name', fcmap_info['fc_name']])
            rows.append(['RC_id', vol['RC_id']])
            rows.append(['RC_name', vol['RC_name']])
            rows.append(['vdisk_UID', vol['uid']])
            rows.append(['throttling', '0'])

            if self._next_cmd_error['lsvdisk'] == 'blank_pref_node':
                rows.append(['preferred_node_id', ''])
                self._next_cmd_error['lsvdisk'] = ''
            elif self._next_cmd_error['lsvdisk'] == 'no_pref_node':
                self._next_cmd_error['lsvdisk'] = ''
            else:
                rows.append(['preferred_node_id', '1'])
            rows.append(['fast_write_state', 'empty'])
            rows.append(['cache', 'readwrite'])
            rows.append(['udid', ''])
            rows.append(['fc_map_count', fcmap_info['fc_map_count']])
            rows.append(['sync_rate', '50'])
            rows.append(['copy_count', '1'])
            rows.append(['se_copy_count', '0'])
            rows.append(['mirror_write_priority', 'latency'])
            rows.append(['RC_change', 'no'])

            for copy in vol['copies'].values():
                rows.append(['copy_id', copy['id']])
                rows.append(['status', copy['status']])
                rows.append(['primary', copy['primary']])
                rows.append(['mdisk_grp_id', copy['mdisk_grp_id']])
                rows.append(['mdisk_grp_name', copy['mdisk_grp_name']])
                rows.append(['type', 'striped'])
                rows.append(['used_capacity', cap_u])
                rows.append(['real_capacity', cap_r])
                rows.append(['free_capacity', cap_f])
                rows.append(['easy_tier', copy['easy_tier']])
                rows.append(['compressed_copy', copy['compressed_copy']])
                rows.append(['autoexpand', vol['autoexpand']])
                rows.append(['warning', vol['warning']])
                rows.append(['grainsize', vol['grainsize']])

            if 'nohdr' in kwargs:
                for index in range(len(rows)):
                    rows[index] = ' '.join(rows[index][1:])

            if 'delim' in kwargs:
                for index in range(len(rows)):
                    rows[index] = kwargs['delim'].join(rows[index])
            return ('%s' % '\n'.join(rows), '')

    def _cmd_lsiogrp(self, **kwargs):
        rows = [None] * 6
        rows[0] = ['id', 'name', 'node_count', 'vdisk_count', 'host_count']
        rows[1] = ['0', 'io_grp0', '2', '0', '4']
        rows[2] = ['1', 'io_grp1', '2', '0', '4']
        rows[3] = ['2', 'io_grp2', '0', '0', '4']
        rows[4] = ['3', 'io_grp3', '0', '0', '4']
        rows[5] = ['4', 'recovery_io_grp', '0', '0', '0']
        return self._print_info_cmd(rows=rows, **kwargs)

    def _add_port_to_host(self, host_info, **kwargs):
        if 'iscsiname' in kwargs:
            added_key = 'iscsi_names'
            added_val = kwargs['iscsiname'].strip('\'\"')
        elif 'hbawwpn' in kwargs:
            added_key = 'wwpns'
            added_val = kwargs['hbawwpn'].strip('\'\"')
        else:
            return self._errors['CMMVC5707E']

        host_info[added_key].append(added_val)

        for v in self._hosts_list.values():
            if v['id'] == host_info['id']:
                continue
            for port in v[added_key]:
                if port == added_val:
                    error = 'CMMVC6035E'
                    if 'iscsiname' in kwargs:
                        error = 'CMMVC6578E'
                    return self._errors[error]
        return ('', '')

    # Make a host
    def _cmd_mkhost(self, **kwargs):
        host_info = {}
        host_info['id'] = self._find_unused_id(self._hosts_list)

        if 'name' in kwargs:
            host_name = kwargs['name'].strip('\'\"')
        else:
            host_name = 'host' + str(host_info['id'])

        if self._is_invalid_name(host_name):
            return self._errors['CMMVC6527E']

        if host_name in self._hosts_list:
            return self._errors['CMMVC6035E']

        host_info['host_name'] = host_name
        host_info['iscsi_names'] = []
        host_info['wwpns'] = []

        if 'site' in kwargs:
            host_info['site_name'] = kwargs['site'].strip('\'\"')
        else:
            host_info['site_name'] = ''
        if 'portset' in kwargs:
            host_info['portset_name'] = kwargs['portset'].strip('\'\"')
        else:
            host_info['portset_name'] = ''
        out, err = self._add_port_to_host(host_info, **kwargs)
        if not len(err):
            self._hosts_list[host_name] = host_info
            return ('Host, id [%s], successfully created' %
                    (host_info['id']), '')
        else:
            return (out, err)

    # Add ports to an existing host
    def _cmd_addhostport(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        host_name = kwargs['obj'].strip('\'\"')

        if host_name not in self._hosts_list:
            return self._errors['CMMVC5753E']

        host_info = self._hosts_list[host_name]
        return self._add_port_to_host(host_info, **kwargs)

    # Change host properties
    def _cmd_chhost(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        host_name = kwargs['obj'].strip('\'\"')

        if host_name not in self._hosts_list:
            return self._errors['CMMVC5753E']

        if 'chapsecret' in kwargs:
            secret = kwargs['chapsecret'].strip('\'\"')
            self._hosts_list[host_name]['chapsecret'] = secret

        if 'site' in kwargs:
            site_name = kwargs['site'].strip('\'\"')
            self._hosts_list[host_name]['site_name'] = site_name

        if 'chapsecret' not in kwargs and 'site' not in kwargs:
            return self._errors['CMMVC5707E']

        return ('', '')

    # Remove a host
    def _cmd_rmhost(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']

        host_name = kwargs['obj'].strip('\'\"')
        if host_name not in self._hosts_list:
            return self._errors['CMMVC5753E']

        for v in self._mappings_list.values():
            if (v['host'] == host_name):
                return self._errors['CMMVC5871E']

        del self._hosts_list[host_name]
        return ('', '')

    # List information about hosts
    def _cmd_lshost(self, **kwargs):
        if 'obj' not in kwargs:
            rows = []
            rows.append(['id', 'name', 'port_count', 'iogrp_count',
                         'status', 'site_name'])

            found = False
            # Sort hosts by names to give predictable order for tests
            # depend on it.
            for host_name in sorted(self._hosts_list.keys()):
                host = self._hosts_list[host_name]
                filterstr = 'name=' + host['host_name']
                if (('filtervalue' not in kwargs) or
                        (kwargs['filtervalue'] == filterstr)):
                    rows.append([host['id'], host['host_name'], '1', '4',
                                'offline', host['site_name']])
                    found = True
            if found:
                return self._print_info_cmd(rows=rows, **kwargs)
            else:
                return ('', '')
        else:
            if self._next_cmd_error['lshost'] == 'missing_host':
                self._next_cmd_error['lshost'] = ''
                return self._errors['CMMVC5754E']
            elif self._next_cmd_error['lshost'] == 'bigger_troubles':
                return self._errors['CMMVC6527E']
            host_name = kwargs['obj'].strip('\'\"')
            if host_name not in self._hosts_list:
                return self._errors['CMMVC5754E']
            if (self._next_cmd_error['lshost'] == 'fail_fastpath' and
                    host_name == 'DifferentHost'):
                return self._errors['CMMVC5701E']
            host = self._hosts_list[host_name]
            rows = []
            rows.append(['id', host['id']])
            rows.append(['name', host['host_name']])
            rows.append(['port_count', '1'])
            rows.append(['type', 'generic'])
            rows.append(['mask', '1111'])
            rows.append(['iogrp_count', '4'])
            rows.append(['status', 'online'])
            rows.append(['site_name', host['site_name']])
            for port in host['iscsi_names']:
                rows.append(['iscsi_name', port])
                rows.append(['node_logged_in_count', '0'])
                rows.append(['state', 'offline'])
            for port in host['wwpns']:
                rows.append(['WWPN', port])
                rows.append(['node_logged_in_count', '0'])
                rows.append(['state', 'active'])

            if 'nohdr' in kwargs:
                for index in range(len(rows)):
                    rows[index] = ' '.join(rows[index][1:])

            if 'delim' in kwargs:
                for index in range(len(rows)):
                    rows[index] = kwargs['delim'].join(rows[index])

            return ('%s' % '\n'.join(rows), '')

    # List iSCSI authorization information about hosts
    def _cmd_lsiscsiauth(self, **kwargs):
        if self._next_cmd_error['lsiscsiauth'] == 'no_info':
            self._next_cmd_error['lsiscsiauth'] = ''
            return ('', '')
        rows = []
        rows.append(['type', 'id', 'name', 'iscsi_auth_method',
                     'iscsi_chap_secret'])

        for host in self._hosts_list.values():
            method = 'none'
            secret = ''
            if 'chapsecret' in host:
                method = 'chap'
                secret = host['chapsecret']
            rows.append(['host', host['id'], host['host_name'], method,
                         secret])
        return self._print_info_cmd(rows=rows, **kwargs)

    # Create a vdisk-host mapping
    def _cmd_mkvdiskhostmap(self, **kwargs):
        mapping_info = {}
        mapping_info['id'] = self._find_unused_id(self._mappings_list)
        if 'host' not in kwargs:
            return self._errors['CMMVC5707E']
        mapping_info['host'] = kwargs['host'].strip('\'\"')

        if 'scsi' in kwargs:
            mapping_info['lun'] = kwargs['scsi'].strip('\'\"')
        else:
            mapping_info['lun'] = mapping_info['id']

        if 'obj' not in kwargs:
            return self._errors['CMMVC5707E']
        mapping_info['vol'] = kwargs['obj'].strip('\'\"')

        if mapping_info['vol'] not in self._volumes_list:
            return self._errors['CMMVC5753E']

        if mapping_info['host'] not in self._hosts_list:
            return self._errors['CMMVC5754E']

        if mapping_info['vol'] in self._mappings_list:
            return self._errors['CMMVC6071E']

        for v in self._mappings_list.values():
            if ((v['host'] == mapping_info['host']) and
                    (v['lun'] == mapping_info['lun'])):
                return self._errors['CMMVC5879E']

        for v in self._mappings_list.values():
            if (v['vol'] == mapping_info['vol']) and ('force' not in kwargs):
                return self._errors['CMMVC6071E']

        self._mappings_list[mapping_info['id']] = mapping_info
        return ('Virtual Disk to Host map, id [%s], successfully created'
                % (mapping_info['id']), '')

    # Delete a vdisk-host mapping
    def _cmd_rmvdiskhostmap(self, **kwargs):
        if 'host' not in kwargs:
            return self._errors['CMMVC5707E']
        host = kwargs['host'].strip('\'\"')

        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol = kwargs['obj'].strip('\'\"')

        mapping_ids = []
        for v in self._mappings_list.values():
            if v['vol'] == vol:
                mapping_ids.append(v['id'])
        if not mapping_ids:
            return self._errors['CMMVC5753E']

        this_mapping = None
        for mapping_id in mapping_ids:
            if self._mappings_list[mapping_id]['host'] == host:
                this_mapping = mapping_id
        if this_mapping is None:
            return self._errors['CMMVC5753E']

        del self._mappings_list[this_mapping]
        return ('', '')

    # List information about host->vdisk mappings
    def _cmd_lshostvdiskmap(self, **kwargs):
        host_name = kwargs['obj'].strip('\'\"')

        if host_name not in self._hosts_list:
            return self._errors['CMMVC5754E']

        rows = []
        rows.append(['id', 'name', 'SCSI_id', 'vdisk_id', 'vdisk_name',
                     'vdisk_UID'])

        for mapping in self._mappings_list.values():
            if (host_name == '') or (mapping['host'] == host_name):
                volume = self._volumes_list[mapping['vol']]
                rows.append([mapping['id'], mapping['host'],
                            mapping['lun'], volume['id'],
                            volume['name'], volume['uid']])

        return self._print_info_cmd(rows=rows, **kwargs)

    # List information about vdisk->host mappings
    def _cmd_lsvdiskhostmap(self, **kwargs):
        mappings_found = 0
        vdisk_name = kwargs['obj'].strip('\'\"')

        if vdisk_name not in self._volumes_list:
            return self._errors['CMMVC5753E']

        rows = []
        rows.append(['id name', 'SCSI_id', 'host_id', 'host_name', 'vdisk_UID',
                     'IO_group_id', 'IO_group_name'])

        for mapping in self._mappings_list.values():
            if (mapping['vol'] == vdisk_name):
                mappings_found += 1
                volume = self._volumes_list[mapping['vol']]
                host = self._hosts_list[mapping['host']]
                rows.append([volume['id'], mapping['lun'], host['id'],
                            host['host_name'], volume['uid'],
                            volume['IO_group_id'], volume['IO_group_name']])

        if mappings_found:
            return self._print_info_cmd(rows=rows, **kwargs)
        else:
            return ('', '')

    # Create a FlashCopy mapping
    def _cmd_mkfcmap(self, **kwargs):
        source = ''
        target = ''
        copyrate = kwargs['copyrate'] if 'copyrate' in kwargs else '50'
        cleanrate = kwargs['cleanrate'] if 'clean_rate' in kwargs else '50'

        if 'source' not in kwargs:
            return self._errors['CMMVC5707E']
        source = kwargs['source'].strip('\'\"')
        if source not in self._volumes_list:
            return self._errors['CMMVC5754E']

        if 'target' not in kwargs:
            return self._errors['CMMVC5707E']
        target = kwargs['target'].strip('\'\"')
        if target not in self._volumes_list:
            return self._errors['CMMVC5754E']

        if source == target:
            return self._errors['CMMVC6303E']

        if (self._volumes_list[source]['capacity'] !=
                self._volumes_list[target]['capacity']):
            return self._errors['CMMVC5754E']

        fcmap_info = {}
        fcmap_info['source'] = source
        fcmap_info['target'] = target
        fcmap_info['id'] = self._find_unused_id(self._fcmappings_list)
        fcmap_info['name'] = 'fcmap' + fcmap_info['id']
        fcmap_info['copyrate'] = copyrate
        fcmap_info['cleanrate'] = cleanrate
        fcmap_info['progress'] = '0'
        fcmap_info['autodelete'] = True if 'autodelete' in kwargs else False
        fcmap_info['status'] = 'idle_or_copied'
        fcmap_info['rc_controlled'] = 'no'

        # Add fcmap to consistency group
        if 'consistgrp' in kwargs:
            consistgrp = kwargs['consistgrp']

            # if is digit, assume is cg id, else is cg name
            cg_id = 0
            if not consistgrp.isdigit():
                for consistgrp_key in self._fcconsistgrp_list.keys():
                    if (self._fcconsistgrp_list[consistgrp_key]['name']
                            == consistgrp):
                        cg_id = consistgrp_key
                        fcmap_info['consistgrp'] = consistgrp_key
                        break
            else:
                if int(consistgrp) in self._fcconsistgrp_list.keys():
                    cg_id = int(consistgrp)

            # If can't find exist consistgrp id, return not exist error
            if not cg_id:
                return self._errors['CMMVC5754E']

            fcmap_info['consistgrp'] = cg_id
            # Add fcmap to consistgrp
            self._fcconsistgrp_list[cg_id]['fcmaps'][fcmap_info['id']] = (
                fcmap_info['name'])
            self._fc_cg_state_transition('add',
                                         self._fcconsistgrp_list[cg_id])

        self._fcmappings_list[fcmap_info['id']] = fcmap_info

        return ('FlashCopy Mapping, id [' + fcmap_info['id'] +
                '], successfully created', '')

    def _cmd_prestartfcmap(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']

        if self._next_cmd_error['prestartfcmap'] == 'bad_id':
            id_num = -1
            self._next_cmd_error['prestartfcmap'] = ''

        try:
            fcmap = self._fcmappings_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        return self._state_transition('prepare', fcmap)

    def _cmd_startfcmap(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']

        if self._next_cmd_error['startfcmap'] == 'bad_id':
            id_num = -1
            self._next_cmd_error['startfcmap'] = ''

        try:
            fcmap = self._fcmappings_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        return self._state_transition('start', fcmap)

    def _cmd_stopfcmap(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']

        try:
            fcmap = self._fcmappings_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        return self._state_transition('stop', fcmap)

    def _cmd_rmfcmap(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']
        force = True if 'force' in kwargs else False

        if self._next_cmd_error['rmfcmap'] == 'bad_id':
            id_num = -1
            self._next_cmd_error['rmfcmap'] = ''

        try:
            fcmap = self._fcmappings_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        function = 'delete_force' if force else 'delete'
        ret = self._state_transition(function, fcmap)
        if fcmap['status'] == 'end':
            del self._fcmappings_list[id_num]
        return ret

    def _cmd_lsvdiskfcmappings(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5707E']
        vdisk = kwargs['obj']
        rows = []
        rows.append(['id', 'name'])
        for v in self._fcmappings_list.values():
            if v['source'] == vdisk or v['target'] == vdisk:
                rows.append([v['id'], v['name']])
        return self._print_info_cmd(rows=rows, **kwargs)

    def _cmd_chfcmap(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5707E']
        id_num = kwargs['obj']

        try:
            fcmap = self._fcmappings_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        for key in ['name', 'copyrate', 'cleanrate', 'autodelete']:
            if key in kwargs:
                fcmap[key] = kwargs[key]
        return ('', '')

    def _cmd_lsfcmap(self, **kwargs):
        rows = []
        rows.append(['id', 'name', 'source_vdisk_id', 'source_vdisk_name',
                     'target_vdisk_id', 'target_vdisk_name', 'group_id',
                     'group_name', 'status', 'progress', 'copy_rate',
                     'clean_progress', 'incremental', 'partner_FC_id',
                     'partner_FC_name', 'restoring', 'start_time',
                     'rc_controlled'])

        # Assume we always get a filtervalue argument
        filter_key = kwargs['filtervalue'].split('=')[0]
        filter_value = kwargs['filtervalue'].split('=')[1]
        to_delete = []
        for k, v in self._fcmappings_list.items():
            if str(v[filter_key]) == filter_value:
                source = self._volumes_list[v['source']]
                target = self._volumes_list[v['target']]
                self._state_transition('wait', v)

                if self._next_cmd_error['lsfcmap'] == 'speed_up':
                    self._next_cmd_error['lsfcmap'] = ''
                    curr_state = v['status']
                    while self._state_transition('wait', v) == ("", ""):
                        if curr_state == v['status']:
                            break
                        curr_state = v['status']

                if ((v['status'] == 'idle_or_copied' and v['autodelete'] and
                     v['progress'] == '100') or (v['status'] == 'end')):
                    to_delete.append(k)
                else:
                    rows.append([v['id'], v['name'], source['id'],
                                 source['name'], target['id'], target['name'],
                                 '', '', v['status'], v['progress'],
                                 v['copyrate'], '100', 'off', '', '', 'no', '',
                                 v['rc_controlled']])

        for d in to_delete:
            del self._fcmappings_list[d]

        return self._print_info_cmd(rows=rows, **kwargs)

    # Create a FlashCopy mapping
    def _cmd_mkfcconsistgrp(self, **kwargs):
        fcconsistgrp_info = {}
        fcconsistgrp_info['id'] = self._find_unused_id(self._fcconsistgrp_list)

        if 'name' in kwargs:
            fcconsistgrp_info['name'] = kwargs['name'].strip('\'\"')
        else:
            fcconsistgrp_info['name'] = 'fccstgrp' + fcconsistgrp_info['id']

        if 'autodelete' in kwargs:
            fcconsistgrp_info['autodelete'] = True
        else:
            fcconsistgrp_info['autodelete'] = False
        fcconsistgrp_info['status'] = 'empty'
        fcconsistgrp_info['start_time'] = None
        fcconsistgrp_info['fcmaps'] = {}

        self._fcconsistgrp_list[fcconsistgrp_info['id']] = fcconsistgrp_info

        return ('FlashCopy Consistency Group, id [' + fcconsistgrp_info['id'] +
                '], successfully created', '')

    def _cmd_prestartfcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        cg_name = kwargs['obj']

        cg_id = 0
        for cg_id in self._fcconsistgrp_list.keys():
            if cg_name == self._fcconsistgrp_list[cg_id]['name']:
                break

        return self._fc_cg_state_transition('prepare',
                                            self._fcconsistgrp_list[cg_id])

    def _cmd_startfcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        cg_name = kwargs['obj']

        cg_id = 0
        for cg_id in self._fcconsistgrp_list.keys():
            if cg_name == self._fcconsistgrp_list[cg_id]['name']:
                break

        return self._fc_cg_state_transition('start',
                                            self._fcconsistgrp_list[cg_id])

    def _cmd_stopfcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']

        try:
            fcconsistgrps = self._fcconsistgrp_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        return self._fc_cg_state_transition('stop', fcconsistgrps)

    def _cmd_rmfcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        cg_name = kwargs['obj']
        force = True if 'force' in kwargs else False

        cg_id = 0
        for cg_id in self._fcconsistgrp_list.keys():
            if cg_name == self._fcconsistgrp_list[cg_id]['name']:
                break
        if not cg_id:
            return self._errors['CMMVC5753E']
        fcconsistgrps = self._fcconsistgrp_list[cg_id]

        function = 'delete_force' if force else 'delete'
        ret = self._fc_cg_state_transition(function, fcconsistgrps)
        if fcconsistgrps['status'] == 'end':
            del self._fcconsistgrp_list[cg_id]
        return ret

    def _cmd_lsfcconsistgrp(self, **kwargs):
        rows = []

        if 'obj' not in kwargs:
            rows.append(['id', 'name', 'status' 'start_time'])

            for fcconsistgrp in self._fcconsistgrp_list.values():
                rows.append([fcconsistgrp['id'],
                             fcconsistgrp['name'],
                             fcconsistgrp['status'],
                             fcconsistgrp['start_time']])
            return self._print_info_cmd(rows=rows, **kwargs)
        else:
            fcconsistgrp = None
            cg_id = 0
            for cg_id in self._fcconsistgrp_list.keys():
                if self._fcconsistgrp_list[cg_id]['name'] == kwargs['obj']:
                    fcconsistgrp = self._fcconsistgrp_list[cg_id]
                    break
            rows = []
            rows.append(['id', str(cg_id)])
            rows.append(['name', fcconsistgrp['name']])
            rows.append(['status', fcconsistgrp['status']])
            rows.append(['autodelete',
                         str(fcconsistgrp['autodelete'])])
            rows.append(['start_time',
                         str(fcconsistgrp['start_time'])])

            for fcmap_id in fcconsistgrp['fcmaps'].keys():
                rows.append(['FC_mapping_id', str(fcmap_id)])
                rows.append(['FC_mapping_name',
                             fcconsistgrp['fcmaps'][fcmap_id]])

            if 'delim' in kwargs:
                for index in range(len(rows)):
                    rows[index] = kwargs['delim'].join(rows[index])
            self._fc_cg_state_transition('wait', fcconsistgrp)
            return ('%s' % '\n'.join(rows), '')

    def _cmd_migratevdisk(self, **kwargs):
        if 'mdiskgrp' not in kwargs or 'vdisk' not in kwargs:
            return self._errors['CMMVC5707E']
        mdiskgrp = kwargs['mdiskgrp'].strip('\'\"')
        vdisk = kwargs['vdisk'].strip('\'\"')
        copy_id = kwargs['copy']
        if vdisk not in self._volumes_list:
            return self._errors['CMMVC5753E']
        mdiskgrp_id = str(self._get_mdiskgrp_id(mdiskgrp))

        self._volumes_list[vdisk]['mdisk_grp_name'] = mdiskgrp
        self._volumes_list[vdisk]['mdisk_grp_id'] = mdiskgrp_id

        vol = self._volumes_list[vdisk]
        vol['copies'][copy_id]['mdisk_grp_name'] = mdiskgrp
        vol['copies'][copy_id]['mdisk_grp_id'] = mdiskgrp_id
        return ('', '')

    def _cmd_addvdiskcopy(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')
        if vol_name not in self._volumes_list:
            return self._errors['CMMVC5753E']
        vol = self._volumes_list[vol_name]
        if 'mdiskgrp' not in kwargs:
            return self._errors['CMMVC5707E']
        mdiskgrp = kwargs['mdiskgrp'].strip('\'\"')
        if mdiskgrp == kwargs['mdiskgrp']:
            raise exception.InvalidInput(
                reason=_('mdiskgrp missing quotes %s') % kwargs['mdiskgrp'])
        auto_del = True if 'autodelete' in kwargs else False

        copy_info = {}
        copy_info['id'] = self._find_unused_id(vol['copies'])
        copy_info['status'] = 'online'
        copy_info['sync'] = 'no'
        copy_info['primary'] = 'no'
        copy_info['mdisk_grp_name'] = mdiskgrp
        copy_info['mdisk_grp_id'] = str(self._get_mdiskgrp_id(mdiskgrp))

        if 'easytier' in kwargs:
            if kwargs['easytier'] == 'on':
                copy_info['easy_tier'] = 'on'
            else:
                copy_info['easy_tier'] = 'off'
        if 'rsize' in kwargs:
            if 'compressed' in kwargs:
                copy_info['compressed_copy'] = 'yes'
            else:
                copy_info['compressed_copy'] = 'no'
        vol['copies'][copy_info['id']] = copy_info
        if auto_del:
            del_copy_id = None
            for v in vol['copies'].values():
                if v['id'] != copy_info['id']:
                    del_copy_id = v['id']
                    break
            if del_copy_id:
                del vol['copies'][del_copy_id]
        return ('Vdisk [%(vid)s] copy [%(cid)s] successfully created' %
                {'vid': vol['id'], 'cid': copy_info['id']}, '')

    def _cmd_lsvdiskcopy(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5804E']
        name = kwargs['obj']
        vol = self._volumes_list[name]
        rows = []
        rows.append(['vdisk_id', 'vdisk_name', 'copy_id', 'status', 'sync',
                     'primary', 'mdisk_grp_id', 'mdisk_grp_name', 'capacity',
                     'type', 'se_copy', 'easy_tier', 'easy_tier_status',
                     'compressed_copy'])
        for copy in vol['copies'].values():
            if 'compressed_copy' not in copy:
                copy['compressed_copy'] = 'False'
            rows.append([vol['id'], vol['name'], copy['id'],
                        copy['status'], copy['sync'], copy['primary'],
                        copy['mdisk_grp_id'], copy['mdisk_grp_name'],
                        vol['capacity'], 'striped', 'yes', copy['easy_tier'],
                        'inactive', copy['compressed_copy']])
        if 'copy' not in kwargs:
            return self._print_info_cmd(rows=rows, **kwargs)
        else:
            copy_id = kwargs['copy'].strip('\'\"')
            if copy_id not in vol['copies']:
                return self._errors['CMMVC6353E']
            copy = vol['copies'][copy_id]
            rows = []
            rows.append(['vdisk_id', vol['id']])
            rows.append(['vdisk_name', vol['name']])
            rows.append(['capacity', vol['capacity']])
            rows.append(['copy_id', copy['id']])
            rows.append(['status', copy['status']])
            rows.append(['sync', copy['sync']])
            copy['sync'] = 'yes'
            rows.append(['primary', copy['primary']])
            rows.append(['mdisk_grp_id', copy['mdisk_grp_id']])
            rows.append(['mdisk_grp_name', copy['mdisk_grp_name']])
            rows.append(['easy_tier', copy['easy_tier']])
            rows.append(['easy_tier_status', 'inactive'])
            rows.append(['compressed_copy', copy['compressed_copy']])
            rows.append(['autoexpand', vol['autoexpand']])

            if 'delim' in kwargs:
                for index in range(len(rows)):
                    rows[index] = kwargs['delim'].join(rows[index])

            return ('%s' % '\n'.join(rows), '')

    def _cmd_rmvdiskcopy(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')
        if 'copy' not in kwargs:
            return self._errors['CMMVC5707E']
        copy_id = kwargs['copy'].strip('\'\"')
        if vol_name not in self._volumes_list:
            return self._errors['CMMVC5753E']
        vol = self._volumes_list[vol_name]
        if copy_id not in vol['copies']:
            return self._errors['CMMVC6353E']
        del vol['copies'][copy_id]

        return ('', '')

    def _cmd_lsvdisks_from_filter(self, filter_name, value):
        volumes = []
        if filter_name == 'mdisk_grp_name':
            for vol in self._volumes_list:
                vol_info = self._volumes_list[vol]
                if vol_info['mdisk_grp_name'] == value:
                    volumes.append(vol)
        return volumes

    def _cmd_chvdisk(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')
        vol = self._volumes_list[vol_name]
        kwargs.pop('obj')

        params = ['name', 'warning', 'udid',
                  'autoexpand', 'easytier', 'primary',
                  'volumegroup', 'novolumegroup']
        for key, value in kwargs.items():
            if key == 'easytier':
                vol['easy_tier'] = value
                for copy in vol['copies'].values():
                    vol['copies'][copy['id']]['easy_tier'] = value
                continue
            if key == 'warning':
                vol['warning'] = value.rstrip('%')
                continue
            if key == 'name':
                vol['name'] = value
                del self._volumes_list[vol_name]
                self._volumes_list[value] = vol
            if key == 'primary':
                if value == '0':
                    self._volumes_list[vol_name]['copies']['0']['primary']\
                        = 'yes'
                    self._volumes_list[vol_name]['copies']['1']['primary']\
                        = 'no'
                elif value == '1':
                    self._volumes_list[vol_name]['copies']['0']['primary']\
                        = 'no'
                    self._volumes_list[vol_name]['copies']['1']['primary']\
                        = 'yes'
                else:
                    err = self._errors['CMMVC6353E'][1] % {'VALUE': key}
                    return ('', err)
            if key == 'volumegroup':
                self._volumes_list[vol_name]['volume_group_id'] = value
            if key in params:
                vol[key] = value
                if key == 'autoexpand':
                    for copy in vol['copies'].values():
                        vol['copies'][copy['id']]['autoexpand'] = value
            else:
                err = self._errors['CMMVC5709E'][1] % {'VALUE': key}
                return ('', err)
        return ('', '')

    def _cmd_movevdisk(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')
        vol = self._volumes_list[vol_name]

        if 'iogrp' not in kwargs:
            return self._errors['CMMVC5707E']

        iogrp = kwargs['iogrp']
        if iogrp.isdigit():
            vol['IO_group_id'] = iogrp
            vol['IO_group_name'] = 'io_grp%s' % iogrp
        else:
            vol['IO_group_id'] = iogrp[6:]
            vol['IO_group_name'] = iogrp
        return ('', '')

    def _cmd_addvdiskaccess(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        return ('', '')

    def _cmd_rmvdiskaccess(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        return ('', '')

    # list vdisk sync process
    def _cmd_lsvdisksyncprogress(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5804E']
        name = kwargs['obj']
        copy_id = kwargs.get('copy', None)
        vol = self._volumes_list[name]
        rows = []
        rows.append(['vdisk_id', 'vdisk_name', 'copy_id', 'progress',
                     'estimated_completion_time'])
        copy_found = False
        for copy in vol['copies'].values():
            if not copy_id or copy_id == copy['id']:
                copy_found = True
                row = [vol['id'], name, copy['id']]
                if copy['sync'] == 'yes':
                    row.extend(['100', ''])
                else:
                    row.extend(['50', '140210115226'])
                    copy['sync'] = 'yes'
                rows.append(row)
        if not copy_found:
            return self._errors['CMMVC5804E']
        return self._print_info_cmd(rows=rows, **kwargs)

    def _add_host_to_list(self, connector):
        host_info = {}
        host_info['id'] = self._find_unused_id(self._hosts_list)
        host_info['host_name'] = connector['host']
        host_info['iscsi_names'] = []
        host_info['site_name'] = ''
        host_info['wwpns'] = []
        if 'initiator' in connector:
            host_info['iscsi_names'].append(connector['initiator'])
        if 'wwpns' in connector:
            host_info['wwpns'] = host_info['wwpns'] + connector['wwpns']
        self._hosts_list[connector['host']] = host_info

    def _host_in_list(self, host_name):
        for k in self._hosts_list:
            if k.startswith(host_name):
                return k
        return None

    # Replication related command
    # Create a remote copy
    def _cmd_mkrcrelationship(self, **kwargs):
        master_vol = ''
        aux_vol = ''
        aux_cluster = ''
        master_sys = self._system_list['storwize-svc-sim']
        aux_sys = self._system_list['aux-svc-sim']

        if 'master' not in kwargs:
            return self._errors['CMMVC5707E']
        master_vol = kwargs['master'].strip('\'\"')
        if master_vol not in self._volumes_list:
            return self._errors['CMMVC5754E']

        if 'aux' not in kwargs:
            return self._errors['CMMVC5707E']
        aux_vol = kwargs['aux'].strip('\'\"')
        if aux_vol not in self._volumes_list:
            return self._errors['CMMVC5754E']

        if 'cluster' not in kwargs:
            return self._errors['CMMVC5707E']
        aux_cluster = kwargs['cluster'].strip('\'\"')
        if aux_cluster != aux_sys['id']:
            return self._errors['CMMVC5754E']

        if (self._volumes_list[master_vol]['capacity'] !=
                self._volumes_list[aux_vol]['capacity']):
            return self._errors['CMMVC5754E']

        cyclingmode = None
        if 'cyclingmode' in kwargs:
            cyclingmode = kwargs['cyclingmode'].strip('\'\"')

        rcrel_info = {}
        rcrel_info['id'] = self._find_unused_id(self._rcrelationship_list)
        rcrel_info['name'] = 'rcrel' + rcrel_info['id']
        rcrel_info['master_cluster_id'] = master_sys['id']
        rcrel_info['master_cluster_name'] = master_sys['name']
        rcrel_info['master_vdisk_id'] = self._volumes_list[master_vol]['id']
        rcrel_info['master_vdisk_name'] = master_vol
        rcrel_info['aux_cluster_id'] = aux_sys['id']
        rcrel_info['aux_cluster_name'] = aux_sys['name']
        rcrel_info['aux_vdisk_id'] = self._volumes_list[aux_vol]['id']
        rcrel_info['aux_vdisk_name'] = aux_vol
        rcrel_info['primary'] = 'master'
        rcrel_info['consistency_group_id'] = ''
        rcrel_info['consistency_group_name'] = ''
        rcrel_info['state'] = 'inconsistent_stopped'
        rcrel_info['bg_copy_priority'] = '50'
        rcrel_info['progress'] = '0'
        rcrel_info['freeze_time'] = ''
        rcrel_info['status'] = 'online'
        rcrel_info['sync'] = ''
        rcrel_info['copy_type'] = 'global' if 'global' in kwargs else 'metro'
        rcrel_info['cycling_mode'] = cyclingmode if cyclingmode else 'none'
        rcrel_info['cycle_period_seconds'] = '300'
        rcrel_info['master_change_vdisk_id'] = ''
        rcrel_info['master_change_vdisk_name'] = ''
        rcrel_info['aux_change_vdisk_id'] = ''
        rcrel_info['aux_change_vdisk_name'] = ''

        self._rcrelationship_list[rcrel_info['name']] = rcrel_info
        self._volumes_list[master_vol]['RC_name'] = rcrel_info['name']
        self._volumes_list[master_vol]['RC_id'] = rcrel_info['id']
        self._volumes_list[aux_vol]['RC_name'] = rcrel_info['name']
        self._volumes_list[aux_vol]['RC_id'] = rcrel_info['id']
        return ('RC Relationship, id [' + rcrel_info['id'] +
                '], successfully created', '')

    def _cmd_lsrcrelationship(self, **kwargs):
        rows = []

        if 'obj' in kwargs:
            name = kwargs['obj']
            for k, v in self._rcrelationship_list.items():
                if ((str(v['name']) == name) or
                        (str(v['id']) == name)):
                    self._rc_state_transition('wait', v)

                    if self._next_cmd_error['lsrcrelationship'] == 'speed_up':
                        self._next_cmd_error['lsrcrelationship'] = ''
                        curr_state = v['status']
                        while self._rc_state_transition('wait', v) == ("", ""):
                            if curr_state == v['status']:
                                break
                            curr_state = v['status']

                    rows.append(['id', v['id']])
                    rows.append(['name', v['name']])
                    rows.append(['master_cluster_id', v['master_cluster_id']])
                    rows.append(['master_cluster_name',
                                v['master_cluster_name']])
                    rows.append(['master_vdisk_id', v['master_vdisk_id']])
                    rows.append(['master_vdisk_name', v['master_vdisk_name']])
                    rows.append(['aux_cluster_id', v['aux_cluster_id']])
                    rows.append(['aux_cluster_name', v['aux_cluster_name']])
                    rows.append(['aux_vdisk_id', v['aux_vdisk_id']])
                    rows.append(['aux_vdisk_name', v['aux_vdisk_name']])
                    rows.append(['consistency_group_id',
                                 v['consistency_group_id']])
                    rows.append(['primary', v['primary']])
                    rows.append(['consistency_group_name',
                                 v['consistency_group_name']])
                    rows.append(['state', v['state']])
                    rows.append(['bg_copy_priority', v['bg_copy_priority']])
                    rows.append(['progress', v['progress']])
                    rows.append(['freeze_time', v['freeze_time']])
                    rows.append(['status', v['status']])
                    rows.append(['sync', v['sync']])
                    rows.append(['copy_type', v['copy_type']])
                    rows.append(['cycling_mode', v['cycling_mode']])
                    rows.append(['cycle_period_seconds',
                                 v['cycle_period_seconds']])
                    rows.append(['master_change_vdisk_id',
                                 v['master_change_vdisk_id']])
                    rows.append(['master_change_vdisk_name',
                                 v['master_change_vdisk_name']])
                    rows.append(['aux_change_vdisk_id',
                                 v['aux_change_vdisk_id']])
                    rows.append(['aux_change_vdisk_name',
                                 v['aux_change_vdisk_name']])

        if 'nohdr' in kwargs:
            for index in range(len(rows)):
                rows[index] = ' '.join(rows[index][1:])
        if 'delim' in kwargs:
            for index in range(len(rows)):
                rows[index] = kwargs['delim'].join(rows[index])

        return ('%s' % '\n'.join(rows), '')

    def _cmd_startrcrelationship(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']

        primary_vol = None
        if 'primary' in kwargs:
            primary_vol = kwargs['primary'].strip('\'\"')

        try:
            rcrel = self._rcrelationship_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        if rcrel['state'] == 'idling' and not primary_vol:
            return self._errors['CMMVC5963E']

        self._rc_state_transition('start', rcrel)
        if primary_vol:
            self._rcrelationship_list[id_num]['primary'] = primary_vol
        return ('', '')

    def _cmd_stoprcrelationship(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']
        force_access = True if 'access' in kwargs else False

        try:
            rcrel = self._rcrelationship_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        if rcrel.get('consistency_group_name', None):
            return self._errors['CMMVC5951E']

        function = 'stop_access' if force_access else 'stop'
        self._rc_state_transition(function, rcrel)
        if force_access:
            self._rcrelationship_list[id_num]['primary'] = ''
        return ('', '')

    def _cmd_switchrcrelationship(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5707E']
        id_num = kwargs['obj']

        try:
            rcrel = self._rcrelationship_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        if rcrel['state'] == storwize_const.REP_CONSIS_SYNC:
            rcrel['primary'] = kwargs['primary']
            return ('', '')
        else:
            return self._errors['CMMVC5753E']

    def _cmd_chrcrelationship(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']

        try:
            rcrel = self._rcrelationship_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        remove_from_rccg = True if 'noconsistgrp' in kwargs else False
        add_to_rccg = True if 'consistgrp' in kwargs else False
        if remove_from_rccg:
            if rcrel['consistency_group_name']:
                rccg_name = rcrel['consistency_group_name']
            else:
                return self._errors['CMMVC6065E']
        elif add_to_rccg:
            rccg_name = (kwargs['consistgrp'].strip('\'\"')
                         if 'consistgrp' in kwargs else None)
        else:
            return self._chrcrelationship_attr(**kwargs)

        try:
            rccg = self._rcconsistgrp_list[rccg_name]
        except KeyError:
            return self._errors['CMMVC5753E']

        if remove_from_rccg:
            rcrel['consistency_group_name'] = ''
            rcrel['consistency_group_id'] = ''

            if int(rccg['relationship_count']) > 0:
                rccg['relationship_count'] = str(
                    int(rccg['relationship_count']) - 1)
            if rccg['relationship_count'] == '0':
                rccg['state'] = 'empty'
                rccg['copy_type'] = 'empty_group'
        else:
            if rccg['copy_type'] == 'empty_group':
                rccg['copy_type'] = rcrel['copy_type']
            elif rccg['copy_type'] != rcrel['copy_type']:
                return self._errors['CMMVC9012E']

            rcrel['consistency_group_name'] = rccg['name']
            rcrel['consistency_group_id'] = rccg['id']
            rccg['relationship_count'] = str(
                int(rccg['relationship_count']) + 1)
            if rccg['state'] == 'empty':
                rccg['state'] = rcrel['state']
                rccg['primary'] = rcrel['primary']
                rccg['cycling_mode'] = rcrel['cycling_mode']
                rccg['cycle_period_seconds'] = rcrel['cycle_period_seconds']

        return '', ''

    def _cmd_chrcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        return self._chrcconsistgrp_attr(**kwargs)

    def _cmd_rmrcrelationship(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']
        force = True if 'force' in kwargs else False

        try:
            rcrel = self._rcrelationship_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        function = 'delete_force' if force else 'delete'
        self._rc_state_transition(function, rcrel)
        if rcrel['state'] == 'end':
            self._volumes_list[rcrel['master_vdisk_name']]['RC_name'] = ''
            self._volumes_list[rcrel['master_vdisk_name']]['RC_id'] = ''
            self._volumes_list[rcrel['aux_vdisk_name']]['RC_name'] = ''
            self._volumes_list[rcrel['aux_vdisk_name']]['RC_id'] = ''
            del self._rcrelationship_list[id_num]

        return ('', '')

    def _chrcrelationship_attr(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5707E']
        id_num = kwargs['obj']

        try:
            rcrel = self._rcrelationship_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        nonull_num = 0
        masterchange = None
        if 'masterchange' in kwargs:
            masterchange = kwargs['masterchange'].strip('\'\"')
            nonull_num += 1

        auxchange = None
        if 'auxchange' in kwargs:
            auxchange = kwargs['auxchange'].strip('\'\"')
            nonull_num += 1

        cycleperiodseconds = None
        if 'cycleperiodseconds' in kwargs:
            cycleperiodseconds = kwargs['cycleperiodseconds'].strip('\'\"')
            nonull_num += 1

        cyclingmode = None
        if 'cyclingmode' in kwargs:
            cyclingmode = kwargs['cyclingmode'].strip('\'\"')
            nonull_num += 1

        if nonull_num > 1:
            return self._errors['CMMVC5713E']
        elif masterchange:
            rcrel['master_change_vdisk_name'] = masterchange
            return ('', '')
        elif auxchange:
            rcrel['aux_change_vdisk_name'] = auxchange
            return ('', '')
        elif cycleperiodseconds:
            rcrel['cycle_period_seconds'] = cycleperiodseconds
        elif cyclingmode:
            rcrel['cycling_mode'] = cyclingmode
        return ('', '')

    def _chrcconsistgrp_attr(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5707E']
        id_num = kwargs['obj']

        try:
            rccg = self._rcconsistgrp_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        if 'cyclingmode' in kwargs:
            cyclingmode = kwargs['cyclingmode'].strip('\'\"')
            rccg['cycling_mode'] = cyclingmode

        return ('', '')

    def _rc_state_transition(self, function, rcrel):
        if (function == 'wait' and
                'wait' not in self._rc_transitions[rcrel['state']]):
            return ('', '')

        if rcrel['state'] == 'inconsistent_copying' and function == 'wait':
            if rcrel['progress'] == '0':
                rcrel['progress'] = '50'
            elif (storwize_const.GMCV_MULTI == rcrel['cycling_mode']
                  and storwize_const.GLOBAL == rcrel['copy_type']):
                rcrel['progress'] = '100'
                rcrel['state'] = 'consistent_copying'
            else:
                rcrel['progress'] = '100'
                rcrel['state'] = 'consistent_synchronized'
            return ('', '')
        else:
            try:
                curr_state = rcrel['state']
                rcrel['state'] = self._rc_transitions[curr_state][function]
                return ('', '')
            except Exception:
                return self._errors['CMMVC5982E']

    def _rccg_state_transition(self, function, rccg):
        if (function == 'wait' and
                'wait' not in self._rccg_transitions[rccg['state']]):
            return ('', '')

        if rccg['state'] == 'inconsistent_copying' and function == 'wait':
            if rccg['cycling_mode'] == storwize_const.GMCV_MULTI:
                rccg['state'] = storwize_const.REP_CONSIS_COPYING
            else:
                rccg['state'] = storwize_const.REP_CONSIS_SYNC
            for rcrel_info in self._rcrelationship_list.values():
                if rcrel_info['consistency_group_name'] == rccg['name']:
                    rcrel_info['progress'] = '100'
                    rcrel_info['state'] = rccg['state']
            return ('', '')
        else:
            try:
                curr_state = rccg['state']
                rccg['state'] = self._rccg_transitions[curr_state][function]
                return ('', '')
            except Exception:
                return self._errors['CMMVC5982E']

    def _cmd_mkvolumegroup(self, **kwargs):
        # Create a Volume group
        volumegroup_info = {}
        volumegroup_info['id'] = self._find_unused_id(self._volumegroup_list)
        if 'name' in kwargs:
            volumegroup_info['name'] = kwargs["name"].strip('\'\"')
        else:
            volumegroup_info['name'] = self.driver._get_volumegroup_name(
                None, volumegroup_info['id'])
        volumegroup_info['volume_count'] = '0'
        volumegroup_info['backup_status'] = 'empty'
        volumegroup_info['last_backup_time'] = ''
        volumegroup_info['owner_id'] = ''
        volumegroup_info['owner_name'] = ''
        volumegroup_info['safeguarded_policy_id'] = ''
        volumegroup_info['safeguarded_policy_name'] = ''
        volumegroup_info['safeguarded_policy_start_time'] = ''
        volumegroup_info['volume_group_type'] = ''
        volumegroup_info['uid'] = (('ABCDEF' * 3) + ('0' * 14) +
                                   volumegroup_info['id'])
        volumegroup_info['source_volume_group_id'] = ''
        volumegroup_info['source_volume_group_name'] = ''
        volumegroup_info['parent_uid'] = ''
        volumegroup_info['source_snapshot_id'] = ''
        volumegroup_info['source_snapshot'] = ''
        volumegroup_info['snapshot_count'] = '0'
        volumegroup_info['protection_provisioned_capacity'] = '0.00MB'
        volumegroup_info['protection_written_capacity'] = '0.00MB'
        volumegroup_info['snapshot_policy_id'] = ''
        volumegroup_info['snapshot_policy_name'] = ''
        self._volumegroup_list[volumegroup_info['name']] = volumegroup_info
        return ('Volume Group, id [' + volumegroup_info['id'] +
                '], successfully created', '')

    def _cmd_lsvolumegroup(self, **kwargs):
        # List the volume group
        if 'obj' not in kwargs:
            rows = []
            rows.append(['id', 'name', 'volume_count', 'backup_status',
                         'last_backup_time', 'owner_id', 'owner_name',
                         'safeguarded_policy_id', 'safeguarded_policy_name',
                         'safeguarded_policy_start_time', 'volume_group_type',
                         'uid', 'source_volume_group_id',
                         'source_volume_group_name', 'parent_uid',
                         'source_snapshot_id', 'source_snapshot',
                         'snapshot_count', 'protection_provisioned_capacity',
                         'protection_written_capacity', 'snapshot_policy_id',
                         'snapshot_policy_name'])
            found = False
            for volumegroup_name in sorted(self._volumegroup_list.keys()):
                volumegroup = self._volumegroup_list[volumegroup_name]
                filterstr = 'name=' + volumegroup['name']
                if (('filtervalue' not in kwargs) or
                        (kwargs['filtervalue'] == filterstr)):
                    rows.append(['0', 'empty', '', '', '', '', '', '', '',
                                 '((\'ABCDEF\' * 3) + (\'0\' * 14) +\
                                 vg_info[\'id\'])', '', '', '', '', '', '0',
                                 '0.00MB', '0.00MB', '', ''])
                    found = True
            if found:
                return self._print_info_cmd(rows=rows, **kwargs)
            else:
                return ('', '')
        else:
            volumegroup_info = kwargs['obj'].strip('\'\"')
            if volumegroup_info not in self._volumegroup_list:
                return self._errors['CMMVC5804E']
            volumegroup_info = self._volumegroup_list[volumegroup_info]
            rows = []
            rows.append(['id', volumegroup_info['id']])
            rows.append(['name', volumegroup_info['name']])
            rows.append(['volume_count', '1'])
            rows.append(['backup_status', 'off'])
            rows.append(['last_backup_time',
                        volumegroup_info['last_backup_time']])
            rows.append(['owner_id', volumegroup_info['owner_id']])
            rows.append(['owner_name', volumegroup_info['owner_name']])
            rows.append(['safeguarded_policy_id',
                        volumegroup_info['safeguarded_policy_id']])
            rows.append(['safeguarded_policy_name',
                        volumegroup_info['safeguarded_policy_name']])
            rows.append(['safeguarded_policy_start_time',
                        volumegroup_info['safeguarded_policy_start_time']])
            rows.append(['volume_group_type',
                        volumegroup_info['volume_group_type']])
            rows.append(['source_volume_group_id',
                        volumegroup_info['source_volume_group_id']])
            rows.append(['source_volume_group_name',
                        volumegroup_info['source_volume_group_name']])
            rows.append(['parent_uid', volumegroup_info['parent_uid']])
            rows.append(['source_snapshot_id',
                        volumegroup_info['source_snapshot_id']])
            rows.append(['source_snapshot',
                        volumegroup_info['source_snapshot']])
            rows.append(['snapshot_count', volumegroup_info['snapshot_count']])
            rows.append(['protection_provisioned_capacity', '1.00GB'])
            rows.append(['protection_written_capacity', '0.75MB'])
            rows.append(['snapshot_policy_id',
                        volumegroup_info['snapshot_policy_id']])
            rows.append(['snapshot_policy_name',
                        volumegroup_info['snapshot_policy_name']])

            if 'delim' in kwargs:
                for index in range(len(rows)):
                    rows[index] = kwargs['delim'].join(rows[index])
            return ('%s' % '\n'.join(rows), '')

    def _cmd_rmvolumegroup(self, **kwargs):
        # Delete a Volume Group
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        volumegroup_name = kwargs['obj'].strip('\'\"')
        del self._volumegroup_list[volumegroup_name]
        return ('', '')

    def _cmd_addsnapshot(self, **kwargs):
        # Create a Volumegroup snapshot
        volumegroup_snapshot_info = {}
        volumegroup_snapshot_info['id'] = self._find_unused_id(
            self._volumegroup_snapshot_list)
        volumegroup_name = kwargs['volumegroup']
        if 'name' in kwargs:
            volumegroup_snapshot_info['name'] = kwargs["name"].strip('\'\"')
        else:
            volumegroup_snapshot_info['name'] = (
                'vg_snap-' + volumegroup_snapshot_info['id'])
        volumegroup_snapshot_info['volume_group_id'] = (
            self._volumegroup_list[volumegroup_name]['id'])
        volumegroup_snapshot_info['volume_group_name'] = volumegroup_name
        volumegroup_snapshot_info['time'] = ''
        volumegroup_snapshot_info['state'] = 'active'
        volumegroup_snapshot_info['matches_group'] = 'yes'
        volumegroup_snapshot_info['parent_uid'] = (
            self._volumegroup_list[volumegroup_name]['uid'])
        volumegroup_snapshot_info['expiration_time'] = ''
        volumegroup_snapshot_info['protection_provisioned_capacity'] = '1.00GB'
        volumegroup_snapshot_info['protection_written_capacity'] = '0.75MB'
        volumegroup_snapshot_info['operation_start_time'] = ''
        volumegroup_snapshot_info['operation_completion_estimate'] = ''
        volumegroup_snapshot_info['owner_id'] = ''
        volumegroup_snapshot_info['owner_name'] = ''
        volumegroup_snapshot_info['auto_snapshot'] = 'no'
        self._volumegroup_snapshot_list[volumegroup_snapshot_info['name']] = (
            volumegroup_snapshot_info)
        return ('Snapshot, id [' + volumegroup_snapshot_info['id'] +
                '], successfully created or triggered', '')

    def _cmd_lsvolumegroupsnapshot(self, **kwargs):
        # List the volume group snapshot
        rows = []
        rows.append(['id', 'name', 'volume_group_id', 'volume_group_name',
                     'time', 'state', 'matches_group', 'parent_uid',
                     'expiration_time', 'protection_provisioned_capacity',
                     'protection_written_capacity', 'operation_start_time',
                     'operation_completion_estimate', 'owner_id',
                     'owner_name', 'auto_snapshot'])
        if 'snapshot' and 'volumegroup' not in kwargs:
            found = False
            for volumegroup_snapshot in sorted(
                    self._volumegroup_snapshot_list.keys()):
                volumegroup_snapshot_info = self._volumegroup_snapshot_list[
                    volumegroup_snapshot]
                if 'filtervalue' not in kwargs:
                    rows.append(
                        [volumegroup_snapshot_info['id'],
                         volumegroup_snapshot_info['name'],
                         volumegroup_snapshot_info['volume_group_id'],
                         volumegroup_snapshot_info['volume_group_name'],
                         '', 'active', 'yes',
                         volumegroup_snapshot_info['parent_uid'], '', '1.00GB',
                         '0.75MB', '', '', '', '', 'no'])
                    found = True
            if found:
                return self._print_info_cmd(rows=rows, **kwargs)
            else:
                return ('', '')
        else:
            volumegroup_snapshot_info = kwargs['snapshot'].strip('\'\"')
            if volumegroup_snapshot_info not in (
                    self._volumegroup_snapshot_list):
                return self._errors['CMMVC5804E']
            volumegroup_snapshot_info = self._volumegroup_snapshot_list[
                volumegroup_snapshot_info]
            rows.append(
                [volumegroup_snapshot_info['id'],
                 volumegroup_snapshot_info['name'],
                 volumegroup_snapshot_info['volume_group_id'],
                 volumegroup_snapshot_info['volume_group_name'],
                 volumegroup_snapshot_info['time'],
                 volumegroup_snapshot_info['state'],
                 volumegroup_snapshot_info['matches_group'],
                 volumegroup_snapshot_info['parent_uid'],
                 volumegroup_snapshot_info['expiration_time'],
                 volumegroup_snapshot_info['protection_provisioned_capacity'],
                 volumegroup_snapshot_info['protection_written_capacity'],
                 volumegroup_snapshot_info['operation_start_time'],
                 volumegroup_snapshot_info['operation_completion_estimate'],
                 volumegroup_snapshot_info['owner_id'],
                 volumegroup_snapshot_info['owner_name'],
                 volumegroup_snapshot_info['auto_snapshot']])

            if 'delim' in kwargs:
                for index in range(len(rows)):
                    rows[index] = kwargs['delim'].join(rows[index])
            return ('%s' % '\n'.join(rows), '')

    def _cmd_rmsnapshot(self, **kwargs):
        # Delete a Volume Group snapshot
        if 'snapshot' and 'volumegroup' not in kwargs:
            return self._errors['CMMVC5701E']

        volumegroup_snapshot_name = kwargs['snapshot'].strip('\'\"')
        if volumegroup_snapshot_name not in self._volumegroup_snapshot_list:
            return self._errors['CMMVC9755E']
        del self._volumegroup_snapshot_list[volumegroup_snapshot_name]
        return ('', '')

    def _cmd_mkrcconsistgrp(self, **kwargs):
        master_sys = self._system_list['storwize-svc-sim']
        aux_sys = self._system_list['aux-svc-sim']
        if 'cluster' not in kwargs:
            return self._errors['CMMVC5707E']
        aux_cluster = kwargs['cluster'].strip('\'\"')
        if (aux_cluster != aux_sys['id'] and
                aux_cluster != master_sys['id']):
            return self._errors['CMMVC5754E']

        rccg_info = {}
        rccg_info['id'] = self._find_unused_id(self._rcconsistgrp_list)

        if 'name' in kwargs:
            rccg_info['name'] = kwargs['name'].strip('\'\"')
        else:
            rccg_info['name'] = self.driver._get_rccg_name(None,
                                                           rccg_info['id'])
        rccg_info['master_cluster_id'] = master_sys['id']
        rccg_info['master_cluster_name'] = master_sys['name']
        rccg_info['aux_cluster_id'] = aux_sys['id']
        rccg_info['aux_cluster_name'] = aux_sys['name']

        rccg_info['primary'] = ''
        rccg_info['state'] = 'empty'
        rccg_info['relationship_count'] = '0'

        rccg_info['freeze_time'] = ''
        rccg_info['status'] = ''
        rccg_info['sync'] = ''
        rccg_info['copy_type'] = 'empty_group'
        rccg_info['cycling_mode'] = ''
        rccg_info['cycle_period_seconds'] = '300'
        self._rcconsistgrp_list[rccg_info['name']] = rccg_info

        return ('RC Consistency Group, id [' + rccg_info['id'] +
                '], successfully created', '')

    def _cmd_lsrcconsistgrp(self, **kwargs):
        rows = []

        if 'obj' not in kwargs:
            rows.append(['id', 'name', 'master_cluster_id',
                         'master_cluster_name', 'aux_cluster_id',
                         'aux_cluster_name', 'primary', 'state',
                         'relationship_count', 'copy_type',
                         'cycling_mode', 'freeze_time'])
            for rccg_info in self._rcconsistgrp_list.values():
                rows.append([rccg_info['id'], rccg_info['name'],
                             rccg_info['master_cluster_id'],
                             rccg_info['master_cluster_name'],
                             rccg_info['aux_cluster_id'],
                             rccg_info['aux_cluster_name'],
                             rccg_info['primary'], rccg_info['state'],
                             rccg_info['relationship_count'],
                             rccg_info['copy_type'], rccg_info['cycling_mode'],
                             rccg_info['freeze_time']])
            return self._print_info_cmd(rows=rows, **kwargs)
        else:
            try:
                rccg_info = self._rcconsistgrp_list[kwargs['obj']]
            except KeyError:
                return self._errors['CMMVC5804E']

            rows = []
            rows.append(['id', rccg_info['id']])
            rows.append(['name', rccg_info['name']])
            rows.append(['master_cluster_id', rccg_info['master_cluster_id']])
            rows.append(['master_cluster_name',
                         rccg_info['master_cluster_name']])
            rows.append(['aux_cluster_id', rccg_info['aux_cluster_id']])
            rows.append(['aux_cluster_name', rccg_info['aux_cluster_name']])
            rows.append(['primary', rccg_info['primary']])
            rows.append(['state', rccg_info['state']])
            rows.append(['relationship_count',
                         rccg_info['relationship_count']])
            rows.append(['freeze_time', rccg_info['freeze_time']])
            rows.append(['status', rccg_info['status']])
            rows.append(['sync', rccg_info['sync']])
            rows.append(['copy_type', rccg_info['copy_type']])
            rows.append(['cycling_mode', rccg_info['cycling_mode']])
            rows.append(['cycle_period_seconds',
                         rccg_info['cycle_period_seconds']])

            if 'delim' in kwargs:
                for index in range(len(rows)):
                    rows[index] = kwargs['delim'].join(rows[index])
            return ('%s' % '\n'.join(rows), '')

    def _cmd_startrcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']

        primary = (kwargs['primary'].strip('\'\"') if 'primary'
                                                      in kwargs else None)
        try:
            rccg = self._rcconsistgrp_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        if rccg['state'] == 'idling' and not primary:
            return self._errors['CMMVC5963E']

        self._rccg_state_transition('start', rccg)
        for rcrel_info in self._rcrelationship_list.values():
            if rcrel_info['consistency_group_name'] == rccg:
                self._rc_state_transition('start', rcrel_info)
        if primary:
            self._rcconsistgrp_list[id_num]['primary'] = primary
            for rcrel_info in self._rcrelationship_list.values():
                if rcrel_info['consistency_group_name'] == rccg['name']:
                    rcrel_info['primary'] = primary
        return ('', '')

    def _cmd_stoprcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        id_num = kwargs['obj']
        force_access = True if 'access' in kwargs else False

        try:
            rccg = self._rcconsistgrp_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        function = 'stop'
        self._rccg_state_transition(function, rccg)
        for rcrel_info in self._rcrelationship_list.values():
            if rcrel_info['consistency_group_name'] == rccg['name']:
                self._rc_state_transition(function, rcrel_info)
        if force_access:
            self._rcconsistgrp_list[id_num]['primary'] = ''
            for rcrel_info in self._rcrelationship_list.values():
                if rcrel_info['consistency_group_name'] == rccg['name']:
                    rcrel_info['primary'] = ''
        return ('', '')

    def _cmd_switchrcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5707E']
        id_num = kwargs['obj']

        try:
            rccg = self._rcconsistgrp_list[id_num]
        except KeyError:
            return self._errors['CMMVC5753E']

        if (rccg['state'] == storwize_const.REP_CONSIS_SYNC or
                (rccg['cycling_mode'] == storwize_const.GMCV_MULTI and
                 rccg['state'] == storwize_const.REP_CONSIS_COPYING)):
            rccg['primary'] = kwargs['primary']
            for rcrel_info in self._rcrelationship_list.values():
                if rcrel_info['consistency_group_name'] == rccg['name']:
                    rcrel_info['primary'] = kwargs['primary']
            return ('', '')
        else:
            return self._errors['CMMVC5753E']

    def _cmd_rmrcconsistgrp(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        rccg_name = kwargs['obj'].strip('\'\"')
        force = True if 'force' in kwargs else False

        try:
            rccg = self._rcconsistgrp_list[rccg_name]
        except KeyError:
            return self._errors['CMMVC5804E']

        function = 'delete_force' if force else 'delete'
        self._rccg_state_transition(function, rccg)
        if rccg['state'] == 'end':
            for rcrel_info in self._rcrelationship_list.values():
                if rcrel_info['consistency_group_name'] == rccg['name']:
                    rcrel_info['consistency_group_name'] = ''
                    rcrel_info['consistency_group_id'] = ''
            del self._rcconsistgrp_list[rccg_name]
        return ('', '')

    def _cmd_lspartnershipcandidate(self, **kwargs):
        rows = [None] * 4
        master_sys = self._system_list['storwize-svc-sim']
        aux_sys = self._system_list['aux-svc-sim']
        rows[0] = ['id', 'configured', 'name']
        rows[1] = [master_sys['id'], 'no', master_sys['name']]
        rows[2] = [aux_sys['id'], 'no', aux_sys['name']]
        rows[3] = ['0123456789001234', 'no', 'fake_svc']
        return self._print_info_cmd(rows=rows, **kwargs)

    def _cmd_lspartnership(self, **kwargs):
        rows = []
        rows.append(['id', 'name', 'location', 'partnership',
                     'type', 'cluster_ip', 'event_log_sequence'])

        master_sys = self._system_list['storwize-svc-sim']
        if master_sys['name'] not in self._partnership_list:
            local_info = {}
            local_info['id'] = master_sys['id']
            local_info['name'] = master_sys['name']
            local_info['location'] = 'local'
            local_info['type'] = ''
            local_info['cluster_ip'] = ''
            local_info['event_log_sequence'] = ''
            local_info['chap_secret'] = ''
            local_info['linkbandwidthmbits'] = ''
            local_info['backgroundcopyrate'] = ''
            local_info['partnership'] = ''
            self._partnership_list[master_sys['id']] = local_info

        # Assume we always get a filtervalue argument
        filter_key = kwargs['filtervalue'].split('=')[0]
        filter_value = kwargs['filtervalue'].split('=')[1]
        for k, v in self._partnership_list.items():
            if str(v[filter_key]) == filter_value:
                rows.append([v['id'], v['name'], v['location'],
                             v['partnership'], v['type'], v['cluster_ip'],
                             v['event_log_sequence']])
        return self._print_info_cmd(rows=rows, **kwargs)

    def _cmd_mkippartnership(self, **kwargs):
        if 'clusterip' not in kwargs:
            return self._errors['CMMVC5707E']
        clusterip = kwargs['master'].strip('\'\"')

        if 'linkbandwidthmbits' not in kwargs:
            return self._errors['CMMVC5707E']
        bandwidth = kwargs['linkbandwidthmbits'].strip('\'\"')

        if 'backgroundcopyrate' not in kwargs:
            return self._errors['CMMVC5707E']
        copyrate = kwargs['backgroundcopyrate'].strip('\'\"')

        if clusterip == '192.168.10.21':
            partner_info_id = self._system_list['storwize-svc-sim']['id']
            partner_info_name = self._system_list['storwize-svc-sim']['name']
        else:
            partner_info_id = self._system_list['aux-svc-sim']['id']
            partner_info_name = self._system_list['aux-svc-sim']['name']

        partner_info = {}
        partner_info['id'] = partner_info_id
        partner_info['name'] = partner_info_name
        partner_info['location'] = 'remote'
        partner_info['type'] = 'ipv4'
        partner_info['cluster_ip'] = clusterip
        partner_info['event_log_sequence'] = ''
        partner_info['chap_secret'] = ''
        partner_info['linkbandwidthmbits'] = bandwidth
        partner_info['backgroundcopyrate'] = copyrate
        partner_info['partnership'] = 'fully_configured'

        self._partnership_list[partner_info['id']] = partner_info
        return ('', '')

    def _cmd_mkfcpartnership(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        peer_sys = kwargs['obj']

        if 'linkbandwidthmbits' not in kwargs:
            return self._errors['CMMVC5707E']
        bandwidth = kwargs['linkbandwidthmbits'].strip('\'\"')

        if 'backgroundcopyrate' not in kwargs:
            return self._errors['CMMVC5707E']
        copyrate = kwargs['backgroundcopyrate'].strip('\'\"')

        partner_info = {}
        partner_info['id'] = self._system_list[peer_sys]['id']
        partner_info['name'] = peer_sys
        partner_info['location'] = 'remote'
        partner_info['type'] = 'fc'
        partner_info['cluster_ip'] = ''
        partner_info['event_log_sequence'] = ''
        partner_info['chap_secret'] = ''
        partner_info['linkbandwidthmbits'] = bandwidth
        partner_info['backgroundcopyrate'] = copyrate
        partner_info['partnership'] = 'fully_configured'
        self._partnership_list[partner_info['id']] = partner_info
        return ('', '')

    def _cmd_chpartnership(self, **kwargs):
        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        peer_sys = kwargs['obj']
        if peer_sys not in self._partnership_list:
            return self._errors['CMMVC5753E']

        partner_state = ('fully_configured' if 'start' in kwargs
                         else 'fully_configured_stopped')
        self._partnership_list[peer_sys]['partnership'] = partner_state
        return ('', '')

    # The main function to run commands on the management simulator
    def execute_command(self, cmd, check_exit_code=True):
        try:
            kwargs = self._cmd_to_dict(cmd)
        except IndexError:
            return self._errors['CMMVC5707E']

        command = kwargs.pop('cmd')
        func = getattr(self, '_cmd_' + command)
        out, err = func(**kwargs)

        if (check_exit_code) and (len(err) != 0):
            raise processutils.ProcessExecutionError(exit_code=1,
                                                     stdout=out,
                                                     stderr=err,
                                                     cmd=' '.join(cmd))

        return (out, err)

    # After calling this function, the next call to the specified command will
    # result in in the error specified
    def error_injection(self, cmd, error):
        self._next_cmd_error[cmd] = error

    def change_vdiskcopy_attr(self, vol_name, key, value, copy="primary"):
        if copy == 'primary':
            self._volumes_list[vol_name]['copies']['0'][key] = value
        elif copy == 'secondary':
            self._volumes_list[vol_name]['copies']['1'][key] = value
        else:
            msg = _("The copy should be primary or secondary")
            raise exception.InvalidInput(reason=msg)

    def create_site_volume_and_fcmapping(self, kwargs, name, sitepool,
                                         fcmapping=False, source=None):

        sitepool_id = self._get_mdiskgrp_id(sitepool)
        site_volume_info = {}
        site_volume_info['id'] = self._find_unused_id(self._volumes_list)
        site_volume_info['uid'] = ('ABCDEF' * 3) + (
            '0' * 14) + site_volume_info['id']

        site_volume_info['mdisk_grp_name'] = sitepool
        site_volume_info['mdisk_grp_id'] = str(sitepool_id)

        if 'name' in kwargs or 'obj' in kwargs:
            site_volume_info['name'] = name
        else:
            site_volume_info['name'] = name + site_volume_info['id']
        # Assume size and unit are given, store it in bytes
        if "size" in kwargs:
            capacity = int(kwargs['size'])
            unit = kwargs['unit']
            site_volume_info['capacity'] = self._convert_units_bytes(
                capacity, unit)
        else:
            site_volume_info['capacity'] = source['capacity']
        site_volume_info['IO_group_id'] = '0'
        site_volume_info['IO_group_name'] = 'io_grp0'
        site_volume_info['RC_name'] = ''
        site_volume_info['RC_id'] = ''

        if 'thin' in kwargs or 'compressed' in kwargs:
            site_volume_info['formatted'] = 'no'
            # Fake numbers
            site_volume_info['used_capacity'] = '786432'
            site_volume_info['real_capacity'] = '21474816'
            site_volume_info['free_capacity'] = '38219264'
            if 'warning' in kwargs:
                site_volume_info['warning'] = kwargs['warning'].rstrip('%')
            else:
                site_volume_info['warning'] = '80'

            if 'noautoexpand' in kwargs:
                site_volume_info['autoexpand'] = 'off'
            else:
                site_volume_info['autoexpand'] = 'on'

            if 'compressed' in kwargs:
                site_volume_info['compressed_copy'] = 'yes'
            else:
                site_volume_info['compressed_copy'] = 'no'

            if 'thin' in kwargs:
                site_volume_info['formatted'] = 'no'
                # Fake numbers
                site_volume_info['used_capacity'] = '786432'
                site_volume_info['real_capacity'] = '21474816'
                site_volume_info['free_capacity'] = '38219264'
                if 'grainsize' in kwargs:
                    site_volume_info['grainsize'] = kwargs['grainsize']
                else:
                    site_volume_info['grainsize'] = '32'
        else:
            site_volume_info['used_capacity'] = site_volume_info['capacity']
            site_volume_info['real_capacity'] = site_volume_info['capacity']
            site_volume_info['free_capacity'] = '0'
            site_volume_info['warning'] = ''
            site_volume_info['autoexpand'] = ''
            site_volume_info['grainsize'] = ''
            site_volume_info['compressed_copy'] = 'no'
            site_volume_info['formatted'] = 'yes'

        vol_cp = {'id': '0',
                  'status': 'online',
                  'sync': 'yes',
                  'primary': 'yes',
                  'mdisk_grp_id': str(sitepool_id),
                  'mdisk_grp_name': sitepool,
                  'easy_tier': 'on',
                  'compressed_copy': site_volume_info['compressed_copy']}
        site_volume_info['copies'] = {'0': vol_cp}

        if site_volume_info['name'] in self._volumes_list:
            return self._errors['CMMVC6035E']
        else:
            self._volumes_list[site_volume_info['name']] = site_volume_info

        # create a flashcopy mapping for site volume and site flashcopy volume
        if fcmapping:
            site_fcmap_info = {}
            site_fcmap_info['source'] = source['name']
            site_fcmap_info['target'] = site_volume_info['name']
            site_fcmap_info['id'] = self._find_unused_id(self._fcmappings_list)
            site_fcmap_info['name'] = 'fcmap' + site_fcmap_info['id']
            site_fcmap_info['copyrate'] = '50'
            site_fcmap_info['progress'] = '0'
            site_fcmap_info['autodelete'] = (True if 'autodelete' in kwargs
                                             else False)
            site_fcmap_info['status'] = 'idle_or_copied'
            site_fcmap_info['rc_controlled'] = 'yes'

            self._fcmappings_list[site_fcmap_info['id']] = site_fcmap_info

        return site_volume_info

    def _cmd_mkvolume(self, **kwargs):
        pool = kwargs['pool'].strip('\'\"')
        pool_split = pool.split(':')
        if len(pool_split) != 2:
            raise exception.InvalidInput(
                reason=_('pool %s is invalid for hyperswap '
                         'volume') % kwargs['pool'])
        else:
            site1pool = pool_split[0]
            site2pool = pool_split[1]

        if pool == kwargs['pool']:
            raise exception.InvalidInput(
                reason=_('pool missing quotes %s') % kwargs['pool'])

        if 'name' in kwargs:
            site1name = kwargs['name'].strip('\'\"')
            site1fcname = 'fcsite1' + kwargs['name'].strip('\'\"')
            site2name = 'site2' + kwargs['name'].strip('\'\"')
            site2fcname = 'fcsite2' + kwargs['name'].strip('\'\"')
        else:
            site1name = 'vdisk'
            site1fcname = 'fcsite1vdisk'
            site2name = 'site2vdisk'
            site2fcname = 'fcsite2vdisk'

        # create hyperswap volume on site1
        site1_volume_info = self.create_site_volume_and_fcmapping(
            kwargs, site1name, site1pool, False, None)
        # create flashcopy volume on site1
        self.create_site_volume_and_fcmapping(kwargs, site1fcname, site1pool,
                                              True, site1_volume_info)
        # create hyperswap volume on site2
        site2_volume_info = self.create_site_volume_and_fcmapping(
            kwargs, site2name, site2pool, False, site1_volume_info)
        # create flashcopy volume on site2
        self.create_site_volume_and_fcmapping(kwargs, site2fcname, site2pool,
                                              True, site2_volume_info)

        # Create remote copy for site1volume and site2volume
        master_sys = self._system_list['storwize-svc-sim']
        aux_sys = self._system_list['storwize-svc-sim']
        rcrel_info = {}
        rcrel_info['id'] = self._find_unused_id(self._rcrelationship_list)
        rcrel_info['name'] = 'rcrel' + rcrel_info['id']
        rcrel_info['master_cluster_id'] = master_sys['id']
        rcrel_info['master_cluster_name'] = master_sys['name']
        rcrel_info['master_vdisk_id'] = site1_volume_info['id']
        rcrel_info['master_vdisk_name'] = site1_volume_info['name']
        rcrel_info['aux_cluster_id'] = aux_sys['id']
        rcrel_info['aux_cluster_name'] = aux_sys['name']
        rcrel_info['aux_vdisk_id'] = site2_volume_info['id']
        rcrel_info['aux_vdisk_name'] = site2_volume_info['name']
        rcrel_info['primary'] = 'master'
        rcrel_info['consistency_group_id'] = ''
        rcrel_info['consistency_group_name'] = ''
        rcrel_info['state'] = 'inconsistent_stopped'
        rcrel_info['bg_copy_priority'] = '50'
        rcrel_info['progress'] = '0'
        rcrel_info['freeze_time'] = ''
        rcrel_info['status'] = 'online'
        rcrel_info['sync'] = ''
        rcrel_info['copy_type'] = 'activeactive'
        rcrel_info['cycling_mode'] = ''
        rcrel_info['cycle_period_seconds'] = '300'
        rcrel_info['master_change_vdisk_id'] = ''
        rcrel_info['master_change_vdisk_name'] = ''
        rcrel_info['aux_change_vdisk_id'] = ''
        rcrel_info['aux_change_vdisk_name'] = ''

        self._rcrelationship_list[rcrel_info['name']] = rcrel_info
        site1_volume_info['RC_name'] = rcrel_info['name']
        site1_volume_info['RC_id'] = rcrel_info['id']
        site2_volume_info['RC_name'] = rcrel_info['name']
        site2_volume_info['RC_id'] = rcrel_info['id']
        return ('Hyperswap volume, id [%s], successfully created' %
                (site1_volume_info['id']), '')

    def _cmd_addvolumecopy(self, **kwargs):

        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')
        site1_volume_info = self._volumes_list[vol_name]
        site1pool = site1_volume_info['mdisk_grp_name']
        site2pool = kwargs['pool'].strip('\'\"')
        site1fcname = 'fcsite1' + vol_name
        site2name = 'site2' + vol_name
        site2fcname = 'fcsite2' + vol_name

        # create flashcopy volume on site1
        self.create_site_volume_and_fcmapping(kwargs, site1fcname, site1pool,
                                              True, site1_volume_info)
        # create hyperswap volume on site2
        site2_volume_info = self.create_site_volume_and_fcmapping(
            kwargs, site2name, site1pool, False, site1_volume_info)
        # create flashcopy volume on site2
        self.create_site_volume_and_fcmapping(kwargs, site2fcname, site2pool,
                                              True, site2_volume_info)

        # create remote copy for site1volume and site2volume
        master_sys = self._system_list['storwize-svc-sim']
        aux_sys = self._system_list['storwize-svc-sim']
        rcrel_info = {}
        rcrel_info['id'] = self._find_unused_id(self._rcrelationship_list)
        rcrel_info['name'] = 'rcrel' + rcrel_info['id']
        rcrel_info['master_cluster_id'] = master_sys['id']
        rcrel_info['master_cluster_name'] = master_sys['name']
        rcrel_info['master_vdisk_id'] = site1_volume_info['id']
        rcrel_info['master_vdisk_name'] = site1_volume_info['name']
        rcrel_info['aux_cluster_id'] = aux_sys['id']
        rcrel_info['aux_cluster_name'] = aux_sys['name']
        rcrel_info['aux_vdisk_id'] = site2_volume_info['id']
        rcrel_info['aux_vdisk_name'] = site2_volume_info['name']
        rcrel_info['primary'] = 'master'
        rcrel_info['consistency_group_id'] = ''
        rcrel_info['consistency_group_name'] = ''
        rcrel_info['state'] = 'inconsistent_stopped'
        rcrel_info['bg_copy_priority'] = '50'
        rcrel_info['progress'] = '0'
        rcrel_info['freeze_time'] = ''
        rcrel_info['status'] = 'online'
        rcrel_info['sync'] = ''
        rcrel_info['copy_type'] = 'activeactive'
        rcrel_info['cycling_mode'] = ''
        rcrel_info['cycle_period_seconds'] = '300'
        rcrel_info['master_change_vdisk_id'] = ''
        rcrel_info['master_change_vdisk_name'] = ''
        rcrel_info['aux_change_vdisk_id'] = ''
        rcrel_info['aux_change_vdisk_name'] = ''

        self._rcrelationship_list[rcrel_info['name']] = rcrel_info
        site1_volume_info['RC_name'] = rcrel_info['name']
        site1_volume_info['RC_id'] = rcrel_info['id']
        site2_volume_info['RC_name'] = rcrel_info['name']
        site2_volume_info['RC_id'] = rcrel_info['id']
        return ('', '')

    def _cmd_rmvolumecopy(self, **kwargs):

        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')
        site1_volume_info = self._volumes_list[vol_name]
        if site1_volume_info['RC_name']:
            rcrel = self._rcrelationship_list[site1_volume_info['RC_name']]
            if rcrel.get('consistency_group_name', None):
                return self._errors['CMMVC8783E']

        site2_volume_info = self._volumes_list['site2' + vol_name]

        del self._rcrelationship_list[self._volumes_list[vol_name]['RC_name']]
        site1fcmap = None
        site2fcmap = None
        for fcmap in self._fcmappings_list.values():
            if ((fcmap['source'] == vol_name) and
                    (fcmap['target'] == 'fcsite1' + vol_name)):
                site1fcmap = fcmap
                continue
            elif ((fcmap['source'] == 'site2' + vol_name) and
                    (fcmap['target'] == 'fcsite2' + vol_name)):
                site2fcmap = fcmap
                continue

        if site1fcmap:
            del self._fcmappings_list[site1fcmap['id']]
        if site2fcmap:
            del self._fcmappings_list[site2fcmap['id']]

        del site2_volume_info
        del self._volumes_list['site2' + vol_name]
        del self._volumes_list['fcsite1' + vol_name]
        del self._volumes_list['fcsite2' + vol_name]
        site1_volume_info['RC_name'] = ''
        site1_volume_info['RC_id'] = ''
        return ('', '')

    def _cmd_rmvolume(self, **kwargs):
        removehostmappings = True if 'removehostmappings' in kwargs else False

        if 'obj' not in kwargs:
            return self._errors['CMMVC5701E']
        vol_name = kwargs['obj'].strip('\'\"')

        if vol_name not in self._volumes_list:
            return self._errors['CMMVC5753E']

        site1fcmap = None
        site2fcmap = None
        for fcmap in self._fcmappings_list.values():
            if ((fcmap['source'] == vol_name) and
                    (fcmap['target'] == 'fcsite1' + vol_name)):
                site1fcmap = fcmap
                continue
            elif ((fcmap['source'] == 'site2' + vol_name) and
                    (fcmap['target'] == 'fcsite2' + vol_name)):
                site2fcmap = fcmap
                continue
        if site1fcmap:
            del self._fcmappings_list[site1fcmap['id']]
        if site2fcmap:
            del self._fcmappings_list[site2fcmap['id']]

        if not removehostmappings:
            for mapping in self._mappings_list.values():
                if mapping['vol'] == vol_name:
                    return self._errors['CMMVC5840E']

        del self._rcrelationship_list[self._volumes_list[vol_name]['RC_name']]
        del self._volumes_list[vol_name]
        del self._volumes_list['fcsite1' + vol_name]
        del self._volumes_list['site2' + vol_name]
        del self._volumes_list['fcsite2' + vol_name]
        return ('', '')


class StorwizeSVCISCSIFakeDriver(storwize_svc_iscsi.StorwizeSVCISCSIDriver):
    def __init__(self, *args, **kwargs):
        super(StorwizeSVCISCSIFakeDriver, self).__init__(*args, **kwargs)

    def set_fake_storage(self, fake):
        self.fake_storage = fake

    def _run_ssh(self, cmd, check_exit_code=True, attempts=1):
        utils.check_ssh_injection(cmd)
        ret = self.fake_storage.execute_command(cmd, check_exit_code)

        return ret


class StorwizeSVCFcFakeDriver(storwize_svc_fc.StorwizeSVCFCDriver):
    def __init__(self, *args, **kwargs):
        super(StorwizeSVCFcFakeDriver, self).__init__(*args, **kwargs)

    def set_fake_storage(self, fake):
        self.fake_storage = fake

    def _run_ssh(self, cmd, check_exit_code=True, attempts=1):
        utils.check_ssh_injection(cmd)
        ret = self.fake_storage.execute_command(cmd, check_exit_code)

        return ret


@ddt.ddt
class StorwizeSVCISCSIDriverTestCase(test.TestCase):
    @mock.patch.object(time, 'sleep')
    def setUp(self, mock_sleep):
        super(StorwizeSVCISCSIDriverTestCase, self).setUp()
        self.USESIM = True
        if self.USESIM:
            self.iscsi_driver = StorwizeSVCISCSIFakeDriver(
                configuration=conf.Configuration([], conf.SHARED_CONF_GROUP))
            self.host_site = {'site1': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
            self._def_flags = {'san_ip': 'hostname',
                               'san_login': 'user',
                               'san_password': 'pass',
                               'storwize_svc_volpool_name': ['openstack'],
                               'storwize_svc_flashcopy_timeout': 20,
                               'storwize_svc_flashcopy_rate': 49,
                               'storwize_svc_multipath_enabled': False,
                               'storwize_svc_allow_tenant_qos': True,
                               'storwize_preferred_host_site': self.host_site}
            wwpns = [
                str(random.randint(0, 9999999999999999)).zfill(16),
                str(random.randint(0, 9999999999999999)).zfill(16)]
            initiator = 'test.initiator.%s' % str(
                random.randint(10000, 99999))
            self._connector = {'ip': '1.234.56.78',
                               'host': 'storwize-svc-test',
                               'wwpns': wwpns,
                               'initiator': initiator}
            self.sim = StorwizeSVCManagementSimulator(['openstack'])

            self.iscsi_driver.set_fake_storage(self.sim)
            self.ctxt = context.get_admin_context()

        self._reset_flags()
        self.ctxt = context.get_admin_context()
        self.db = cinder.db
        self.iscsi_driver.db = self.db
        self.iscsi_driver.do_setup(None)
        self.iscsi_driver.check_for_setup_error()
        self.iscsi_driver._helpers.check_fcmapping_interval = 0

    def _set_flag(self, flag, value):
        group = self.iscsi_driver.configuration.config_group
        self.override_config(flag, value, group)

    def _reset_flags(self):
        CONF.reset()
        for k, v in self._def_flags.items():
            self._set_flag(k, v)

    def _create_volume(self, **kwargs):
        pool = _get_test_pool()
        prop = {'host': 'openstack@svc#%s' % pool,
                'size': 1,
                'volume_type_id': self.vt['id']}
        for p in prop.keys():
            if p not in kwargs:
                kwargs[p] = prop[p]
        vol = testutils.create_volume(self.ctxt, **kwargs)
        self.iscsi_driver.create_volume(vol)
        return vol

    def _delete_volume(self, volume):
        self.iscsi_driver.delete_volume(volume)
        self.db.volume_destroy(self.ctxt, volume['id'])

    def _generate_vol_info(self, vol_name, vol_id):
        pool = _get_test_pool()
        prop = {'mdisk_grp_name': pool}
        if vol_name:
            prop.update(volume_name=vol_name,
                        volume_id=vol_id,
                        volume_size=10)
        else:
            prop.update(size=10,
                        volume_type_id=None,
                        mdisk_grp_name=pool,
                        host='openstack@svc#%s' % pool)
        vol = testutils.create_volume(self.ctxt, **prop)
        return vol

    def _generate_snap_info(self, vol_id, size=10):
        prop = {'volume_id': vol_id,
                'volume_size': size}
        snap = testutils.create_snapshot(self.ctxt, **prop)
        return snap

    def _assert_vol_exists(self, name, exists):
        is_vol_defined = self.iscsi_driver._helpers.is_vdisk_defined(name)
        self.assertEqual(exists, is_vol_defined)

    def test_storwize_svc_iscsi_validate_connector(self):
        conn_neither = {'host': 'host'}
        conn_iscsi = {'host': 'host', 'initiator': 'foo'}
        conn_fc = {'host': 'host', 'wwpns': 'bar'}
        conn_both = {'host': 'host', 'initiator': 'foo', 'wwpns': 'bar'}

        self.iscsi_driver._state['enabled_protocols'] = set(['iSCSI'])
        self.iscsi_driver.validate_connector(conn_iscsi)
        self.iscsi_driver.validate_connector(conn_both)
        self.assertRaises(exception.InvalidConnectorException,
                          self.iscsi_driver.validate_connector, conn_fc)
        self.assertRaises(exception.InvalidConnectorException,
                          self.iscsi_driver.validate_connector, conn_neither)

        self.iscsi_driver._state['enabled_protocols'] = set(['iSCSI', 'FC'])
        self.iscsi_driver.validate_connector(conn_iscsi)
        self.iscsi_driver.validate_connector(conn_both)
        self.assertRaises(exception.InvalidConnectorException,
                          self.iscsi_driver.validate_connector, conn_neither)

    def test_storwize_terminate_iscsi_connection(self):
        # create a iSCSI volume
        volume_iSCSI = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> iSCSI'}
        vol_type_iSCSI = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iSCSI['volume_type_id'] = vol_type_iSCSI['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.iscsi_driver.initialize_connection(volume_iSCSI, connector)
        self.iscsi_driver.terminate_connection(volume_iSCSI, connector)

    @ddt.data(({'is_multi_attach': True}, 1),
              ({'is_multi_attach': True}, 2),
              ({'is_multi_attach': False}, 1))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'initialize_host_info')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsvdiskhostmap')
    @mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                       '_do_terminate_connection')
    @mock.patch.object(cinder.db, 'volume_attachment_get_all_by_volume_id')
    @ddt.unpack
    def test_storwize_terminate_iscsi_multi_attach(self, vol_spec,
                                                   no_of_fake_attachments,
                                                   get_db_vol_attach,
                                                   do_term_conn, lsvdishosmap,
                                                   init_host_info):
        # create a iSCSI volume
        volume_iscsi = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> iSCSI'}
        vol_type_iscsi = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iscsi['volume_type_id'] = vol_type_iscsi['id']
        volume_iscsi['multiattach'] = vol_spec['is_multi_attach']

        connector1 = {'host': 'storwize-svc-host',
                      'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                      'wwpns': ['ff00000000000000', 'ff00000000000001'],
                      'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        connector2 = {'host': 'storwize-svc-host',
                      'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                      'wwpns': ['ff00000000000000', 'ff00000000000001'],
                      'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.iscsi_driver.initialize_connection(volume_iscsi, connector1)
        self.iscsi_driver.initialize_connection(volume_iscsi, connector2)
        init_host_info.assert_called()
        for conn in [connector1, connector2]:
            host = self.iscsi_driver._helpers.get_host_from_connector(
                conn, iscsi=True)
            self.assertIsNotNone(host)
        vol_updates = {'id': volume_iscsi['id'], 'size': 1}
        volume_model = models.Volume(**vol_updates)
        attachment_updates1 = {
            'volume': volume_model,
            'volume_id': volume_iscsi['id'],
            'id': '271eb937-5a5a-45bc-86a1-014afa8e4c37',
            'attach_status': 'attached',
            'attached_host': 'storwize-svc-host'
        }
        db_attachment1 = models.VolumeAttachment(**attachment_updates1)
        attachment_updates2 = {
            'volume': volume_model,
            'volume_id': volume_iscsi['id'],
            'id': '9a3b9fc4-2524-4367-8092-5382a43e5125',
            'attach_status': 'attached',
            'attached_host': 'storwize-svc-host'
        }
        db_attachment2 = models.VolumeAttachment(**attachment_updates2)
        if no_of_fake_attachments == 1:
            get_db_vol_attach.return_value = [db_attachment1]
        else:
            get_db_vol_attach.return_value = [db_attachment1, db_attachment2]

        attachments = objects.VolumeAttachmentList.get_all_by_volume_id(
            self.ctxt, volume_iscsi['id'])
        volume_iscsi['volume_attachment'] = attachments
        attachment_list = volume_iscsi['volume_attachment']
        attachment_count = 0
        if volume_iscsi['multiattach']:
            self.iscsi_driver.terminate_connection(volume_iscsi, connector1)
            try:
                for attachment in attachment_list:
                    if (attachment.attach_status == "attached" and
                       attachment.attached_host == "storwize-svc-host"):
                        attachment_count += 1
            except AttributeError:
                pass
            if attachment_count > 1:
                self.assertEqual(0, do_term_conn.call_count)

    def test_storwize_get_host_from_connector_with_both_fc_iscsi_host(self):
        volume_iSCSI = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> iSCSI'}
        vol_type_iSCSI = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iSCSI['volume_type_id'] = vol_type_iSCSI['id']

        connector = {'host': 'storwize-svc-host',
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        if self.USESIM:
            self.sim._cmd_mkhost(name='storwize-svc-host-99999999',
                                 hbawwpn='123')
            self.iscsi_driver.initialize_connection(volume_iSCSI, connector)
            self.iscsi_driver.terminate_connection(volume_iSCSI, connector)

    def test_storwize_iscsi_connection_snapshot(self):
        # create a iSCSI volume
        volume_iSCSI = self._create_volume()
        snapshot = self._generate_snap_info(volume_iSCSI.id)
        self.iscsi_driver.create_snapshot(snapshot)
        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.iscsi_driver.initialize_connection_snapshot(snapshot, connector)
        self.iscsi_driver.terminate_connection_snapshot(snapshot, connector)

    def test_storwize_replication_failover_iscsi_connection_snapshot(self):
        volume_iSCSI = self._create_volume()
        snapshot = self._generate_snap_info(volume_iSCSI.id)
        self.iscsi_driver.create_snapshot(snapshot)
        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        # a snapshot of a replication failover volume. attach will be failed
        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_get_volume_replicated_type') as rep_type:
            rep_type.return_value = True
            with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                                   '_get_vol_sys_info') as sys_info:
                sys_info.return_value = {'volume_name': 'voliscsi',
                                         'backend_helper':
                                             'self._aux_backend_helpers',
                                         'node_state': 'self._state'}
                self.assertRaises(exception.VolumeDriverException,
                                  self.iscsi_driver.
                                  initialize_connection_snapshot,
                                  snapshot,
                                  connector)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'initialize_host_info')
    def test_storwize_initialize_iscsi_connection_with_host_site(self,
                                                                 init_host):
        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        volume_iSCSI_1 = self._create_volume()
        volume_iSCSI = self._create_volume()
        extra_spec = {'drivers:volume_topology': 'hyperswap',
                      'peer_pool': 'openstack1'}
        vol_type_iSCSI = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iSCSI['volume_type_id'] = vol_type_iSCSI['id']
        volume_iSCSI_2 = self._create_volume()
        volume_iSCSI_2['volume_type_id'] = vol_type_iSCSI['id']
        self.iscsi_driver.initialize_connection(volume_iSCSI, connector)
        init_host.assert_called()
        self.assertEqual(1, init_host.call_count)
        host_name = self.iscsi_driver._helpers.get_host_from_connector(
            connector, iscsi=True)
        host_info = self.iscsi_driver._helpers.ssh.lshost(host=host_name)
        self.assertEqual('site1', host_info[0]['site_name'])
        self.iscsi_driver.terminate_connection(volume_iSCSI, connector)
        self.iscsi_driver.initialize_connection(volume_iSCSI_1, connector)
        self.iscsi_driver.initialize_connection(volume_iSCSI, connector)

        host_site = {'site1': 'iqn.1993-08.org.debian:01:eac5ccc1aaa',
                     'site2': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        self._set_flag('storwize_preferred_host_site', host_site)
        self.assertRaises(exception.InvalidConfigurationValue,
                          self.iscsi_driver.initialize_connection,
                          volume_iSCSI_2,
                          connector)

    @mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                       '_do_terminate_connection')
    @mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                       '_do_initialize_connection')
    def test_storwize_do_terminate_iscsi_connection(self, init_conn,
                                                    term_conn):
        # create an iSCSI volume
        volume_iSCSI = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> iSCSI'}
        vol_type_iSCSI = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iSCSI['volume_type_id'] = vol_type_iSCSI['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.iscsi_driver.initialize_connection(volume_iSCSI, connector)
        self.iscsi_driver.terminate_connection(volume_iSCSI, connector)
        init_conn.assert_called_once_with(volume_iSCSI, connector)
        term_conn.assert_called_once_with(volume_iSCSI, connector)

    @mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                       '_do_terminate_connection')
    def test_storwize_initialize_iscsi_connection_failure(self, term_conn):
        # create an iSCSI volume
        volume_iSCSI = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> iSCSI'}
        vol_type_iSCSI = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iSCSI['volume_type_id'] = vol_type_iSCSI['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.iscsi_driver._state['storage_nodes'] = {}
        self.assertRaises(exception.VolumeBackendAPIException,
                          self.iscsi_driver.initialize_connection,
                          volume_iSCSI, connector)
        term_conn.assert_called_once_with(volume_iSCSI, connector)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'initialize_host_info')
    def test_storwize_terminate_iscsi_connection_multi_attach(self,
                                                              init_host_info):
        # create an iSCSI volume
        volume_iSCSI = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> iSCSI'}
        vol_type_iSCSI = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iSCSI['volume_type_id'] = vol_type_iSCSI['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        connector2 = {'host': 'STORWIZE-SVC-HOST',
                      'wwnns': ['30000090fa17311e', '30000090fa17311f'],
                      'wwpns': ['ffff000000000000', 'ffff000000000001'],
                      'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1bbb'}

        # map and unmap the volume to two hosts normal case
        self.iscsi_driver.initialize_connection(volume_iSCSI, connector)
        init_host_info.assert_called()
        self.iscsi_driver.initialize_connection(volume_iSCSI, connector2)
        for conn in [connector, connector2]:
            host = self.iscsi_driver._helpers.get_host_from_connector(
                conn, iscsi=True)
            self.assertIsNotNone(host)
        self.iscsi_driver.terminate_connection(volume_iSCSI, connector)
        self.iscsi_driver.terminate_connection(volume_iSCSI, connector2)
        # validate that the host entries are deleted
        for conn in [connector, connector2]:
            host = self.iscsi_driver._helpers.get_host_from_connector(conn)
            self.assertIsNone(host)
        # map and unmap the volume to two hosts with the mapping removed
        self.iscsi_driver.initialize_connection(volume_iSCSI, connector)
        self.iscsi_driver.initialize_connection(volume_iSCSI, connector2)
        # Test multiple attachments case
        host_name = self.iscsi_driver._helpers.get_host_from_connector(
            connector2, iscsi=True)
        self.iscsi_driver._helpers.unmap_vol_from_host(
            volume_iSCSI['name'], host_name)
        host_name = self.iscsi_driver._helpers.get_host_from_connector(
            connector2, iscsi=True)
        self.assertIsNotNone(host_name)
        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'rmvdiskhostmap') as rmmap:
            rmmap.side_effect = Exception('boom')
            self.iscsi_driver.terminate_connection(volume_iSCSI,
                                                   connector2)
            init_host_info.assert_called()
        host_name = self.iscsi_driver._helpers.get_host_from_connector(
            connector2, iscsi=True)
        self.assertIsNone(host_name)
        # Test single attachment case
        self.iscsi_driver._helpers.unmap_vol_from_host(
            volume_iSCSI['name'], host_name)
        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'rmvdiskhostmap') as rmmap:
            rmmap.side_effect = Exception('boom')
            self.iscsi_driver.terminate_connection(volume_iSCSI, connector)
        # validate that the host entries are deleted
        for conn in [connector, connector2]:
            host = self.iscsi_driver._helpers.get_host_from_connector(
                conn, iscsi=True)
        self.assertIsNone(host)

    def test_storwize_initialize_iscsi_connection_single_path(self):
        # Test the return value for _get_iscsi_properties

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        # Expected single path host-volume map return value
        exp_s_path = {'driver_volume_type': 'iscsi',
                      'data': {'target_discovered': False,
                               'target_iqn':
                                   'iqn.1982-01.com.ibm:1234.sim.node1',
                               'target_portal': '1.234.56.78:3260',
                               'target_lun': 0,
                               'auth_method': 'CHAP',
                               'discovery_auth_method': 'CHAP'}}

        volume_iSCSI = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> iSCSI'}
        vol_type_iSCSI = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iSCSI['volume_type_id'] = vol_type_iSCSI['id']

        # Make sure that the volumes have been created
        self._assert_vol_exists(volume_iSCSI['name'], True)

        # Check case where no hosts exist
        ret = self.iscsi_driver._helpers.get_host_from_connector(
            connector, iscsi=True)
        self.assertIsNone(ret)

        # Initialize connection to map volume to a host
        ret = self.iscsi_driver.initialize_connection(
            volume_iSCSI, connector)
        self.assertEqual(exp_s_path['driver_volume_type'],
                         ret['driver_volume_type'])

        # Check the single path host-volume map return value
        for k, v in exp_s_path['data'].items():
            self.assertEqual(v, ret['data'][k])

        ret = self.iscsi_driver._helpers.get_host_from_connector(
            connector, iscsi=True)
        self.assertIsNotNone(ret)

    def test_storwize_initialize_iscsi_connection_multipath(self):
        # Test the return value for _get_iscsi_properties

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa',
                     'multipath': True}

        # Expected multipath host-volume map return value
        exp_m_path = {'driver_volume_type': 'iscsi',
                      'data': {'target_discovered': False,
                               'target_iqn':
                                   'iqn.1982-01.com.ibm:1234.sim.node1',
                               'target_portal': '1.234.56.78:3260',
                               'target_lun': 0,
                               'target_iqns': [
                                   'iqn.1982-01.com.ibm:1234.sim.node1',
                                   'iqn.1982-01.com.ibm:1234.sim.node1',
                                   'iqn.1982-01.com.ibm:1234.sim.node2'],
                               'target_portals':
                                   ['1.234.56.78:3260',
                                    '1.234.56.80:3260',
                                    '1.234.56.79:3260'],
                               'target_luns': [0, 0, 0],
                               'auth_method': 'CHAP',
                               'discovery_auth_method': 'CHAP'}}

        volume_iSCSI = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> iSCSI'}
        vol_type_iSCSI = volume_types.create(self.ctxt, 'iSCSI', extra_spec)
        volume_iSCSI['volume_type_id'] = vol_type_iSCSI['id']

        # Check case where no hosts exist
        ret = self.iscsi_driver._helpers.get_host_from_connector(
            connector, iscsi=True)
        self.assertIsNone(ret)

        # Initialize connection to map volume to a host
        ret = self.iscsi_driver.initialize_connection(
            volume_iSCSI, connector)
        self.assertEqual(exp_m_path['driver_volume_type'],
                         ret['driver_volume_type'])

        # Check the multipath host-volume map return value
        # target_iqns and target_portals have no guaranteed order
        self.assertCountEqual(exp_m_path['data']['target_iqns'],
                              ret['data']['target_iqns'])
        del exp_m_path['data']['target_iqns']

        self.assertCountEqual(exp_m_path['data']['target_portals'],
                              ret['data']['target_portals'])
        del exp_m_path['data']['target_portals']

        for k, v in exp_m_path['data'].items():
            self.assertEqual(v, ret['data'][k])

        ret = self.iscsi_driver._helpers.get_host_from_connector(
            connector, iscsi=True)
        self.assertIsNotNone(ret)

    def test_storwize_svc_iscsi_host_maps(self):
        # Create two volumes to be used in mappings

        ctxt = context.get_admin_context()
        volume1 = self._generate_vol_info(None, None)
        self.iscsi_driver.create_volume(volume1)
        volume2 = self._generate_vol_info(None, None)
        self.iscsi_driver.create_volume(volume2)

        # Create volume types that we created
        types = {}
        for protocol in ['iSCSI']:
            opts = {'storage_protocol': '<in> ' + protocol}
            types[protocol] = volume_types.create(ctxt, protocol, opts)

        expected = {'iSCSI': {'driver_volume_type': 'iscsi',
                              'data': {'target_discovered': False,
                                       'target_iqn':
                                       'iqn.1982-01.com.ibm:1234.sim.node1',
                                       'target_portal': '1.234.56.78:3260',
                                       'target_lun': 0,
                                       'auth_method': 'CHAP',
                                       'discovery_auth_method': 'CHAP'}}}
        volume1['volume_type_id'] = types[protocol]['id']
        volume2['volume_type_id'] = types[protocol]['id']

        # Check case where no hosts exist
        if self.USESIM:
            ret = self.iscsi_driver._helpers.get_host_from_connector(
                self._connector)
            self.assertIsNone(ret)

        # Make sure that the volumes have been created
        self._assert_vol_exists(volume1['name'], True)
        self._assert_vol_exists(volume2['name'], True)

        # Initialize connection from the first volume to a host
        ret = self.iscsi_driver.initialize_connection(
            volume1, self._connector)
        self.assertEqual(expected[protocol]['driver_volume_type'],
                         ret['driver_volume_type'])
        for k, v in expected[protocol]['data'].items():
            self.assertEqual(v, ret['data'][k])

        # Initialize again, should notice it and do nothing
        ret = self.iscsi_driver.initialize_connection(
            volume1, self._connector)
        self.assertEqual(expected[protocol]['driver_volume_type'],
                         ret['driver_volume_type'])
        for k, v in expected[protocol]['data'].items():
            self.assertEqual(v, ret['data'][k])

        # Try to delete the 1st volume (should fail because it is mapped)
        self.assertRaises(exception.VolumeBackendAPIException,
                          self.iscsi_driver.delete_volume,
                          volume1)

        ret = self.iscsi_driver.terminate_connection(volume1,
                                                     self._connector)
        if self.USESIM:
            ret = self.iscsi_driver._helpers.get_host_from_connector(
                self._connector)
            self.assertIsNone(ret)

        # Check cases with no auth set for host
        if self.USESIM:
            for auth_enabled in [True, False]:
                for host_exists in ['yes-auth', 'yes-noauth', 'no']:
                    self._set_flag('storwize_svc_iscsi_chap_enabled',
                                   auth_enabled)
                    case = 'en' + str(
                        auth_enabled) + 'ex' + str(host_exists)
                    conn_na = {'initiator': 'test:init:%s' %
                                            random.randint(10000, 99999),
                               'ip': '11.11.11.11',
                               'host': 'host-%s' % case}
                    if host_exists.startswith('yes'):
                        self.sim._add_host_to_list(conn_na)
                        if host_exists == 'yes-auth':
                            kwargs = {'chapsecret': 'foo',
                                      'obj': conn_na['host']}
                            self.sim._cmd_chhost(**kwargs)
                    volume1['volume_type_id'] = types['iSCSI']['id']

                    init_ret = self.iscsi_driver.initialize_connection(volume1,
                                                                       conn_na)
                    host_name = self.sim._host_in_list(conn_na['host'])
                    chap_ret = (
                        self.iscsi_driver._helpers.get_chap_secret_for_host(
                            host_name))
                    if auth_enabled or host_exists == 'yes-auth':
                        self.assertIn('auth_password', init_ret['data'])
                        self.assertIsNotNone(chap_ret)
                    else:
                        self.assertNotIn('auth_password', init_ret['data'])
                        self.assertIsNone(chap_ret)
                    self.iscsi_driver.terminate_connection(volume1, conn_na)
        self._set_flag('storwize_svc_iscsi_chap_enabled', True)

        # Test no preferred node
        if self.USESIM:
            self.sim.error_injection('lsvdisk', 'no_pref_node')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.iscsi_driver.initialize_connection,
                              volume1, self._connector)

        # Initialize connection from the second volume to the host with no
        # preferred node set if in simulation mode, otherwise, just
        # another initialize connection.
        if self.USESIM:
            self.sim.error_injection('lsvdisk', 'blank_pref_node')
        self.iscsi_driver.initialize_connection(volume2, self._connector)

        # Try to remove connection from host that doesn't exist (should fail)
        conn_no_exist = self._connector.copy()
        conn_no_exist['initiator'] = 'i_dont_exist'
        conn_no_exist['wwpns'] = ['0000000000000000']
        self.assertRaises(exception.VolumeDriverException,
                          self.iscsi_driver.terminate_connection,
                          volume1,
                          conn_no_exist)

        # Try to remove connection from volume that isn't mapped (should print
        # message but NOT fail)
        unmapped_vol = self._generate_vol_info(None, None)
        self.iscsi_driver.create_volume(unmapped_vol)
        self.iscsi_driver.terminate_connection(unmapped_vol, self._connector)
        self.iscsi_driver.delete_volume(unmapped_vol)

        # Remove the mapping from the 1st volume and delete it
        self.iscsi_driver.terminate_connection(volume1, self._connector)
        self.iscsi_driver.delete_volume(volume1)
        self._assert_vol_exists(volume1['name'], False)

        # Make sure our host still exists
        host_name = self.iscsi_driver._helpers.get_host_from_connector(
            self._connector, iscsi=True)
        self.assertIsNotNone(host_name)

        # Remove the mapping from the 2nd volume. The host should
        # be automatically removed because there are no more mappings.
        self.iscsi_driver.terminate_connection(volume2, self._connector)

        # Check if we successfully terminate connections when the host is not
        # specified (see bug #1244257)
        fake_conn = {'ip': '127.0.0.1', 'initiator': 'iqn.fake'}
        self.iscsi_driver.initialize_connection(volume2, self._connector)
        host_name = self.iscsi_driver._helpers.get_host_from_connector(
            self._connector, iscsi=True)
        self.assertIsNotNone(host_name)
        self.iscsi_driver.terminate_connection(volume2, fake_conn)
        host_name = self.iscsi_driver._helpers.get_host_from_connector(
            self._connector, iscsi=True)
        self.assertIsNone(host_name)
        self.iscsi_driver.delete_volume(volume2)
        self._assert_vol_exists(volume2['name'], False)

        # Delete volume types that we created
        for protocol in ['iSCSI']:
            volume_types.destroy(ctxt, types[protocol]['id'])

        # Check if our host still exists (it should not)
        if self.USESIM:
            ret = (
                self.iscsi_driver._helpers.get_host_from_connector(
                    self._connector, iscsi=True))
            self.assertIsNone(ret)

    def test_storwize_svc_iscsi_multi_host_maps(self):
        # We can't test connecting to multiple hosts from a single host when
        # using real storage
        if not self.USESIM:
            return

        # Create a volume to be used in mappings
        ctxt = context.get_admin_context()
        volume = self._generate_vol_info(None, None)
        self.iscsi_driver.create_volume(volume)

        # Create volume types for protocols
        types = {}
        for protocol in ['iSCSI']:
            opts = {'storage_protocol': '<in> ' + protocol}
            types[protocol] = volume_types.create(ctxt, protocol, opts)

        # Create a connector for the second 'host'
        wwpns = [str(random.randint(0, 9999999999999999)).zfill(16),
                 str(random.randint(0, 9999999999999999)).zfill(16)]
        initiator = 'test.initiator.%s' % str(random.randint(10000, 99999))
        conn2 = {'ip': '1.234.56.79',
                 'host': 'storwize-svc-test2',
                 'wwpns': wwpns,
                 'initiator': initiator}

        # Check protocols for iSCSI
        volume['volume_type_id'] = types[protocol]['id']

        # Make sure that the volume has been created
        self._assert_vol_exists(volume['name'], True)

        self.iscsi_driver.initialize_connection(volume, self._connector)

        self._set_flag('storwize_svc_multihostmap_enabled', False)
        self.assertRaises(
            exception.CinderException,
            self.iscsi_driver.initialize_connection, volume, conn2)

        self._set_flag('storwize_svc_multihostmap_enabled', True)
        self.iscsi_driver.initialize_connection(volume, conn2)

        self.iscsi_driver.terminate_connection(volume, conn2)
        self.iscsi_driver.terminate_connection(volume, self._connector)

    def test_add_vdisk_copy_iscsi(self):
        # Ensure only iSCSI is available
        self.iscsi_driver._state['enabled_protocols'] = set(['iSCSI'])
        volume = self._generate_vol_info(None, None)
        self.iscsi_driver.create_volume(volume)
        self.iscsi_driver.add_vdisk_copy(volume['name'], 'fake-pool', None)


@ddt.ddt
class StorwizeSVCFcDriverTestCase(test.TestCase):
    @mock.patch.object(time, 'sleep')
    def setUp(self, mock_sleep):
        super(StorwizeSVCFcDriverTestCase, self).setUp()
        self.USESIM = True
        if self.USESIM:
            self.fc_driver = StorwizeSVCFcFakeDriver(
                configuration=conf.Configuration(None))
            self._def_flags = {'san_ip': 'hostname',
                               'san_login': 'user',
                               'san_password': 'pass',
                               'storwize_svc_volpool_name':
                               SVC_POOLS,
                               'storwize_svc_flashcopy_timeout': 20,
                               'storwize_svc_flashcopy_rate': 49,
                               'storwize_svc_multipath_enabled': False,
                               'storwize_svc_allow_tenant_qos': True}
            wwpns = [
                str(random.randint(0, 9999999999999999)).zfill(16),
                str(random.randint(0, 9999999999999999)).zfill(16)]
            initiator = 'test.initiator.%s' % str(
                random.randint(10000, 99999))
            self._connector = {'ip': '1.234.56.78',
                               'host': 'storwize-svc-test',
                               'wwpns': wwpns,
                               'initiator': initiator}
            self.sim = StorwizeSVCManagementSimulator(SVC_POOLS)

            self.fc_driver.set_fake_storage(self.sim)
            self.ctxt = context.get_admin_context()

        self._reset_flags()
        self.ctxt = context.get_admin_context()
        self.db = cinder.db
        self.fc_driver.db = self.db
        self.fc_driver.do_setup(None)
        self.fc_driver.check_for_setup_error()
        self.fc_driver._helpers.check_fcmapping_interval = 0

    def _set_flag(self, flag, value):
        group = self.fc_driver.configuration.config_group
        self.fc_driver.configuration.set_override(flag, value, group)

    def _reset_flags(self):
        self.fc_driver.configuration.local_conf.reset()
        for k, v in self._def_flags.items():
            self._set_flag(k, v)

    def _create_volume(self, **kwargs):
        pool = _get_test_pool()
        prop = {'host': 'openstack@svc#%s' % pool,
                'size': 1,
                'volume_type_id': self.vt['id']}
        for p in prop.keys():
            if p not in kwargs:
                kwargs[p] = prop[p]
        vol = testutils.create_volume(self.ctxt, **kwargs)
        self.fc_driver.create_volume(vol)
        return vol

    def _delete_volume(self, volume):
        self.fc_driver.delete_volume(volume)
        self.db.volume_destroy(self.ctxt, volume['id'])

    def _generate_vol_info(self, vol_name, vol_id):
        pool = _get_test_pool()
        prop = {'mdisk_grp_name': pool}
        if vol_name:
            prop.update(volume_name=vol_name,
                        volume_id=vol_id,
                        volume_size=10)
        else:
            prop.update(size=10,
                        volume_type_id=None,
                        mdisk_grp_name=pool,
                        host='openstack@svc#%s' % pool)
        vol = testutils.create_volume(self.ctxt, **prop)
        return vol

    def _generate_snap_info(self, vol_id, size=10):
        prop = {'volume_id': vol_id,
                'volume_size': size}
        snap = testutils.create_snapshot(self.ctxt, **prop)
        return snap

    def _assert_vol_exists(self, name, exists):
        is_vol_defined = self.fc_driver._helpers.is_vdisk_defined(name)
        self.assertEqual(exists, is_vol_defined)

    def test_storwize_get_host_with_fc_connection(self):
        # Create a FC host
        del self._connector['initiator']
        helper = self.fc_driver._helpers
        host_name = helper.create_host(self._connector)

        # Remove the first wwpn from connector, and then try get host
        wwpns = self._connector['wwpns']
        wwpns.remove(wwpns[0])
        host_name = helper.get_host_from_connector(self._connector)

        self.assertIsNotNone(host_name)

    def test_storwize_fc_connection_snapshot(self):
        # create a fc volume snapshot
        volume_fc = self._create_volume()
        snapshot = self._generate_snap_info(volume_fc.id)
        self.fc_driver.create_snapshot(snapshot)
        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.fc_driver.initialize_connection_snapshot(snapshot, connector)
        self.fc_driver.terminate_connection_snapshot(snapshot, connector)

    def test_storwize_replication_failover_fc_connection_snapshot(self):
        volume_fc = self._create_volume()
        volume_fc['replication_status'] = fields.ReplicationStatus.FAILED_OVER
        snapshot = self._generate_snap_info(volume_fc.id)
        self.fc_driver.create_snapshot(snapshot)
        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        # a snapshot of a replication failover volume. attach will be failed
        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_get_volume_replicated_type') as rep_type:
            rep_type.return_value = True
            with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                                   '_get_vol_sys_info') as sys_info:
                sys_info.return_value = {'volume_name': 'volfc',
                                         'backend_helper':
                                             'self._aux_backend_helpers',
                                         'node_state': 'self._state'}
                self.assertRaises(exception.VolumeDriverException,
                                  self.fc_driver.
                                  initialize_connection_snapshot,
                                  snapshot,
                                  connector)

    def test_storwize_get_host_with_fc_connection_with_volume(self):
        # create a FC volume
        volume_fc = self._generate_vol_info(None, None)
        self.fc_driver.create_volume(volume_fc)
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type_fc = volume_types.create(self.ctxt, 'FC', extra_spec)
        volume_fc['volume_type_id'] = vol_type_fc['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        self.fc_driver.initialize_connection(volume_fc, connector)
        # Create a FC host
        helper = self.fc_driver._helpers

        host_name = helper.get_host_from_connector(
            connector, volume_fc['name'])
        self.assertIsNotNone(host_name)

    def test_storwize_get_host_from_connector_with_lshost_failure(self):
        self._connector.pop('initiator')
        helper = self.fc_driver._helpers
        # Create two hosts. The first is not related to the connector and
        # we use the simulator for that. The second is for the connector.
        # We will force the missing_host error for the first host, but
        # then tolerate and find the second host on the slow path normally.
        if self.USESIM:
            self.sim._cmd_mkhost(name='storwize-svc-test-9', hbawwpn='123456')
        helper.create_host(self._connector)
        # tell lshost to fail while calling get_host_from_connector
        if self.USESIM:
            # tell lshost to fail while called from get_host_from_connector
            self.sim.error_injection('lshost', 'missing_host')
            # tell lsfabric to skip rows so that we skip past fast path
            self.sim.error_injection('lsfabric', 'remove_rows')
        # Run test
        host_name = helper.get_host_from_connector(self._connector)

        self.assertIsNotNone(host_name)

    def test_storwize_get_host_from_connector_with_lshost_failure2(self):
        self._connector.pop('initiator')
        self._connector['wwpns'] = []  # Clearing will skip over fast-path
        helper = self.fc_driver._helpers
        if self.USESIM:
            # Add a host to the simulator. We don't need it to match the
            # connector since we will force a bad failure for lshost.
            self.sim._cmd_mkhost(name='DifferentHost', hbawwpn='123456')
            # tell lshost to fail badly while called from
            # get_host_from_connector
            self.sim.error_injection('lshost', 'bigger_troubles')
            self.assertRaises(exception.VolumeBackendAPIException,
                              helper.get_host_from_connector,
                              self._connector)

    def test_storwize_get_host_from_connector_not_found(self):
        self._connector.pop('initiator')
        helper = self.fc_driver._helpers
        # Create some hosts. The first is not related to the connector and
        # we use the simulator for that. The second is for the connector.
        # We will force the missing_host error for the first host, but
        # then tolerate and find the second host on the slow path normally.
        if self.USESIM:
            self.sim._cmd_mkhost(name='storwize-svc-test-3', hbawwpn='1234567')
            self.sim._cmd_mkhost(name='storwize-svc-test-2', hbawwpn='2345678')
            self.sim._cmd_mkhost(name='storwize-svc-test-1', hbawwpn='3456789')
            self.sim._cmd_mkhost(name='A-Different-host', hbawwpn='9345678')
            self.sim._cmd_mkhost(name='B-Different-host', hbawwpn='8345678')
            self.sim._cmd_mkhost(name='C-Different-host', hbawwpn='7345678')
        # tell lshost to fail while calling get_host_from_connector
        if self.USESIM:
            # tell lsfabric to skip rows so that we skip past fast path
            self.sim.error_injection('lsfabric', 'remove_rows')
        # Run test
        host_name = helper.get_host_from_connector(self._connector)

        self.assertIsNone(host_name)

    def test_storwize_get_host_from_connector_fast_path(self):
        self._connector.pop('initiator')
        helper = self.fc_driver._helpers
        # Create two hosts. Our lshost will return the hosts in sorted
        # Order. The extra host will be returned before the target
        # host. If we get detailed lshost info on our host without
        # gettting detailed info on the other host we used the fast path
        if self.USESIM:
            self.sim._cmd_mkhost(name='A-DifferentHost', hbawwpn='123456')
        helper.create_host(self._connector)
        # tell lshost to fail while calling get_host_from_connector
        if self.USESIM:
            # tell lshost to fail while called from get_host_from_connector
            self.sim.error_injection('lshost', 'fail_fastpath')
            # tell lsfabric to skip rows so that we skip past fast path
            self.sim.error_injection('lsfabric', 'remove_rows')
        # Run test
        host_name = helper.get_host_from_connector(self._connector)

        self.assertIsNotNone(host_name)
        # Need to assert that lshost was actually called. The way
        # we do that is check that the next simulator error for lshost
        # has not been reset.
        self.assertEqual(self.sim._next_cmd_error['lshost'], 'fail_fastpath',
                         "lshost was not called in the simulator. The "
                         "queued error still remains.")

    def test_storwize_initiator_multiple_wwpns_connected(self):

        # Generate us a test volume
        volume = self._create_volume()

        # Fibre Channel volume type
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type = volume_types.create(self.ctxt, 'FC', extra_spec)

        volume['volume_type_id'] = vol_type['id']

        # Make sure that the volumes have been created
        self._assert_vol_exists(volume['name'], True)

        # Set up one WWPN that won't match and one that will.
        self.fc_driver._state['storage_nodes']['1']['WWPN'] = [
            '123456789ABCDEF0', 'AABBCCDDEEFF0010']

        wwpns = ['ff00000000000000', 'ff00000000000001']
        connector = {'host': 'storwize-svc-test', 'wwpns': wwpns}

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_conn_fc_wwpns') as get_mappings:
            mapped_wwpns = ['AABBCCDDEEFF0001', 'AABBCCDDEEFF0002',
                            'AABBCCDDEEFF0010', 'AABBCCDDEEFF0012']
            get_mappings.return_value = mapped_wwpns

            # Initialize the connection
            init_ret = self.fc_driver.initialize_connection(volume, connector)

            # Make sure we return all wwpns which where mapped as part of the
            # connection
            self.assertEqual(mapped_wwpns,
                             init_ret['data']['target_wwn'])

    def test_storwize_svc_fc_validate_connector(self):
        conn_neither = {'host': 'host'}
        conn_iscsi = {'host': 'host', 'initiator': 'foo'}
        conn_fc = {'host': 'host', 'wwpns': 'bar'}
        conn_both = {'host': 'host', 'initiator': 'foo', 'wwpns': 'bar'}

        self.fc_driver._state['enabled_protocols'] = set(['FC'])
        self.fc_driver.validate_connector(conn_fc)
        self.fc_driver.validate_connector(conn_both)
        self.assertRaises(exception.InvalidConnectorException,
                          self.fc_driver.validate_connector, conn_iscsi)
        self.assertRaises(exception.InvalidConnectorException,
                          self.fc_driver.validate_connector, conn_neither)

        self.fc_driver._state['enabled_protocols'] = set(['iSCSI', 'FC'])
        self.fc_driver.validate_connector(conn_fc)
        self.fc_driver.validate_connector(conn_both)
        self.assertRaises(exception.InvalidConnectorException,
                          self.fc_driver.validate_connector, conn_neither)

    def test_storwize_terminate_fc_connection(self):
        # create a FC volume
        volume_fc = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type_fc = volume_types.create(self.ctxt, 'FC', extra_spec)
        volume_fc['volume_type_id'] = vol_type_fc['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.fc_driver.initialize_connection(volume_fc, connector)
        self.fc_driver.initialize_connection(volume_fc, connector)
        self.fc_driver.terminate_connection(volume_fc, connector)
        with mock.patch.object(
                storwize_svc_common.StorwizeSSH,
                'mkvdiskhostmap') as mkvdiskhostmap:
            ex = exception.VolumeBackendAPIException(data='CMMVC5879E')
            mkvdiskhostmap.side_effect = [ex, ex, mock.MagicMock()]
            self.fc_driver.initialize_connection(volume_fc, connector)
            self.fc_driver.terminate_connection(volume_fc, connector)
            mkvdiskhostmap.side_effect = ex
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.fc_driver.initialize_connection,
                              volume_fc,
                              connector)
            ex1 = exception.VolumeBackendAPIException(data='CMMVC6071E')
            mkvdiskhostmap.side_effect = ex1
            self._set_flag('storwize_svc_multihostmap_enabled', False)
            self.assertRaises(exception.VolumeDriverException,
                              self.fc_driver.initialize_connection,
                              volume_fc,
                              connector)
            ex2 = exception.VolumeBackendAPIException(data='CMMVC5707E')
            mkvdiskhostmap.side_effect = ex2
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.fc_driver.initialize_connection,
                              volume_fc,
                              connector)

    def test_storwize_initialize_fc_connection_with_host_site(self):
        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ffff000000000000', 'ffff000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        # attach hyperswap volume without host_site
        volume_fc = self._create_volume()
        extra_spec = {'drivers:volume_topology': 'hyperswap',
                      'peer_pool': 'openstack1'}
        vol_type_fc = volume_types.create(self.ctxt, 'FC', extra_spec)
        volume_fc['volume_type_id'] = vol_type_fc['id']
        volume_fc_2 = self._create_volume()
        volume_fc_2['volume_type_id'] = vol_type_fc['id']

        self.assertRaises(exception.VolumeDriverException,
                          self.fc_driver.initialize_connection,
                          volume_fc,
                          connector)
        # the wwpns of 1 host config to 2 different sites
        host_site = {'site1': 'ffff000000000000',
                     'site2': 'ffff000000000001'}
        self.fc_driver.configuration.set_override(
            'storwize_preferred_host_site', host_site)
        self.assertRaises(exception.InvalidConfigurationValue,
                          self.fc_driver.initialize_connection,
                          volume_fc,
                          connector)
        # All the wwpns of this host are not configured.
        host_site_2 = {'site1': 'ff00000000000000',
                       'site2': 'ff00000000000001'}
        self.fc_driver.configuration.set_override(
            'storwize_preferred_host_site', host_site_2)
        self.assertRaises(exception.VolumeDriverException,
                          self.fc_driver.initialize_connection,
                          volume_fc,
                          connector)

        # All the wwpns of this host are configured
        host_site_3 = {'site1': 'ffff000000000000&ffff000000000001'}
        self.fc_driver.configuration.set_override(
            'storwize_preferred_host_site', host_site_3)
        self.fc_driver.initialize_connection(volume_fc, connector)
        host_name = self.fc_driver._helpers.get_host_from_connector(
            connector, iscsi=True)
        host_info = self.fc_driver._helpers.ssh.lshost(host=host_name)
        self.assertEqual('site1', host_info[0]['site_name'])

        # Partial wwpns of this host are configured
        host_site_4 = {'site1': 'ff00000000000000',
                       'site2': 'ffff000000000001'}
        self.fc_driver.configuration.set_override(
            'storwize_preferred_host_site', host_site_4)
        self.assertRaises(exception.InvalidConfigurationValue,
                          self.fc_driver.initialize_connection,
                          volume_fc_2,
                          connector)

    @mock.patch.object(storwize_svc_fc.StorwizeSVCFCDriver,
                       '_do_terminate_connection')
    @mock.patch.object(storwize_svc_fc.StorwizeSVCFCDriver,
                       '_do_initialize_connection')
    def test_storwize_do_terminate_fc_connection(self, init_conn,
                                                 term_conn):
        # create a FC volume
        volume_fc = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type_fc = volume_types.create(self.ctxt, 'FC', extra_spec)
        volume_fc['volume_type_id'] = vol_type_fc['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.fc_driver.initialize_connection(volume_fc, connector)
        self.fc_driver.terminate_connection(volume_fc, connector)
        init_conn.assert_called_once_with(volume_fc, connector)
        term_conn.assert_called_once_with(volume_fc, connector)

    @mock.patch.object(storwize_svc_fc.StorwizeSVCFCDriver,
                       '_do_terminate_connection')
    def test_storwize_initialize_fc_connection_failure(self, term_conn):
        # create a FC volume
        volume_fc = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type_fc = volume_types.create(self.ctxt, 'FC', extra_spec)
        volume_fc['volume_type_id'] = vol_type_fc['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.fc_driver._state['storage_nodes'] = {}
        self.assertRaises(exception.VolumeBackendAPIException,
                          self.fc_driver.initialize_connection,
                          volume_fc, connector)
        term_conn.assert_called_once_with(volume_fc, connector)

    @ddt.data(({'is_multi_attach': True}, 1),
              ({'is_multi_attach': True}, 2),
              ({'is_multi_attach': False}, 1))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'initialize_host_info')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsvdiskhostmap')
    @mock.patch.object(storwize_svc_fc.StorwizeSVCFCDriver,
                       '_do_terminate_connection')
    @mock.patch.object(cinder.db, 'volume_attachment_get_all_by_volume_id')
    @ddt.unpack
    def test_storwize_terminate_conn_fc_multi_attach(self, vol_spec,
                                                     no_of_fake_attachments,
                                                     get_db_vol_attach,
                                                     do_term_conn,
                                                     lsvdishosmap,
                                                     init_host_info):
        # create a FC  volume
        volume_fc = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type_fc = volume_types.create(self.ctxt, 'FC', extra_spec)
        volume_fc['volume_type_id'] = vol_type_fc['id']
        volume_fc['multiattach'] = vol_spec['is_multi_attach']
        connector1 = {'host': 'storwize-svc-host',
                      'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                      'wwpns': ['ff00000000000000', 'ff00000000000001'],
                      'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        connector2 = {'host': 'storwize-svc-host',
                      'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                      'wwpns': ['ff00000000000000', 'ff00000000000001'],
                      'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}

        self.fc_driver.initialize_connection(volume_fc, connector1)
        self.fc_driver.initialize_connection(volume_fc, connector2)
        init_host_info.assert_called()
        for conn in [connector1, connector2]:
            host = self.fc_driver._helpers.get_host_from_connector(conn)
            self.assertIsNotNone(host)
        vol_updates = {'id': volume_fc['id'], 'size': 1}
        volume_model = models.Volume(**vol_updates)
        attachment_updates = {
            'volume': volume_model,
            'volume_id': volume_fc['id'],
            'id': '271eb937-5a5a-45bc-86a1-014afa8e4c37',
            'attach_status': 'attached',
            'attached_host': 'storwize-svc-host'
        }
        db_attachment1 = models.VolumeAttachment(**attachment_updates)
        attachment_updates2 = {
            'volume': volume_model,
            'volume_id': volume_fc['id'],
            'id': '9a3b9fc4-2524-4367-8092-5382a43e5125',
            'attach_status': 'attached',
            'attached_host': 'storwize-svc-host'
        }
        db_attachment2 = models.VolumeAttachment(**attachment_updates2)
        if no_of_fake_attachments == 1:
            get_db_vol_attach.return_value = [db_attachment1]
        else:
            get_db_vol_attach.return_value = [db_attachment1, db_attachment2]

        attachments = objects.VolumeAttachmentList.get_all_by_volume_id(
            self.ctxt, volume_fc['id'])
        volume_fc['volume_attachment'] = attachments
        attachment_list = volume_fc['volume_attachment']
        attachment_count = 0
        if volume_fc['multiattach']:
            self.fc_driver.terminate_connection(volume_fc, connector1)
            try:
                for attachment in attachment_list:
                    if (attachment.attach_status == "attached" and
                       attachment.attached_host == "storwize-svc-host"):
                        attachment_count += 1
            except AttributeError:
                pass
            if attachment_count > 1:
                self.assertEqual(0, do_term_conn.call_count)

    def test_storwize_terminate_fc_connection_multi_attach(self):
        # create a FC volume
        volume_fc = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type_fc = volume_types.create(self.ctxt, 'FC', extra_spec)
        volume_fc['volume_type_id'] = vol_type_fc['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        connector2 = {'host': 'STORWIZE-SVC-HOST',
                      'wwnns': ['30000090fa17311e', '30000090fa17311f'],
                      'wwpns': ['ffff000000000000', 'ffff000000000001'],
                      'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1bbb'}

        # map and unmap the volume to two hosts normal case
        self.fc_driver.initialize_connection(volume_fc, connector)
        self.fc_driver.initialize_connection(volume_fc, connector2)
        # validate that the host entries are created
        for conn in [connector, connector2]:
            host = self.fc_driver._helpers.get_host_from_connector(conn)
            self.assertIsNotNone(host)
        self.fc_driver.terminate_connection(volume_fc, connector)
        self.fc_driver.terminate_connection(volume_fc, connector2)
        # validate that the host entries are deleted
        for conn in [connector, connector2]:
            host = self.fc_driver._helpers.get_host_from_connector(conn)
            self.assertIsNone(host)
        # map and unmap the volume to two hosts with the mapping gone
        self.fc_driver.initialize_connection(volume_fc, connector)
        self.fc_driver.initialize_connection(volume_fc, connector2)
        # Test multiple attachments case
        host_name = self.fc_driver._helpers.get_host_from_connector(connector2)
        self.fc_driver._helpers.unmap_vol_from_host(
            volume_fc['name'], host_name)
        host_name = self.fc_driver._helpers.get_host_from_connector(connector2)
        self.assertIsNotNone(host_name)
        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'rmvdiskhostmap') as rmmap:
            rmmap.side_effect = Exception('boom')
            self.fc_driver.terminate_connection(volume_fc, connector2)
        host_name = self.fc_driver._helpers.get_host_from_connector(connector2)
        self.assertIsNone(host_name)
        # Test single attachment case
        self.fc_driver._helpers.unmap_vol_from_host(
            volume_fc['name'], host_name)
        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'rmvdiskhostmap') as rmmap:
            rmmap.side_effect = Exception('boom')
            self.fc_driver.terminate_connection(volume_fc, connector)
        # validate that the host entries are deleted
        for conn in [connector, connector2]:
            host = self.fc_driver._helpers.get_host_from_connector(conn)
        self.assertIsNone(host)

    def test_storwize_initiator_target_map(self):
        # Generate us a test volume
        volume = self._create_volume()

        # FIbre Channel volume type
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type = volume_types.create(self.ctxt, 'FC', extra_spec)

        volume['volume_type_id'] = vol_type['id']

        # Make sure that the volumes have been created
        self._assert_vol_exists(volume['name'], True)

        wwpns = ['ff00000000000000', 'ff00000000000001']
        connector = {'host': 'storwize-svc-test', 'wwpns': wwpns}

        # Initialise the connection
        init_ret = self.fc_driver.initialize_connection(volume, connector)

        # Check that the initiator_target_map is as expected
        init_data = {'driver_volume_type': 'fibre_channel',
                     'data': {'initiator_target_map':
                              {'ff00000000000000': ['AABBCCDDEEFF0011'],
                               'ff00000000000001': ['AABBCCDDEEFF0011']},
                              'target_discovered': False,
                              'target_lun': 0,
                              'target_wwn': ['AABBCCDDEEFF0011'],
                              'volume_id': volume['id']
                              }
                     }

        self.assertEqual(init_data, init_ret)

        # Terminate connection
        term_ret = self.fc_driver.terminate_connection(volume, connector)

        # Check that the initiator_target_map is as expected
        term_data = {'driver_volume_type': 'fibre_channel',
                     'data': {'initiator_target_map':
                              {'ff00000000000000': ['5005076802432ADE',
                                                    '5005076802332ADE',
                                                    '5005076802532ADE',
                                                    '5005076802232ADE',
                                                    '5005076802132ADE',
                                                    '5005086802132ADE',
                                                    '5005086802332ADE',
                                                    '5005086802532ADE',
                                                    '5005086802232ADE',
                                                    '5005086802432ADE'],
                               'ff00000000000001': ['5005076802432ADE',
                                                    '5005076802332ADE',
                                                    '5005076802532ADE',
                                                    '5005076802232ADE',
                                                    '5005076802132ADE',
                                                    '5005086802132ADE',
                                                    '5005086802332ADE',
                                                    '5005086802532ADE',
                                                    '5005086802232ADE',
                                                    '5005086802432ADE']}
                              }
                     }

        self.assertCountEqual(term_data, term_ret)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_conn_fc_wwpns')
    def test_storwize_npiv_initiator_target_map(self, get_fc_wwpns):
        # create a FC volume
        get_fc_wwpns.side_effect = [[]]
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'standard',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.fc_driver.do_setup(None)
        volume_fc = self._create_volume()
        extra_spec = {'capabilities:storage_protocol': '<in> FC'}
        vol_type_fc = volume_types.create(self.ctxt, 'FC', extra_spec)
        volume_fc['volume_type_id'] = vol_type_fc['id']

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        conn_info = self.fc_driver.initialize_connection(volume_fc, connector)
        expected_target_wwn = ['5005076801A91806',
                               '5005076801A96CFE',
                               '5005076801996CFE',
                               '5005076801991806']
        self.assertCountEqual(expected_target_wwn, conn_info[
            'data']['target_wwn'])

        # Terminate connection
        term_ret = self.fc_driver.terminate_connection(volume_fc, connector)
        target_wwn1 = term_ret['data']['initiator_target_map'][
            'ff00000000000000']
        target_wwn2 = term_ret['data']['initiator_target_map'][
            'ff00000000000001']

        # Check that the initiator_target_map is as expected
        expected_term_data = ['5005076801A96CFE',
                              '5005076801A91806',
                              '5005076801201806',
                              '5005076801991806',
                              '5005076801101806',
                              '5005076801996CFE',
                              '5005076801206CFE',
                              '5005076801106CFE']
        self.assertCountEqual(expected_term_data, target_wwn1)
        self.assertCountEqual(expected_term_data, target_wwn2)

    def test_storwize_svc_fc_host_maps(self):
        # Create two volumes to be used in mappings

        ctxt = context.get_admin_context()
        volume1 = self._generate_vol_info(None, None)
        self.fc_driver.create_volume(volume1)
        volume2 = self._generate_vol_info(None, None)
        self.fc_driver.create_volume(volume2)

        # Create volume types that we created
        types = {}
        for protocol in ['FC']:
            opts = {'storage_protocol': '<in> ' + protocol}
            types[protocol] = volume_types.create(ctxt, protocol, opts)

        expected = {'FC': {'driver_volume_type': 'fibre_channel',
                           'data': {'target_lun': 0,
                                    'target_wwn': ['AABBCCDDEEFF0011'],
                                    'target_discovered': False}}}

        volume1['volume_type_id'] = types[protocol]['id']
        volume2['volume_type_id'] = types[protocol]['id']

        # Check case where no hosts exist
        if self.USESIM:
            ret = self.fc_driver._helpers.get_host_from_connector(
                self._connector)
            self.assertIsNone(ret)

        # Make sure that the volumes have been created
        self._assert_vol_exists(volume1['name'], True)
        self._assert_vol_exists(volume2['name'], True)

        # Initialize connection from the first volume to a host
        ret = self.fc_driver.initialize_connection(
            volume1, self._connector)
        self.assertEqual(expected[protocol]['driver_volume_type'],
                         ret['driver_volume_type'])
        for k, v in expected[protocol]['data'].items():
            self.assertEqual(v, ret['data'][k])

        # Initialize again, should notice it and do nothing
        ret = self.fc_driver.initialize_connection(
            volume1, self._connector)
        self.assertEqual(expected[protocol]['driver_volume_type'],
                         ret['driver_volume_type'])
        for k, v in expected[protocol]['data'].items():
            self.assertEqual(v, ret['data'][k])

        # Try to delete the 1st volume (should fail because it is mapped)
        self.assertRaises(exception.VolumeBackendAPIException,
                          self.fc_driver.delete_volume,
                          volume1)

        # Check bad output from lsfabric for the 2nd volume
        if protocol == 'FC' and self.USESIM:
            with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                                   'get_conn_fc_wwpns') as conn_fc_wwpns:
                conn_fc_wwpns.return_value = []
                ret = self.fc_driver.initialize_connection(volume2,
                                                           self._connector)

        ret = self.fc_driver.terminate_connection(volume1, self._connector)
        if protocol == 'FC' and self.USESIM:
            # For the first volume detach, ret['data'] should be empty
            # only ret['driver_volume_type'] returned
            self.assertEqual({}, ret['data'])
            self.assertEqual('fibre_channel', ret['driver_volume_type'])
            ret = self.fc_driver.terminate_connection(volume2,
                                                      self._connector)
            self.assertEqual('fibre_channel', ret['driver_volume_type'])
            # wwpn is randomly created
            self.assertNotEqual({}, ret['data'])
        if self.USESIM:
            ret = self.fc_driver._helpers.get_host_from_connector(
                self._connector)
            self.assertIsNone(ret)

        # Test no preferred node
        if self.USESIM:
            self.sim.error_injection('lsvdisk', 'no_pref_node')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.fc_driver.initialize_connection,
                              volume1, self._connector)

        # Initialize connection from the second volume to the host with no
        # preferred node set if in simulation mode, otherwise, just
        # another initialize connection.
        if self.USESIM:
            self.sim.error_injection('lsvdisk', 'blank_pref_node')
        self.fc_driver.initialize_connection(volume2, self._connector)

        # Try to remove connection from host that doesn't exist (should fail)
        conn_no_exist = self._connector.copy()
        conn_no_exist['initiator'] = 'i_dont_exist'
        conn_no_exist['wwpns'] = ['0000000000000000']
        self.assertRaises(exception.VolumeDriverException,
                          self.fc_driver.terminate_connection,
                          volume1,
                          conn_no_exist)

        # Try to remove connection from volume that isn't mapped (should print
        # message but NOT fail)
        unmapped_vol = self._generate_vol_info(None, None)
        self.fc_driver.create_volume(unmapped_vol)
        self.fc_driver.terminate_connection(unmapped_vol, self._connector)
        self.fc_driver.delete_volume(unmapped_vol)

        # Remove the mapping from the 1st volume and delete it
        self.fc_driver.terminate_connection(volume1, self._connector)
        self.fc_driver.delete_volume(volume1)
        self._assert_vol_exists(volume1['name'], False)

        # Make sure our host still exists
        host_name = self.fc_driver._helpers.get_host_from_connector(
            self._connector)
        self.assertIsNotNone(host_name)

        # Remove the mapping from the 2nd volume. The host should
        # be automatically removed because there are no more mappings.
        self.fc_driver.terminate_connection(volume2, self._connector)

        # Check if we successfully terminate connections when the host is not
        # specified (see bug #1244257)
        fake_conn = {'ip': '127.0.0.1', 'initiator': 'iqn.fake'}
        self.fc_driver.initialize_connection(volume2, self._connector)
        host_name = self.fc_driver._helpers.get_host_from_connector(
            self._connector)
        self.assertIsNotNone(host_name)
        self.fc_driver.terminate_connection(volume2, fake_conn)
        host_name = self.fc_driver._helpers.get_host_from_connector(
            self._connector)
        self.assertIsNone(host_name)
        self.fc_driver.delete_volume(volume2)
        self._assert_vol_exists(volume2['name'], False)

        # Delete volume types that we created
        for protocol in ['FC']:
            volume_types.destroy(ctxt, types[protocol]['id'])

        # Check if our host still exists (it should not)
        if self.USESIM:
            ret = (self.fc_driver._helpers.get_host_from_connector(
                self._connector))
            self.assertIsNone(ret)

    def test_storwize_svc_fc_multi_host_maps(self):
        # We can't test connecting to multiple hosts from a single host when
        # using real storage
        if not self.USESIM:
            return

        # Create a volume to be used in mappings
        ctxt = context.get_admin_context()
        volume = self._generate_vol_info(None, None)
        self.fc_driver.create_volume(volume)

        # Create volume types for protocols
        types = {}
        for protocol in ['FC']:
            opts = {'storage_protocol': '<in> ' + protocol}
            types[protocol] = volume_types.create(ctxt, protocol, opts)

        # Create a connector for the second 'host'
        wwpns = [str(random.randint(0, 9999999999999999)).zfill(16),
                 str(random.randint(0, 9999999999999999)).zfill(16)]
        initiator = 'test.initiator.%s' % str(random.randint(10000, 99999))
        conn2 = {'ip': '1.234.56.79',
                 'host': 'storwize-svc-test2',
                 'wwpns': wwpns,
                 'initiator': initiator}

        # Check protocols for FC

        volume['volume_type_id'] = types[protocol]['id']

        # Make sure that the volume has been created
        self._assert_vol_exists(volume['name'], True)

        self.fc_driver.initialize_connection(volume, self._connector)

        self._set_flag('storwize_svc_multihostmap_enabled', False)
        self.assertRaises(
            exception.CinderException,
            self.fc_driver.initialize_connection, volume, conn2)

        self._set_flag('storwize_svc_multihostmap_enabled', True)
        self.fc_driver.initialize_connection(volume, conn2)

        self.fc_driver.terminate_connection(volume, conn2)
        self.fc_driver.terminate_connection(volume, self._connector)

    def test_add_vdisk_copy_fc(self):
        # Ensure only FC is available
        self.fc_driver._state['enabled_protocols'] = set(['FC'])
        volume = self._generate_vol_info(None, None)
        self.fc_driver.create_volume(volume)
        self.fc_driver.add_vdisk_copy(volume['name'], 'fake-pool', None)


@ddt.ddt
class StorwizeSVCCommonDriverTestCase(test.TestCase):
    @mock.patch.object(time, 'sleep')
    def setUp(self, mock_sleep):
        super(StorwizeSVCCommonDriverTestCase, self).setUp()
        self.USESIM = True
        if self.USESIM:
            self._def_flags = {'san_ip': 'hostname',
                               'storwize_san_secondary_ip': 'secondaryname',
                               'san_login': 'user',
                               'san_password': 'pass',
                               'storwize_svc_volpool_name':
                               SVC_POOLS,
                               'storwize_svc_flashcopy_timeout': 20,
                               'storwize_svc_flashcopy_rate': 49,
                               'storwize_svc_clean_rate': 50,
                               'storwize_svc_allow_tenant_qos': True}
            config = conf.Configuration(storwize_svc_common.storwize_svc_opts,
                                        conf.SHARED_CONF_GROUP)
            # Override any configs that may get set in __init__
            self._reset_flags(config)
            self.driver = StorwizeSVCISCSIFakeDriver(
                configuration=config)
            self._driver = storwize_svc_iscsi.StorwizeSVCISCSIDriver(
                configuration=config)
            self.fcdriver = StorwizeSVCFcFakeDriver(
                configuration=config)
            wwpns = [
                str(random.randint(0, 9999999999999999)).zfill(16),
                str(random.randint(0, 9999999999999999)).zfill(16)]
            initiator = 'test.initiator.%s' % str(
                random.randint(10000, 99999))
            self._connector = {'ip': '1.234.56.78',
                               'host': 'storwize-svc-test',
                               'wwpns': wwpns,
                               'initiator': initiator}
            self.sim = StorwizeSVCManagementSimulator(SVC_POOLS)

            self.driver.set_fake_storage(self.sim)
            self.fcdriver.set_fake_storage(self.sim)
            self.ctxt = context.get_admin_context()
        else:
            self._reset_flags()
        self.ctxt = context.get_admin_context()
        self.db = cinder.db
        self.driver.db = self.db
        self.driver.do_setup(None)
        self.driver.check_for_setup_error()
        self.driver._helpers.check_fcmapping_interval = 0
        self.mock_object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                         'DEFAULT_GR_SLEEP', 0)
        self._create_test_volume_types()

    def _set_flag(self, flag, value, configuration=None):
        if not configuration:
            configuration = self.driver.configuration
        group = configuration.config_group
        self.override_config(flag, value, group)

    def _reset_flags(self, configuration=None):
        if not configuration:
            configuration = self.driver.configuration
        CONF.reset()
        for k, v in self._def_flags.items():
            self._set_flag(k, v, configuration)

    def _assert_vol_exists(self, name, exists):
        is_vol_defined = self.driver._helpers.is_vdisk_defined(name)
        self.assertEqual(exists, is_vol_defined)

    def _create_test_volume_types(self):
        spec = {'mirror_pool': 'openstack1'}
        self.mirror_vol_type = self._create_volume_type(spec, 'mirror_type')
        self.default_vol_type = self._create_volume_type(None, 'default_type')

    def test_storwize_svc_connectivity(self):
        # Make sure we detect if the pool doesn't exist
        no_exist_pool = 'i-dont-exist-%s' % random.randint(10000, 99999)
        self._set_flag('storwize_svc_volpool_name', no_exist_pool)
        self.assertRaises(exception.InvalidInput,
                          self.driver.do_setup, None)
        self._reset_flags()

        # Check the case where the user didn't configure IP addresses
        # as well as receiving unexpected results from the storage
        if self.USESIM:
            self.sim.error_injection('lsnodecanister', 'header_mismatch')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.do_setup, None)
            self.sim.error_injection('lsnodecanister', 'remove_field')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.do_setup, None)
            self.sim.error_injection('lsportip', 'header_mismatch')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.do_setup, None)
            self.sim.error_injection('lsportip', 'remove_field')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.do_setup, None)
            self.sim.error_injection('lsfcportsetmember', 'invalid_input')
            self.driver.do_setup(None)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (8, 5, 0, 0),
                                'topology': 'standard',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info

            if self.USESIM:
                self.sim.error_injection('lsip', 'invalid_portset')
                self.driver.do_setup(None)
                self.sim.error_injection('lsip', 'header_mismatch')
                self.assertRaises(exception.VolumeBackendAPIException,
                                  self.driver.do_setup, None)
                self.sim.error_injection('lsip', 'remove_field')
                self.assertRaises(exception.VolumeBackendAPIException,
                                  self.driver.do_setup, None)

        # Check with bad parameters
        self._set_flag('san_ip', '')
        self.assertRaises(exception.InvalidInput,
                          self.driver.check_for_setup_error)
        self._reset_flags()

        self._set_flag('san_password', None)
        self._set_flag('san_private_key', None)
        self.assertRaises(exception.InvalidInput,
                          self.driver.check_for_setup_error)
        self._reset_flags()

        self._set_flag('storwize_svc_vol_grainsize', 42)
        self.assertRaises(exception.InvalidInput,
                          self.driver.check_for_setup_error)
        self._reset_flags()

        self._set_flag('storwize_svc_vol_compression', True)
        self._set_flag('storwize_svc_vol_rsize', -1)
        self.assertRaises(exception.InvalidInput,
                          self.driver.check_for_setup_error)
        self._reset_flags()

        self._set_flag('storwize_svc_vol_rsize', 2)
        self._set_flag('storwize_svc_vol_nofmtdisk', True)
        self.assertRaises(exception.InvalidInput,
                          self.driver.check_for_setup_error)
        self._reset_flags()

        self._set_flag('storwize_svc_vol_iogrp', 5)
        self.assertRaises(exception.InvalidInput,
                          self.driver.check_for_setup_error)
        self._reset_flags()

        if self.USESIM:
            self.sim.error_injection('lslicense', 'no_compression')
            self.sim.error_injection('lsguicapabilities', 'no_compression')
            self._set_flag('storwize_svc_vol_compression', True)
            self.driver.do_setup(None)
            self.assertRaises(exception.InvalidInput,
                              self.driver.check_for_setup_error)
            self._reset_flags()

        # Finally, check with good parameters
        self.driver.do_setup(None)

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'lsip')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_node_info')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_system_info')
    def test_storwize_add_iscsi_ip_address(self, get_system_info,
                                           get_node_info, lsip):
        helper = self.driver._master_backend_helpers
        helper.state = {'storage_nodes': {}, 'enabled_protocols': set(),
                        'compression_enabled': False, 'available_iogrps': [],
                        'system_name': None, 'system_id': None,
                        'code_level': None}
        get_system_info.return_value = {'code_level': (8, 5, 0, 0),
                                        'topology': 'standard',
                                        'system_name': 'storwize-svc-sim',
                                        'system_id': '0123456789ABCDEF'}

        get_node_info.return_value = {'1': {'id': '1', 'name': 'node1',
                                            'IO_group': 0,
                                            'iscsi_name': 'test_iscsi1',
                                            'site_id': '1',
                                            'site_name': 'site1',
                                            'ipv4': [],
                                            'ipv6': [],
                                            'IP_address': [],
                                            'WWPN': [],
                                            'enabled_protocols': [],
                                            'status': 'online'},
                                      '2': {'id': '2', 'name': 'node2',
                                            'IO_group': 1,
                                            'iscsi_name': 'test_iscsi2',
                                            'site_id': '1',
                                            'site_name': 'site1',
                                            'ipv4': [],
                                            'ipv6': [],
                                            'IP_address': [],
                                            'WWPN': [],
                                            'enabled_protocols': [],
                                            'status': 'online'}}

        lsip.return_value = [{'node_id': '1', 'IP_address': '1.1.1.1'},
                             {'node_id': '2', 'IP_address': '2.2.2.2'}]

        # Initially the storage_nodes will be empty
        self.assertEqual(helper.state["storage_nodes"], {})

        # _update_storwize_state will update the code_level
        # and node info. After that it will call add_iscsi_ip_addrs to
        # update the IP_address for the corresponding node_id in storage_nodes
        self.driver._update_storwize_state(helper.state, helper)

        # Now, IPs of both the node_id in storage_nodes is updated correctly
        # Which means add_iscsi_ip_addrs was successful.
        self.assertNotEqual(helper.state["storage_nodes"], {})
        self.assertEqual(helper.state["storage_nodes"]['1']['IP_address'],
                         ['1.1.1.1'])
        self.assertEqual(helper.state["storage_nodes"]['2']['IP_address'],
                         ['2.2.2.2'])

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'mkhost')
    def test_storwize_create_host_with_portset(self, mkhost):
        self.driver.do_setup(self.ctxt)
        connector = {'host': 'storwize-svc-host',
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa',
                     'ip': '127.0.0.1'}
        # Using portset other than default portset0
        portset = "portset1"
        self.driver._helpers.create_host(connector, iscsi=True,
                                         portset=portset)
        host_name = self.driver._helpers.get_host_from_connector(
            connector, iscsi=True)
        self.assertIsNotNone(host_name)

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'mkhost')
    def test_storwize_create_host_with_portset_from_config(self, mkhost):
        self.driver.do_setup(self.ctxt)
        connector = {'host': 'storwize-svc-host',
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa',
                     'ip': '127.0.0.1'}
        # Using portset other than default portset0
        self._set_flag('storwize_portset', "portset1")
        self.driver._helpers.create_host(
            connector, iscsi=True,
            portset=self.driver.configuration.storwize_portset)
        host_name = self.driver._helpers.get_host_from_connector(
            connector, iscsi=True)
        self.assertIsNotNone(host_name)

    @mock.patch.object(ssh_utils, 'SSHPool')
    @mock.patch.object(processutils, 'ssh_execute')
    def test_run_ssh_set_up_with_san_ip(self, mock_ssh_execute, mock_ssh_pool):
        ssh_cmd = ['svcinfo']
        self._driver._run_ssh(ssh_cmd)

        mock_ssh_pool.assert_called_once_with(
            self._driver.configuration.san_ip,
            self._driver.configuration.san_ssh_port,
            self._driver.configuration.ssh_conn_timeout,
            self._driver.configuration.san_login,
            password=self._driver.configuration.san_password,
            privatekey=self._driver.configuration.san_private_key,
            min_size=self._driver.configuration.ssh_min_pool_conn,
            max_size=self._driver.configuration.ssh_max_pool_conn)

    @mock.patch.object(ssh_utils, 'SSHPool')
    @mock.patch.object(processutils, 'ssh_execute')
    def test_run_ssh_set_up_with_secondary_ip(self, mock_ssh_execute,
                                              mock_ssh_pool):
        mock_ssh_pool.side_effect = [paramiko.SSHException, mock.MagicMock()]
        ssh_cmd = ['svcinfo']
        self._driver._run_ssh(ssh_cmd)

        mock_ssh_pool.assert_called_with(
            self._driver.configuration.storwize_san_secondary_ip,
            self._driver.configuration.san_ssh_port,
            self._driver.configuration.ssh_conn_timeout,
            self._driver.configuration.san_login,
            password=self._driver.configuration.san_password,
            privatekey=self._driver.configuration.san_private_key,
            min_size=self._driver.configuration.ssh_min_pool_conn,
            max_size=self._driver.configuration.ssh_max_pool_conn)

    @mock.patch.object(random, 'randint', mock.Mock(return_value=0))
    @mock.patch.object(ssh_utils, 'SSHPool')
    @mock.patch.object(processutils, 'ssh_execute')
    def test_run_ssh_fail_to_secondary_ip(self, mock_ssh_execute,
                                          mock_ssh_pool):
        mock_ssh_execute.side_effect = [processutils.ProcessExecutionError,
                                        mock.MagicMock()]
        ssh_cmd = ['svcinfo']
        self._driver._run_ssh(ssh_cmd)

        mock_ssh_pool.assert_called_with(
            self._driver.configuration.storwize_san_secondary_ip,
            self._driver.configuration.san_ssh_port,
            self._driver.configuration.ssh_conn_timeout,
            self._driver.configuration.san_login,
            password=self._driver.configuration.san_password,
            privatekey=self._driver.configuration.san_private_key,
            min_size=self._driver.configuration.ssh_min_pool_conn,
            max_size=self._driver.configuration.ssh_max_pool_conn)

    @mock.patch.object(ssh_utils, 'SSHPool')
    @mock.patch.object(processutils, 'ssh_execute')
    def test_run_secondary_ip_ssh_fail_to_san_ip(self, mock_ssh_execute,
                                                 mock_ssh_pool):
        mock_ssh_pool.side_effect = [
            paramiko.SSHException,
            mock.MagicMock(
                ip=self._driver.configuration.storwize_san_secondary_ip),
            mock.MagicMock()]
        mock_ssh_execute.side_effect = [processutils.ProcessExecutionError,
                                        mock.MagicMock()]
        ssh_cmd = ['svcinfo']
        self._driver._run_ssh(ssh_cmd)

        mock_ssh_pool.assert_called_with(
            self._driver.configuration.san_ip,
            self._driver.configuration.san_ssh_port,
            self._driver.configuration.ssh_conn_timeout,
            self._driver.configuration.san_login,
            password=self._driver.configuration.san_password,
            privatekey=self._driver.configuration.san_private_key,
            min_size=self._driver.configuration.ssh_min_pool_conn,
            max_size=self._driver.configuration.ssh_max_pool_conn)

    @mock.patch.object(ssh_utils, 'SSHPool')
    @mock.patch.object(processutils, 'ssh_execute')
    def test_run_ssh_both_ip_set_failure(self, mock_ssh_execute,
                                         mock_ssh_pool):
        mock_ssh_pool.side_effect = [
            paramiko.SSHException,
            mock.MagicMock(),
            mock.MagicMock()]
        mock_ssh_execute.side_effect = [processutils.ProcessExecutionError,
                                        processutils.ProcessExecutionError]
        ssh_cmd = ['svcinfo']
        self.assertRaises(processutils.ProcessExecutionError,
                          self._driver._run_ssh, ssh_cmd)

    @mock.patch.object(ssh_utils, 'SSHPool')
    @mock.patch.object(processutils, 'ssh_execute')
    def test_run_ssh_second_ip_not_set_failure(self, mock_ssh_execute,
                                               mock_ssh_pool):
        mock_ssh_execute.side_effect = [processutils.ProcessExecutionError,
                                        mock.MagicMock()]
        self._set_flag('storwize_san_secondary_ip', None)
        ssh_cmd = ['svcinfo']
        self.assertRaises(processutils.ProcessExecutionError,
                          self._driver._run_ssh, ssh_cmd)

    @mock.patch.object(random, 'randint', mock.Mock(return_value=0))
    @mock.patch.object(ssh_utils, 'SSHPool')
    @mock.patch.object(processutils, 'ssh_execute')
    def test_run_ssh_consistent_active_ip(self, mock_ssh_execute,
                                          mock_ssh_pool):
        ssh_cmd = ['svcinfo']
        self._driver._run_ssh(ssh_cmd)
        self._driver._run_ssh(ssh_cmd)
        self._driver._run_ssh(ssh_cmd)
        self.assertEqual(self._driver.configuration.san_ip,
                         self._driver.active_ip)
        mock_ssh_execute.side_effect = [paramiko.SSHException,
                                        mock.MagicMock(), mock.MagicMock()]
        self._driver._run_ssh(ssh_cmd)
        self._driver._run_ssh(ssh_cmd)
        self.assertEqual(self._driver.configuration.storwize_san_secondary_ip,
                         self._driver.active_ip)

    @mock.patch.object(ssh_utils, 'SSHPool')
    @mock.patch.object(processutils, 'ssh_execute')
    def test_run_ssh_response_no_ascii(self, mock_ssh_execute, mock_ssh_pool):
        mock_ssh_execute.side_effect = processutils.ProcessExecutionError(
            u'',
            'CMMVC6035E \xe6\x93\x8d\xe4\xbd\x9c\xe5\xa4\xb1\xe8\xb4\xa5\n',
            1,
            u'svctask lsmdiskgrp "openstack"',
            None)
        self.assertRaises(exception.InvalidInput,
                          self._driver._validate_pools_exist)

    @ddt.data({'node1': 'online', 'node2': 'online', 'node3': 'online',
               'node4': 'online', 'state': 'enabled', 'reason': None},
              {'node1': 'online', 'node2': 'online', 'node3': 'offline',
               'node4': 'offline', 'state': 'disabled',
               'reason': 'site2 is down'},
              {'node1': 'offline', 'node2': 'offline', 'node3': 'online',
               'node4': 'online', 'state': 'disabled',
               'reason': 'site1 is down'},
              {'node1': 'offline', 'node2': 'offline', 'node3': 'offline',
               'node4': 'offline', 'state': 'disabled',
               'reason': 'site1 is down'})
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_node_info')
    def test_get_hyperswap_storage_state(self, node_data, get_node_info):
        get_node_info.return_value = {'7': {'id': '7', 'name': 'node1',
                                            'status': node_data['node1'],
                                            'site_id': '1',
                                            'site_name': 'site1'},
                                      '8': {'id': '8', 'name': 'node2',
                                            'status': node_data['node2'],
                                            'site_id': '1',
                                            'site_name': 'site1'},
                                      '9': {'id': '9', 'name': 'node3',
                                            'status': node_data['node3'],
                                            'site_id': '2',
                                            'site_name': 'site2'},
                                      '10': {'id': '10', 'name': 'node4',
                                             'status': node_data['node4'],
                                             'site_id': '2',
                                             'site_name': 'site2'}}

        state, reason = self.driver.get_hyperswap_storage_state()

        self.assertEqual(state, node_data['state'])
        self.assertEqual(reason, node_data['reason'])

    @ddt.data((True, 'online'),
              (True, 'offline'),
              (False, 'online'),
              (False, 'offline'))
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'lsnode')
    @ddt.unpack
    def test_get_node_info(self, online_node, node_status, lsnode):
        empty_nodes_info = {}
        fake_lsnode_info = [{
                            'id': 1,
                            'name': 'test',
                            'IO_group_id': 'test_io_group',
                            'iscsi_name': 'test_iscsi',
                            'WWNN': '123456',
                            'status': node_status,
                            'WWPN': '8999',
                            'ipv4': '192.9.123.1',
                            'ipv6': '1.2.3.4',
                            'enabled_protocols': 'ipv6',
                            'site_id': '1783',
                            'site_name': 'test-sitename'
                            }]
        lsnode.return_value = fake_lsnode_info
        nodes = self.driver._helpers.get_node_info(online_node)
        if not online_node or online_node and node_status == 'online':
            self.assertIsNotNone(nodes)
        elif online_node and node_status == 'offline':
            self.assertEqual(nodes, empty_nodes_info)

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_build_pool_stats')
    def test_update_volume_stats_non_replication(self, _build_pool_stats):
        self.driver._update_volume_stats()
        self.assertFalse(self.driver._replica_enabled)
        self.assertEqual(SVC_POOLS, self.driver._get_backend_pools())
        self.assertEqual(len(SVC_POOLS), _build_pool_stats.call_count)
        self.assertIsNotNone(self.driver._master_backend_helpers.stats)
        self.assertIsNone(self.driver._aux_backend_helpers)

    @ddt.data((False, 'enabled', ''),
              (False, 'disabled', 'site 2 down'),
              (True, '', ''))
    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       'get_hyperswap_storage_state')
    @ddt.unpack
    def test_update_volume_stats(self, is_replica_enabled,
                                 replication_status,
                                 reason, get_hs_storage_state):
        self._replica_enabled = is_replica_enabled
        self.driver._update_volume_stats()
        if not self._replica_enabled:
            with mock.patch.object(
                    storwize_svc_common.StorwizeHelpers,
                    'is_system_topology_hyperswap') as is_hyperswap:
                with mock.patch.object(
                        storwize_svc_common.StorwizeHelpers,
                        'get_node_info') as get_node_info:
                    is_hyperswap.return_value = is_hyperswap
                    if is_hyperswap:
                        get_node_info.return_value = None
                        get_hs_storage_state.side_effect =\
                            exception.VolumeBackendAPIException(data='')
                        self.assertRaises(exception.VolumeBackendAPIException,
                                          get_hs_storage_state)
                        get_hs_storage_state.return_value = (
                            replication_status, reason)
                        if replication_status != 'enabled':
                            self.assertNotEqual(
                                fields.ReplicationStatus.ENABLED,
                                replication_status)
                            self.assertIsNotNone(reason)
                        else:
                            self.assertEqual(fields.ReplicationStatus.ENABLED,
                                             replication_status)
                            self.assertEqual(reason, '')
        else:
            self.assertFalse(get_hs_storage_state.called)

    def _get_pool_volumes(self, pool):
        vdisks = self.sim._cmd_lsvdisks_from_filter('mdisk_grp_name', pool)
        return vdisks

    def test_get_all_volumes(self):
        _volumes_list = []
        pools = _get_test_pool(get_all=True)
        for pool in pools:
            host = 'openstack@svc#%s' % pool
            vol1 = testutils.create_volume(
                self.ctxt, host=host,
                volume_type_id=self.vt['id'])
            self.driver.create_volume(vol1)
            vol2 = testutils.create_volume(
                self.ctxt, host=host,
                volume_type_id=self.vt['id'])
            self.driver.create_volume(vol2)
        for pool in pools:
            pool_vols = self._get_pool_volumes(pool)
            for pool_vol in pool_vols:
                _volumes_list.append(pool_vol)
        for vol in _volumes_list:
            self.assertIn(vol, self.sim._volumes_list)

    def _create_volume_type(self, opts, type_name):
        type_ref = volume_types.create(self.ctxt, type_name, opts)
        vol_type = objects.VolumeType.get_by_id(self.ctxt, type_ref['id'])
        return vol_type

    def _create_hyperswap_type(self, type_name):
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'hyperswap2'}
        hyper_type = self._create_volume_type(spec, type_name)
        return hyper_type

    def _create_hyperswap_volume(self, hyper_type, **kwargs):
        pool = 'hyperswap1'
        prop = {'host': 'openstack@svc#%s' % pool,
                'size': 1}
        prop['volume_type_id'] = hyper_type.id
        for p in prop.keys():
            if p not in kwargs:
                kwargs[p] = prop[p]
        vol = testutils.create_volume(self.ctxt, **kwargs)
        self.driver.create_volume(vol)
        return vol

    def _generate_hyperswap_vol_info(self, hyper_type, size=10):
        pool = 'hyperswap1'
        prop = {'host': 'openstack@svc#%s' % pool,
                'size': size,
                'volume_type_id': hyper_type.id}
        vol = testutils.create_volume(self.ctxt, **prop)
        return vol

    def _generate_vol_info(self, vol_type=None, size=10):
        pool = _get_test_pool()
        prop = {'size': size,
                'host': 'openstack@svc#%s' % pool,
                'volume_type_id': self.vt['id']}
        if vol_type:
            prop['volume_type_id'] = vol_type.id
        vol = testutils.create_volume(self.ctxt, **prop)
        return vol

    def _generate_vol_info_on_dr_pool(self, vol_type=None, size=10):
        pool = 'dr_pool1'
        prop = {'size': size,
                'host': 'openstack@svc#%s' % pool}
        if vol_type:
            prop['volume_type_id'] = vol_type.id
        vol = testutils.create_volume(self.ctxt, **prop)
        return vol

    def _generate_snap_info(self, vol_id, size=10):
        prop = {'volume_id': vol_id,
                'volume_size': size}
        snap = testutils.create_snapshot(self.ctxt, **prop)
        return snap

    def _create_volume(self, **kwargs):
        pool = _get_test_pool()
        prop = {'host': 'openstack@svc#%s' % pool,
                'size': 1,
                'volume_type_id': self.vt['id']}
        for p in prop.keys():
            if p not in kwargs:
                kwargs[p] = prop[p]
        vol = testutils.create_volume(self.ctxt, **kwargs)
        self.driver.create_volume(vol)
        return vol

    def _delete_volume(self, volume):
        self.driver.delete_volume(volume)
        self.db.volume_destroy(self.ctxt, volume['id'])

    def _create_group_in_db(self, **kwargs):
        cg = testutils.create_group(self.ctxt, **kwargs)
        return cg

    def _create_group(self, **kwargs):
        grp = self._create_group_in_db(**kwargs)

        model_update = self.driver.create_group(self.ctxt, grp)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'],
                         "CG created failed")
        return grp

    def _create_volumegroup_type_and_volumegroup(self, vol_type_ref,
                                                 is_pool=None,
                                                 is_io_grp=None):
        # Create volumegroup type
        volumegroup_spec = {'volume_group_enabled': '<is> True'}
        if is_pool:
            volumegroup_spec.update({'volume_group_pool': is_pool})
        if is_io_grp:
            volumegroup_spec.update({'volume_group_iogrp': is_io_grp})

        volumegroup_type_ref = group_types.create(self.ctxt,
                                                  'volumegroup_type',
                                                  volumegroup_spec)
        volumegroup_type = objects.GroupType.get_by_id(
            self.ctxt, volumegroup_type_ref['id'])

        # Create volumegroup
        volumegroup = testutils.create_group(
            self.ctxt, group_type_id=volumegroup_type.id,
            volume_type_ids=[vol_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt, volumegroup)
        return (volumegroup_type_ref, volumegroup_type,
                volumegroup, model_update)

    def _create_group_snapshot_in_db(self, group_id, **kwargs):
        group_snapshot = testutils.create_group_snapshot(self.ctxt,
                                                         group_id=group_id,
                                                         **kwargs)
        snapshots = []
        volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), group_id)

        if not volumes:
            msg = _("Group is empty. No cgsnapshot will be created.")
            raise exception.InvalidGroup(reason=msg)

        for volume in volumes:
            snapshots.append(testutils.create_snapshot(
                self.ctxt, volume['id'],
                group_snapshot.id,
                group_snapshot.name,
                group_snapshot.id,
                fields.SnapshotStatus.CREATING))
        return group_snapshot, snapshots

    def _create_group_snapshot(self, cg_id, **kwargs):
        group_snapshot, snapshots = self._create_group_snapshot_in_db(
            cg_id, **kwargs)

        model_update, snapshots_model = (
            self.driver.create_group_snapshot(self.ctxt, group_snapshot,
                                              snapshots))
        self.assertEqual(fields.GroupSnapshotStatus.AVAILABLE,
                         model_update['status'],
                         "CGSnapshot created failed")

        for snapshot in snapshots_model:
            self.assertEqual(fields.SnapshotStatus.AVAILABLE,
                             snapshot['status'])
        return group_snapshot, snapshots

    def _create_test_vol(self, opts):
        ctxt = testutils.get_test_admin_context()
        type_ref = volume_types.create(ctxt, 'testtype', opts)
        volume = self._generate_vol_info()
        volume.volume_type_id = type_ref['id']
        volume.volume_typ = objects.VolumeType.get_by_id(ctxt,
                                                         type_ref['id'])
        self.driver.create_volume(volume)

        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        self.driver.delete_volume(volume)
        volume_types.destroy(ctxt, type_ref['id'])
        return attrs

    def _get_default_opts(self):
        opt = {'rsize': 2,
               'warning': 0,
               'autoexpand': True,
               'grainsize': 256,
               'compression': False,
               'easytier': True,
               'iogrp': '0',
               'qos': None,
               'replication': False,
               'stretched_cluster': None,
               'nofmtdisk': False,
               'flashcopy_rate': 49,
               'clean_rate': 50,
               'mirror_pool': None,
               'aux_mirror_pool': None,
               'volume_topology': None,
               'peer_pool': None,
               'storwize_portset': None,
               'storwize_svc_src_child_pool': None,
               'storwize_svc_target_child_pool': None,
               'cycle_period_seconds': 300
               }
        return opt

    @ddt.data(('5000', 'iops', True),
              ('500', 'iops_per_gb', False),
              ('3000', 'mbps', False))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'add_vdisk_qos')
    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_get_vdisk_params')
    @ddt.unpack
    def test_storwize_svc_create_volume_with_qos(self,
                                                 fake_iothrottling_value,
                                                 fake_iothrottling_unit,
                                                 empty_qos,
                                                 get_vdisk_params,
                                                 add_vdisk_qos):
        fake_opts = self._get_default_opts()
        # If the qos is empty, chvdisk should not be called
        # for create_volume.
        get_vdisk_params.return_value = fake_opts
        vol = self._create_volume()
        if empty_qos:
            self._assert_vol_exists(vol['name'], True)
            self.assertFalse(add_vdisk_qos.called)
        self.driver.delete_volume(vol)

        # If the qos is not empty, chvdisk should be called
        # for create_volume.
        fake_opts['qos'] = {'IOThrottling': fake_iothrottling_value,
                            'IOThrottling_unit': fake_iothrottling_unit}
        get_vdisk_params.return_value = fake_opts
        self.driver.create_volume(vol)
        self._assert_vol_exists(vol['name'], True)
        add_vdisk_qos.assert_called_once_with(vol['name'], fake_opts['qos'],
                                              vol['size'])

        self.driver.delete_volume(vol)
        self._assert_vol_exists(vol['name'], False)

    def test_storwize_svc_snapshots(self):
        vol1 = self._create_volume()
        snap1 = self._generate_snap_info(vol1.id)

        # Test timeout and volume cleanup
        self._set_flag('storwize_svc_flashcopy_timeout', 1)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_snapshot, snap1)
        self._assert_vol_exists(snap1['name'], False)
        self._reset_flags()

        # Test flashcopy_rate > 100 on 7.2.0.0
        self._set_flag('storwize_svc_flashcopy_rate', 149)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_snapshot, snap1)
        self._assert_vol_exists(snap1['name'], False)
        self._reset_flags()

        # Test clean_rate < 150 on 7.2.0.0
        self._set_flag('storwize_svc_clean_rate', 100)
        vol2 = self._create_volume()
        snap2 = self._generate_snap_info(vol2.id)
        self.driver.create_snapshot(snap2)
        self._assert_vol_exists(snap2['name'], True)
        self._reset_flags()

        # Test flashcopy_rate out of range
        spec = {'flashcopy_rate': 151}
        type_ref = volume_types.create(self.ctxt, "fccopy_rate", spec)
        vol3 = self._generate_vol_info(type_ref)
        self.driver.create_volume(vol3)
        snap3 = self._generate_snap_info(vol3.id)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_snapshot, snap3)
        self._assert_vol_exists(snap3['name'], False)

        # Test prestartfcmap failing
        with mock.patch.object(
                storwize_svc_common.StorwizeSSH, 'prestartfcmap') as prestart:
            prestart.side_effect = exception.VolumeBackendAPIException(data='')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_snapshot, snap1)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
            self.sim.error_injection('startfcmap', 'bad_id')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_snapshot, snap1)
            self._assert_vol_exists(snap1['name'], False)
            self.sim.error_injection('prestartfcmap', 'bad_id')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_snapshot, snap1)
            self._assert_vol_exists(snap1['name'], False)

        # Test successful snapshot
        self.driver.create_snapshot(snap1)
        self._assert_vol_exists(snap1['name'], True)

        # Try to create a snapshot from an non-existing volume - should fail
        vol2 = self._generate_vol_info()
        snap_novol = self._generate_snap_info(vol2.id)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_snapshot,
                          snap_novol)

        # We support deleting a volume that has snapshots, so delete the volume
        # first
        self.driver.delete_volume(vol1)
        self.driver.delete_snapshot(snap1)

    def test_storwize_svc_create_cloned_volume(self):
        vol1 = self._create_volume()
        vol2 = testutils.create_volume(
            self.ctxt,
            volume_type_id=self.vt['id'])
        vol3 = testutils.create_volume(
            self.ctxt,
            volume_type_id=self.vt['id'])
        vol4 = testutils.create_volume(
            self.ctxt,
            volume_type_id=self.vt['id'])

        # Try to clone where source size = target size
        vol1['size'] = vol2['size']
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_cloned_volume(vol2, vol1)
        if self.USESIM:
            # validate copyrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['target'] == vol2['name']:
                    self.assertEqual('49', fcmap['copyrate'])
        self._assert_vol_exists(vol2['name'], True)

        # Try to clone where  source size < target size
        vol3['size'] = vol1['size'] + 1
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_cloned_volume(vol3, vol1)
        if self.USESIM:
            # Validate copyrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['target'] == vol3['name']:
                    self.assertEqual('49', fcmap['copyrate'])
        self._assert_vol_exists(vol3['name'], True)

        # Try to clone and check if clean_rate is set to default
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_cloned_volume(vol4, vol1)
        if self.USESIM:
            # Validate copyrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['target'] == vol4['name']:
                    self.assertEqual('50', fcmap['cleanrate'])
        self._assert_vol_exists(vol4['name'], True)

        # Delete in the 'opposite' order to make sure it works
        self.driver.delete_volume(vol4)
        self._assert_vol_exists(vol4['name'], False)
        self.driver.delete_volume(vol3)
        self._assert_vol_exists(vol3['name'], False)
        self.driver.delete_volume(vol2)
        self._assert_vol_exists(vol2['name'], False)
        self.driver.delete_volume(vol1)
        self._assert_vol_exists(vol1['name'], False)

        # retype the flashcopy_rate
        ctxt = context.get_admin_context()
        key_specs_old = {'flashcopy_rate': 49}
        key_specs_new = {'flashcopy_rate': 149}
        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)
        host = {'host': 'openstack@svc#openstack'}
        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])
        volume = self._generate_vol_info(old_type)
        volume['host'] = host['host']
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        volume2 = testutils.create_volume(
            self.ctxt,
            volume_type_id=self.vt['id'])
        self.driver.create_cloned_volume(volume2, volume)
        if self.USESIM:
            # Validate copyrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['target'] == volume2['name']:
                    self.assertEqual('49', fcmap['copyrate'])
        self.driver.retype(ctxt, volume, new_type, diff, host)
        if self.USESIM:
            # Validate copyrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['source'] == volume['name']:
                    self.assertEqual('149', fcmap['copyrate'])

        # create cloned volume with new type diffrent iogrp
        key_specs_old = {'iogrp': '0'}
        key_specs_new = {'iogrp': '1'}
        old_type_ref = volume_types.create(ctxt, 'oldio', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'newio', key_specs_new)
        old_io_type = objects.VolumeType.get_by_id(ctxt,
                                                   old_type_ref['id'])
        new_io_type = objects.VolumeType.get_by_id(ctxt,
                                                   new_type_ref['id'])

        volume3 = self._generate_vol_info(old_io_type)
        self.driver.create_volume(volume3)
        volume4 = self._generate_vol_info(new_io_type)
        self.driver.create_cloned_volume(volume4, volume)
        attributes = self.driver._helpers.get_vdisk_attributes(volume4['name'])
        self.assertEqual('1', attributes['IO_group_id'])

    def test_storwize_svc_retype_only_change_clean_rate(self):
        self.driver.do_setup(None)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#openstack', 'capabilities': cap}
        ctxt = context.get_admin_context()

        key_specs_old = {'clean_rate': 50}
        key_specs_new = {'clean_rate': 100}
        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)
        host = {'host': 'openstack@svc#openstack'}
        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])
        volume = self._generate_vol_info(old_type)
        volume['host'] = host['host']
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        volume2 = testutils.create_volume(
            self.ctxt,
            volume_type_id=self.vt['id'])

        # Create the snapshot of the source volume
        snap = self._generate_snap_info(volume.id)
        self.driver.create_snapshot(snap)
        if self.USESIM:
            # Validate cleanrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['source'] == volume['name']:
                    self.assertEqual('50', fcmap['cleanrate'])

        # Try to retype the source volume
        self.driver.retype(ctxt, volume, new_type, diff, host)
        if self.USESIM:
            # Validate cleanrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['source'] == volume['name']:
                    self.assertEqual('100', fcmap['cleanrate'])

        # Delete the volumes
        self.driver.delete_volume(volume2)
        self._assert_vol_exists(volume2['name'], False)
        self.driver.delete_volume(volume)
        self._assert_vol_exists(volume['name'], False)

    def test_storwize_svc_create_volume_from_snapshot(self):
        vol1 = self._create_volume()
        snap1 = self._generate_snap_info(vol1.id)
        self.driver.create_snapshot(snap1)
        vol2 = self._generate_vol_info()
        vol3 = self._generate_vol_info()

        # Try to create a volume from a non-existing snapshot
        vol_novol = self._generate_vol_info()
        snap_novol = self._generate_snap_info(vol_novol.id)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_volume_from_snapshot,
                          vol_novol,
                          snap_novol)

        # Fail the snapshot
        with mock.patch.object(
                storwize_svc_common.StorwizeSSH, 'prestartfcmap') as prestart:
            prestart.side_effect = exception.VolumeBackendAPIException(
                data='')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_volume_from_snapshot,
                              vol2, snap1)
            self._assert_vol_exists(vol2['name'], False)

        # Try to create where volume size > snapshot size
        vol2['size'] += 1
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_volume_from_snapshot(vol2, snap1)
        self._assert_vol_exists(vol2['name'], True)
        vol2['size'] -= 1

        # Try to create where volume size = snapshot size
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_volume_from_snapshot(vol3, snap1)
        self._assert_vol_exists(vol3['name'], True)

        # Delete in the 'opposite' order to make sure it works
        self.driver.delete_volume(vol3)
        self._assert_vol_exists(vol3['name'], False)
        self.driver.delete_volume(vol2)
        self._assert_vol_exists(vol2['name'], False)
        self.driver.delete_snapshot(snap1)
        self._assert_vol_exists(snap1['name'], False)
        self.driver.delete_volume(vol1)
        self._assert_vol_exists(vol1['name'], False)

    @ddt.data(('5000', 'iops', True),
              ('500', 'iops_per_gb', False),
              ('3000', 'mbps', False))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'add_vdisk_qos')
    @ddt.unpack
    def test_storwize_svc_create_volfromsnap_clone_with_qos(
            self, fake_iothrottling_value, fake_iothrottling_unit,
            empty_qos, add_vdisk_qos):
        vol1 = self._create_volume()
        snap1 = self._generate_snap_info(vol1.id)
        self.driver.create_snapshot(snap1)
        vol2 = self._generate_vol_info()
        vol3 = self._generate_vol_info()
        fake_opts = self._get_default_opts()

        # Succeed
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')

        # If the qos is empty, chvdisk should not be called
        # for create_volume_from_snapshot.
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            if empty_qos:
                get_vdisk_params.return_value = fake_opts
                self.driver.create_volume_from_snapshot(vol2, snap1)
                self._assert_vol_exists(vol2['name'], True)
                self.assertFalse(add_vdisk_qos.called)
                self.driver.delete_volume(vol2)

            # If the qos is not empty, chvdisk should be called
            # for create_volume_from_snapshot.
            fake_opts['qos'] = {'IOThrottling': fake_iothrottling_value,
                                'IOThrottling_unit': fake_iothrottling_unit}
            get_vdisk_params.return_value = fake_opts
            self.driver.create_volume_from_snapshot(vol2, snap1)
            self._assert_vol_exists(vol2['name'], True)
            add_vdisk_qos.assert_called_once_with(vol2['name'],
                                                  fake_opts['qos'],
                                                  vol2['size'])

            if self.USESIM:
                self.sim.error_injection('lsfcmap', 'speed_up')

            # If the qos is empty, chvdisk should not be called
            # for create_cloned_volume.
            add_vdisk_qos.reset_mock()
            if empty_qos:
                fake_opts['qos'] = None
                get_vdisk_params.return_value = fake_opts
                self.driver.create_cloned_volume(vol3, vol2)
                self._assert_vol_exists(vol3['name'], True)
                self.assertFalse(add_vdisk_qos.called)
                self.driver.delete_volume(vol3)

            # If the qos is not empty, chvdisk should be called
            # for create_cloned_volume.
            fake_opts['qos'] = {'IOThrottling': fake_iothrottling_value,
                                'IOThrottling_unit': fake_iothrottling_unit}
            get_vdisk_params.return_value = fake_opts
            self.driver.create_cloned_volume(vol3, vol2)
            self._assert_vol_exists(vol3['name'], True)
            add_vdisk_qos.assert_called_once_with(vol3['name'],
                                                  fake_opts['qos'],
                                                  vol3['size'])

        # Delete in the 'opposite' order to make sure it works
        self.driver.delete_volume(vol3)
        self._assert_vol_exists(vol3['name'], False)
        self.driver.delete_volume(vol2)
        self._assert_vol_exists(vol2['name'], False)
        self.driver.delete_snapshot(snap1)
        self._assert_vol_exists(snap1['name'], False)
        self.driver.delete_volume(vol1)
        self._assert_vol_exists(vol1['name'], False)

    def test_storwize_svc_delete_vol_with_fcmap(self):
        vol1 = self._create_volume()
        # create two snapshots
        snap1 = self._generate_snap_info(vol1.id)
        snap2 = self._generate_snap_info(vol1.id)
        self.driver.create_snapshot(snap1)
        self.driver.create_snapshot(snap2)
        vol2 = self._generate_vol_info()
        vol3 = self._generate_vol_info()

        # Create vol from the second snapshot
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_volume_from_snapshot(vol2, snap2)
        if self.USESIM:
            # validate copyrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['target'] == vol2['name']:
                    self.assertEqual('copying', fcmap['status'])
        self._assert_vol_exists(vol2['name'], True)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_cloned_volume(vol3, vol2)

        if self.USESIM:
            # validate copyrate was set on the flash copy
            for i, fcmap in self.sim._fcmappings_list.items():
                if fcmap['target'] == vol3['name']:
                    self.assertEqual('copying', fcmap['status'])
        self._assert_vol_exists(vol3['name'], True)

        # Delete in the 'opposite' order to make sure it works
        self.driver.delete_volume(vol3)
        self._assert_vol_exists(vol3['name'], False)
        self.driver.delete_volume(vol2)
        self._assert_vol_exists(vol2['name'], False)
        self.driver.delete_snapshot(snap2)
        self._assert_vol_exists(snap2['name'], False)
        self.driver.delete_snapshot(snap1)
        self._assert_vol_exists(snap1['name'], False)
        self.driver.delete_volume(vol1)
        self._assert_vol_exists(vol1['name'], False)

    def test_storwize_svc_volumes(self):
        # Create a first volume
        volume = self._generate_vol_info()
        self.driver.create_volume(volume)

        self.driver.ensure_export(None, volume)

        # Do nothing
        self.driver.create_export(None, volume, {})
        self.driver.remove_export(None, volume)

        # Make sure volume attributes are as they should be
        attributes = self.driver._helpers.get_vdisk_attributes(volume['name'])
        attr_size = float(attributes['capacity']) / units.Gi  # bytes to GB
        self.assertEqual(attr_size, float(volume['size']))
        pool = _get_test_pool()
        self.assertEqual(attributes['mdisk_grp_name'], pool)

        # Try to create the volume again (should fail)
        self.assertRaises(exception.VolumeBackendAPIException,
                          self.driver.create_volume,
                          volume)

        # Try to delete a volume that doesn't exist (should not fail)
        vol_no_exist = self._generate_vol_info()
        self.driver.delete_volume(vol_no_exist)
        # Ensure export for volume that doesn't exist (should not fail)
        self.driver.ensure_export(None, vol_no_exist)

        # Delete the volume
        self.driver.delete_volume(volume)

    def test_storwize_svc_volume_name(self):
        volume = self._generate_vol_info()
        self.driver.create_volume(volume)
        self.driver.ensure_export(None, volume)

        # Ensure lsvdisk can find the volume by name
        attributes = self.driver._helpers.get_vdisk_attributes(volume['name'])
        self.assertIn('name', attributes)
        self.assertEqual(volume['name'], attributes['name'])
        self.driver.delete_volume(volume)

    def test_storwize_svc_volume_params(self):
        # Option test matrix
        # Option        Value   Covered by test #
        # rsize         -1      1
        # rsize         2       2,3
        # warning       0       2
        # warning       80      3
        # autoexpand    True    2
        # autoexpand    False   3
        # grainsize     32      2
        # grainsize     256     3
        # compression   True    4
        # compression   False   2,3
        # easytier      True    1,3
        # easytier      False   2
        # iogrp         0       1
        # iogrp         1       2
        # nofmtdisk     False   1
        # nofmtdisk     True    1

        opts_list = []
        chck_list = []
        opts_list.append({'rsize': -1, 'easytier': True, 'iogrp': '0'})
        chck_list.append({'free_capacity': '0', 'easy_tier': 'on',
                          'IO_group_id': '0'})

        opts_list.append({'rsize': -1, 'nofmtdisk': False})
        chck_list.append({'formatted': 'yes'})

        opts_list.append({'rsize': -1, 'nofmtdisk': True})
        chck_list.append({'formatted': 'no'})

        test_iogrp = '1' if self.USESIM else '0'
        opts_list.append({'rsize': 2, 'compression': False, 'warning': 0,
                          'autoexpand': True, 'grainsize': 32,
                          'easytier': False, 'iogrp': test_iogrp})
        chck_list.append({'-free_capacity': '0', 'compressed_copy': 'no',
                          'warning': '0', 'autoexpand': 'on',
                          'grainsize': '32', 'easy_tier': 'off',
                          'IO_group_id': (test_iogrp)})
        opts_list.append({'rsize': 2, 'compression': False, 'warning': 80,
                          'autoexpand': False, 'grainsize': 256,
                          'easytier': True})
        chck_list.append({'-free_capacity': '0', 'compressed_copy': 'no',
                          'warning': '80', 'autoexpand': 'off',
                          'grainsize': '256', 'easy_tier': 'on'})
        opts_list.append({'rsize': 2, 'compression': True})
        chck_list.append({'-free_capacity': '0',
                          'compressed_copy': 'yes'})

        for idx in range(len(opts_list)):
            attrs = self._create_test_vol(opts_list[idx])
            for k, v in chck_list[idx].items():
                try:
                    if k[0] == '-':
                        k = k[1:]
                        self.assertNotEqual(v, attrs[k])
                    else:
                        self.assertEqual(v, attrs[k])
                except processutils.ProcessExecutionError as e:
                    if 'CMMVC7050E' not in e.stderr:
                        raise

    @ddt.data(('yes'), ('Yes'), ('no'), ('NO'), (''))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_pool_attrs')
    def test_build_pool_stats_drp(self, is_drp, get_pool_attrs):
        get_pool_attrs.return_value = {'id': 1, 'name': 'openstack',
                                       'data_reduction': is_drp,
                                       'easy_tier': 'on',
                                       'capacity': '20',
                                       'free_capacity': '40',
                                       'used_capacity': '0',
                                       'real_capacity': '0',
                                       'virtual_capacity': '0',
                                       'status': 'online',
                                       'site_id': '1',
                                       'site_name': 'site1'}
        pool = 'openstack'
        pool_stats = self.driver._build_pool_stats(pool)
        if is_drp in ['yes', 'Yes']:
            self.assertTrue(pool_stats['data_reduction'])
        else:
            self.assertFalse(pool_stats['data_reduction'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_pool_attrs')
    def test_build_pool_stats_drp_none(self, get_pool_attrs):
        get_pool_attrs.return_value = {'id': 1, 'name': 'openstack1',
                                       'easy_tier': 'on',
                                       'capacity': '20',
                                       'free_capacity': '40',
                                       'used_capacity': '0',
                                       'real_capacity': '0',
                                       'virtual_capacity': '0',
                                       'status': 'online',
                                       'site_id': '1',
                                       'site_name': 'site1'}
        pool = 'openstack1'
        pool_stats = self.driver._build_pool_stats(pool)
        self.assertFalse(pool_stats['data_reduction'])

    @ddt.data(('IOPs_limit', "50"), ('bandwidth_limit_MB', "100"))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'get_pool_volumes')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsthrottle')
    @ddt.unpack
    def test_storwize_svc_max_pool_throttle_rate(self, fake_throttle_name,
                                                 fake_throttle_value,
                                                 io_throttles,
                                                 get_pool_volumes):
        pools = _get_test_pool(get_all=True)
        for pool in pools:
            vol_throttles = []
            expected_throttle_value = 0
            if 'openstack' == pool:
                # Create volumes in the pool 'openstack'
                host = 'openstack@svc#%s' % pool
                vol1 = testutils.create_volume(
                    self.ctxt, host=host,
                    volume_type_id=self.vt['id'])
                self.driver.create_volume(vol1)
                self._assert_vol_exists(vol1['name'], True)

                vol2 = testutils.create_volume(
                    self.ctxt, host=host,
                    volume_type_id=self.vt['id'])
                self.driver.create_volume(vol2)
                self._assert_vol_exists(vol1['name'], True)

                # Set io_throttle values to volumes
                vol_throttles = [
                    {'object_name': vol1.name, 'IOPs_limit': '20',
                     'bandwidth_limit_MB': '40'},
                    {'object_name': vol2.name, 'IOPs_limit': '30',
                     'bandwidth_limit_MB': '60'}]
                expected_throttle_value = int(fake_throttle_value)

            get_pool_volumes.return_value = [vol1, vol2]
            io_throttles.return_value = vol_throttles
            iothrottle_value = (
                self.driver._helpers.get_pool_max_throttle_rate_vdisk(
                    pool, fake_throttle_name))
            # Check the sum of throttle values set to volumes from a pool
            self.assertEqual(expected_throttle_value, iothrottle_value)
            self.assertTrue(get_pool_volumes.called)
            self.assertTrue(io_throttles.called)

    def test_storwize_svc_unicode_host_and_volume_names(self):
        # We'll check with iSCSI only - nothing protocol-dependent here
        self.driver.do_setup(None)

        rand_id = random.randint(10000, 99999)
        volume1 = self._generate_vol_info()
        self.driver.create_volume(volume1)
        self._assert_vol_exists(volume1['name'], True)

        self.assertRaises(exception.VolumeDriverException,
                          self.driver._helpers.create_host,
                          {'host': 12345})

        # Add a host first to make life interesting (this host and
        # conn['host'] should be translated to the same prefix, and the
        # initiator should differentiate
        tmpconn1 = {'initiator': u'unicode:initiator1.%s' % rand_id,
                    'ip': '10.10.10.10',
                    'host': u'unicode.foo}.bar{.baz-%s' % rand_id}
        self.driver._helpers.create_host(tmpconn1, iscsi=True)

        # Add a host with a different prefix
        tmpconn2 = {'initiator': u'unicode:initiator2.%s' % rand_id,
                    'ip': '10.10.10.11',
                    'host': u'unicode.hello.world-%s' % rand_id}
        self.driver._helpers.create_host(tmpconn2, iscsi=True)

        conn = {'initiator': u'unicode:initiator3.%s' % rand_id,
                'ip': '10.10.10.12',
                'host': u'unicode.foo}.bar}.baz-%s' % rand_id}
        self.driver.initialize_connection(volume1, conn)
        host_name = self.driver._helpers.get_host_from_connector(
            conn, iscsi=True)
        self.assertIsNotNone(host_name)
        self.driver.terminate_connection(volume1, conn)
        host_name = self.driver._helpers.get_host_from_connector(conn)
        self.assertIsNone(host_name)
        self.driver.delete_volume(volume1)

        # Clean up temporary hosts
        for tmpconn in [tmpconn1, tmpconn2]:
            host_name = self.driver._helpers.get_host_from_connector(
                tmpconn, iscsi=True)
            self.assertIsNotNone(host_name)
            self.driver._helpers.delete_host(host_name)

    def test_storwize_svc_delete_volume_snapshots(self):
        # Create a volume with two snapshots
        master = self._create_volume()

        # Fail creating a snapshot - will force delete the snapshot
        if self.USESIM and False:
            snap = self._generate_snap_info(master.id)
            self.sim.error_injection('startfcmap', 'bad_id')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_snapshot, snap)
            self._assert_vol_exists(snap['name'], False)

        # Delete a snapshot
        snap = self._generate_snap_info(master.id)
        self.driver.create_snapshot(snap)
        self._assert_vol_exists(snap['name'], True)
        self.driver.delete_snapshot(snap)
        self._assert_vol_exists(snap['name'], False)

        # Delete a volume with snapshots (regular)
        snap = self._generate_snap_info(master.id)
        self.driver.create_snapshot(snap)
        self._assert_vol_exists(snap['name'], True)
        self.driver.delete_volume(master)
        self._assert_vol_exists(master['name'], False)

        # Fail create volume from snapshot - will force delete the volume
        if self.USESIM:
            volfs = self._generate_vol_info()
            self.sim.error_injection('startfcmap', 'bad_id')
            self.sim.error_injection('lsfcmap', 'speed_up')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_volume_from_snapshot,
                              volfs, snap)
            self._assert_vol_exists(volfs['name'], False)

        # Create volume from snapshot and delete it
        volfs = self._generate_vol_info()
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_volume_from_snapshot(volfs, snap)
        self._assert_vol_exists(volfs['name'], True)
        self.driver.delete_volume(volfs)
        self._assert_vol_exists(volfs['name'], False)

        # Create volume from snapshot and delete the snapshot
        volfs = self._generate_vol_info()
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_volume_from_snapshot(volfs, snap)
        self.driver.delete_snapshot(snap)
        self._assert_vol_exists(snap['name'], False)

        # Fail create clone - will force delete the target volume
        if self.USESIM:
            clone = self._generate_vol_info()
            self.sim.error_injection('startfcmap', 'bad_id')
            self.sim.error_injection('lsfcmap', 'speed_up')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_cloned_volume,
                              clone, volfs)
            self._assert_vol_exists(clone['name'], False)

        # Create the clone, delete the source and target
        clone = self._generate_vol_info()
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_cloned_volume(clone, volfs)
        self._assert_vol_exists(clone['name'], True)
        self.driver.delete_volume(volfs)
        self._assert_vol_exists(volfs['name'], False)
        self.driver.delete_volume(clone)
        self._assert_vol_exists(clone['name'], False)

    @ddt.data((True, None), (True, 5), (False, -1), (False, 100))
    @ddt.unpack
    def test_storwize_svc_get_volume_stats(
            self, is_thin_provisioning_enabled, rsize):
        self._set_flag('reserved_percentage', 25)
        self._set_flag('storwize_svc_multihostmap_enabled', True)
        self._set_flag('storwize_svc_vol_rsize', rsize)
        stats = self.driver.get_volume_stats(True)
        for each_pool in stats['pools']:
            self.assertIn(each_pool['pool_name'],
                          self._def_flags['storwize_svc_volpool_name'])
            self.assertTrue(each_pool['multiattach'])
            self.assertLessEqual(each_pool['free_capacity_gb'],
                                 each_pool['total_capacity_gb'])
            self.assertEqual(25, each_pool['reserved_percentage'])
            self.assertEqual(is_thin_provisioning_enabled,
                             each_pool['thin_provisioning_support'])
            self.assertEqual(not is_thin_provisioning_enabled,
                             each_pool['thick_provisioning_support'])
            self.assertTrue(each_pool['consistent_group_snapshot_enabled'])
        if self.USESIM:
            expected = 'storwize-svc-sim'
            self.assertEqual(expected, stats['volume_backend_name'])
            for each_pool in stats['pools']:
                self.assertIn(each_pool['pool_name'],
                              self._def_flags['storwize_svc_volpool_name'])
                self.assertAlmostEqual(3328.0, each_pool['total_capacity_gb'])
                self.assertAlmostEqual(3287.5, each_pool['free_capacity_gb'])
                if is_thin_provisioning_enabled:
                    self.assertAlmostEqual(
                        1576.96, each_pool['provisioned_capacity_gb'])

    def test_storwize_svc_get_volume_stats_backend_state(self):
        self._set_flag('storwize_svc_volpool_name', ['openstack', 'openstack1',
                                                     'openstack2'])

        stats = self.driver.get_volume_stats()
        for each_pool in stats['pools']:
            self.assertEqual('up', each_pool['backend_state'])

        self._reset_flags()
        self._set_flag('storwize_svc_volpool_name', ['openstack3',
                                                     'openstack4',
                                                     'openstack5'])
        stats = self.driver.get_volume_stats(True)
        for each_pool in stats['pools']:
            self.assertEqual('down', each_pool['backend_state'])

    def test_get_pool(self):
        ctxt = testutils.get_test_admin_context()
        type_ref = volume_types.create(ctxt, 'testtype', None)
        volume = self._generate_vol_info()
        volume.volume_type_id = type_ref['id']
        volume.volume_type = objects.VolumeType.get_by_id(ctxt,
                                                          type_ref['id'])
        self.driver.create_volume(volume)
        vol = self.driver._helpers.get_vdisk_attributes(volume.name)
        self.assertEqual(vol['mdisk_grp_name'],
                         self.driver.get_pool(volume))

        self.driver.delete_volume(volume)
        volume_types.destroy(ctxt, type_ref['id'])

    @ddt.data(('100', 'iops', '100', True),
              ('100', 'iops_per_gb', '1500', False),
              ('200', 'mbps', '200', False))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'update_vdisk_qos')
    @ddt.unpack
    def test_storwize_svc_extend_volume(self, old_iothrottling_value,
                                        iothrottling_unit,
                                        new_iothrottling_value,
                                        empty_qos,
                                        update_vdisk_qos):
        volume = self._create_volume()
        if empty_qos:
            self.driver.extend_volume(volume, '13')
            attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
            vol_size = int(attrs['capacity']) / units.Gi
            self.assertAlmostEqual(vol_size, 13)
            self.assertFalse(update_vdisk_qos.called)

            snap = self._generate_snap_info(volume.id)
            self.driver.create_snapshot(snap)
            self._assert_vol_exists(snap['name'], True)
            self.assertRaises(exception.VolumeDriverException,
                              self.driver.extend_volume, volume, '16')
            self.driver.delete_snapshot(snap)

        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is specified for source volume with 'iops_per_gb' as
            # IOThrottling_unit, update_vdisk_qos will be called for
            # extend_volume.
            fake_opts_qos = self._get_default_opts()
            fake_opts_qos['qos'] = {'IOThrottling': new_iothrottling_value,
                                    'IOThrottling_unit': iothrottling_unit}
            get_vdisk_params.return_value = fake_opts_qos

            self.driver.extend_volume(volume, 15)
            attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
            vol_size = int(attrs['capacity']) / units.Gi
            if fake_opts_qos['qos']['IOThrottling_unit'] == 'iops_per_gb':
                update_vdisk_qos.assert_called_with(volume['name'],
                                                    fake_opts_qos['qos'],
                                                    vol_size)
            else:
                self.assertFalse(update_vdisk_qos.called)

        self.driver.delete_volume(volume)

    @mock.patch.object(storwize_rep.StorwizeSVCReplicationGlobalMirror,
                       'create_relationship')
    @mock.patch.object(storwize_rep.StorwizeSVCReplicationGlobalMirror,
                       'extend_target_volume')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_relationship')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_relationship_info')
    def _storwize_svc_extend_volume_replication(self,
                                                get_relationship,
                                                delete_relationship,
                                                extend_target_volume,
                                                create_relationship):
        fake_target = mock.Mock()
        rep_type = 'global'
        self.driver.replications[rep_type] = (
            self.driver.replication_factory(rep_type, fake_target))
        volume = self._create_volume()
        volume['replication_status'] = fields.ReplicationStatus.ENABLED
        fake_target_vol = 'vol-target-id'
        get_relationship.return_value = {'aux_vdisk_name': fake_target_vol}
        with mock.patch.object(
                self.driver,
                '_get_volume_replicated_type_mirror') as mirror_type:
            mirror_type.return_value = 'global'
            self.driver.extend_volume(volume, '13')
            attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
            vol_size = int(attrs['capacity']) / units.Gi
            self.assertAlmostEqual(vol_size, 13)
            delete_relationship.assert_called_once_with(volume['name'])
            extend_target_volume.assert_called_once_with(fake_target_vol,
                                                         12)
            create_relationship.assert_called_once_with(volume,
                                                        fake_target_vol)

        self.driver.delete_volume(volume)

    def _storwize_svc_extend_volume_replication_failover(self):
        volume = self._create_volume()
        volume['replication_status'] = fields.ReplicationStatus.FAILED_OVER
        with mock.patch.object(
                self.driver,
                '_get_volume_replicated_type_mirror') as mirror_type:
            mirror_type.return_value = 'global'
            self.driver.extend_volume(volume, '13')
            attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
            vol_size = int(attrs['capacity']) / units.Gi
            self.assertAlmostEqual(vol_size, 13)

        self.driver.delete_volume(volume)

    def _check_loc_info(self, capabilities, expected):
        host = {'host': 'foo', 'capabilities': capabilities}
        vol = {'name': 'test', 'id': 1, 'size': 1}
        ctxt = context.get_admin_context()
        moved, model_update = self.driver.migrate_volume(ctxt, vol, host)
        self.assertEqual(expected['moved'], moved)
        self.assertEqual(expected['model_update'], model_update)

    def test_storwize_svc_migrate_bad_loc_info(self):
        self._check_loc_info({}, {'moved': False, 'model_update': None})
        cap = {'location_info': 'foo'}
        self._check_loc_info(cap, {'moved': False, 'model_update': None})
        cap = {'location_info': 'FooDriver:foo:bar'}
        self._check_loc_info(cap, {'moved': False, 'model_update': None})
        cap = {'location_info': 'StorwizeSVCDriver:foo:bar'}
        self._check_loc_info(cap, {'moved': False, 'model_update': None})

    def test_storwize_svc_volume_migrate(self):
        # Make sure we don't call migrate_volume_vdiskcopy
        self.driver.do_setup(None)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack2')
        cap = {'location_info': loc, 'extent_size': '256'}
        host = {'host': 'openstack@svc#openstack2', 'capabilities': cap}
        ctxt = context.get_admin_context()
        volume = self._create_volume()
        volume['volume_type_id'] = None
        self.driver.migrate_volume(ctxt, volume, host)
        self._delete_volume(volume)

    @ddt.data(('5000', 'iops', 5000, 'iops', True),
              ('500', 'iops_per_gb', 500, 'iops_per_gb', False),
              ('2000', 'mbps', 2000, 'mbps', False))
    @ddt.unpack
    def test_storwize_svc_get_vdisk_params(self,
                                           fake_iothrottling_value,
                                           fake_iothrottling_unit,
                                           expected_iothrottling_value,
                                           expected_iothrottling_unit,
                                           empty_qos):
        self.driver.do_setup(None)
        fake_qos = {'qos:IOThrottling': fake_iothrottling_value,
                    'qos:IOThrottling_unit': fake_iothrottling_unit}
        expected_qos = {'IOThrottling': float(expected_iothrottling_value),
                        'IOThrottling_unit': expected_iothrottling_unit}
        fake_opts = self._get_default_opts()
        # The parameters retured should be the same to the default options,
        # if the QoS is empty.
        if empty_qos:
            vol_type_empty_qos = self._create_volume_type_qos(True, None)
            type_id = vol_type_empty_qos['id']
            params = \
                self.driver._get_vdisk_params(type_id,
                                              volume_type=vol_type_empty_qos,
                                              volume_metadata=None)
            self.assertEqual(fake_opts, params)
            volume_types.destroy(self.ctxt, type_id)

        # If the QoS is set via the qos association with the volume type,
        # qos value should be set in the retured parameters.
        vol_type_qos = self._create_volume_type_qos(False, fake_qos)
        type_id = vol_type_qos['id']
        # If type_id is not none and volume_type is none, it should work fine.
        params = self.driver._get_vdisk_params(type_id, volume_type=None,
                                               volume_metadata=None)
        self.assertEqual(expected_qos, params['qos'])
        # If type_id is not none and volume_type is not none, it should
        # work fine.
        params = self.driver._get_vdisk_params(type_id,
                                               volume_type=vol_type_qos,
                                               volume_metadata=None)
        self.assertEqual(expected_qos, params['qos'])
        # If type_id is none and volume_type is not none, it should work fine.
        params = self.driver._get_vdisk_params(None, volume_type=vol_type_qos,
                                               volume_metadata=None)
        self.assertEqual(expected_qos, params['qos'])
        # If both type_id and volume_type are none, no qos will be returned
        # in the parameter.
        params = self.driver._get_vdisk_params(None, volume_type=None,
                                               volume_metadata=None)
        self.assertIsNone(params['qos'])
        qos_spec = volume_types.get_volume_type_qos_specs(type_id)
        volume_types.destroy(self.ctxt, type_id)
        qos_specs.delete(self.ctxt, qos_spec['qos_specs']['id'])

        # If the QoS is set via the extra specs in the volume type,
        # qos value should be set in the retured parameters.
        vol_type_qos = self._create_volume_type_qos(True, fake_qos)
        type_id = vol_type_qos['id']
        # If type_id is not none and volume_type is none, it should work fine.
        params = self.driver._get_vdisk_params(type_id, volume_type=None,
                                               volume_metadata=None)
        self.assertEqual(expected_qos, params['qos'])
        # If type_id is not none and volume_type is not none,
        # it should work fine.
        params = self.driver._get_vdisk_params(type_id,
                                               volume_type=vol_type_qos,
                                               volume_metadata=None)
        self.assertEqual(expected_qos, params['qos'])
        # If type_id is none and volume_type is not none,
        # it should work fine.
        params = self.driver._get_vdisk_params(None,
                                               volume_type=vol_type_qos,
                                               volume_metadata=None)
        self.assertEqual(expected_qos, params['qos'])
        # If both type_id and volume_type are none, no qos will be returned
        # in the parameter.
        params = self.driver._get_vdisk_params(None, volume_type=None,
                                               volume_metadata=None)
        self.assertIsNone(params['qos'])
        volume_types.destroy(self.ctxt, type_id)

        # If the QoS is set in the volume metadata,
        # qos value should be set in the retured parameters.
        metadata = [{'key': 'qos:IOThrottling', 'value': '4000'},
                    {'key': 'qos:IOThrottling_unit',
                     'value': fake_iothrottling_unit}]
        expected_qos_metadata = {
            'IOThrottling': 4000.0,
            'IOThrottling_unit': expected_iothrottling_unit}
        params = self.driver._get_vdisk_params(None, volume_type=None,
                                               volume_metadata=metadata)
        self.assertEqual(expected_qos_metadata, params['qos'])

        # If the QoS is set both in the metadata and the volume type, the one
        # in the volume type will take effect.
        vol_type_qos = self._create_volume_type_qos(True, fake_qos)
        type_id = vol_type_qos['id']
        params = self.driver._get_vdisk_params(type_id, volume_type=None,
                                               volume_metadata=metadata)
        self.assertEqual(expected_qos, params['qos'])
        volume_types.destroy(self.ctxt, type_id)

        # If the QoS is set both via the qos association and the
        # extra specs, the one from the qos association will take effect.
        fake_qos_associate = {'qos:IOThrottling': '6000',
                              'qos:IOThrottling_unit': fake_iothrottling_unit}
        expected_qos_associate = {
            'IOThrottling': 6000.0,
            'IOThrottling_unit': expected_iothrottling_unit}
        vol_type_qos = self._create_volume_type_qos_both(fake_qos,
                                                         fake_qos_associate)
        type_id = vol_type_qos['id']
        params = self.driver._get_vdisk_params(type_id, volume_type=None,
                                               volume_metadata=None)
        self.assertEqual(expected_qos_associate, params['qos'])
        qos_spec = volume_types.get_volume_type_qos_specs(type_id)
        volume_types.destroy(self.ctxt, type_id)
        qos_specs.delete(self.ctxt, qos_spec['qos_specs']['id'])

    @ddt.data(('5000', 'iops', 500, 'iops_per_gb', True),
              ('5000', 'iops', '2000', 'mbps', False),
              ('500', 'iops_per_gb', '5000', 'iops', False),
              ('500', 'iops_per_gb', '2000', 'mbps', False),
              ('2000', 'mbps', '5000', 'iops', False),
              ('2000', 'mbps', '500', 'iops_per_gb', False))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'disable_vdisk_qos')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'update_vdisk_qos')
    @ddt.unpack
    def test_storwize_svc_retype_no_copy(self, old_iothrottling_value,
                                         old_iothrottling_unit,
                                         new_iothrottling_value,
                                         new_iothrottling_unit,
                                         empty_qos,
                                         update_vdisk_qos,
                                         disable_vdisk_qos):
        self.driver.do_setup(None)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#openstack', 'capabilities': cap}
        ctxt = context.get_admin_context()

        key_specs_old = {'easytier': False, 'warning': 2, 'autoexpand': True}
        key_specs_new = {'easytier': True, 'warning': 5, 'autoexpand': False}
        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)

        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])
        volume = self._generate_vol_info(old_type)
        volume['host'] = host['host']
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        self.driver.retype(ctxt, volume, new_type, diff, host)
        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        self.assertEqual('on', attrs['easy_tier'], 'Volume retype failed')
        self.assertEqual('5', attrs['warning'], 'Volume retype failed')
        self.assertEqual('off', attrs['autoexpand'], 'Volume retype failed')
        self.driver.delete_volume(volume)

        fake_opts = self._get_default_opts()
        fake_opts_old = self._get_default_opts()
        fake_opts_old['qos'] = {'IOThrottling': old_iothrottling_value,
                                'IOThrottling_unit': old_iothrottling_unit}
        fake_opts_qos = self._get_default_opts()
        fake_opts_qos['qos'] = {'IOThrottling': new_iothrottling_value,
                                'IOThrottling_unit': new_iothrottling_unit}
        self.driver.create_volume(volume)
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is empty for both the source and target volumes,
            # update_vdisk_qos and disable_vdisk_qos will not be called for
            # retype.
            if empty_qos:
                get_vdisk_params.side_effect = [fake_opts,
                                                fake_opts,
                                                fake_opts]
                self.driver.retype(ctxt, volume, new_type, diff, host)
                self.assertFalse(update_vdisk_qos.called)
                self.assertFalse(disable_vdisk_qos.called)
            self.driver.delete_volume(volume)

        self.driver.create_volume(volume)
        update_vdisk_qos.reset_mock()
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is specified for both source and target volumes,
            # update_vdisk_qos will be called for retype, and disable_vdisk_qos
            # will not be called.
            get_vdisk_params.side_effect = [fake_opts_old, fake_opts_qos,
                                            fake_opts_old]
            self.driver.retype(ctxt, volume, new_type, diff, host)
            update_vdisk_qos.assert_called_with(volume['name'],
                                                fake_opts_qos['qos'],
                                                volume['size'])
            self.assertFalse(disable_vdisk_qos.called)
            self.driver.delete_volume(volume)

        self.driver.create_volume(volume)
        update_vdisk_qos.reset_mock()
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is empty for source and speficied for target volume,
            # update_vdisk_qos will be called for retype, and disable_vdisk_qos
            # will not be called.
            get_vdisk_params.side_effect = [fake_opts, fake_opts_qos,
                                            fake_opts]
            self.driver.retype(ctxt, volume, new_type, diff, host)
            update_vdisk_qos.assert_called_with(volume['name'],
                                                fake_opts_qos['qos'],
                                                volume['size'])
            self.assertFalse(disable_vdisk_qos.called)
            self.driver.delete_volume(volume)

        self.driver.create_volume(volume)
        update_vdisk_qos.reset_mock()
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is empty for target volume and specified for source
            # volume, update_vdisk_qos will not be called for retype, and
            # disable_vdisk_qos will be called.
            get_vdisk_params.side_effect = [fake_opts_qos, fake_opts,
                                            fake_opts]
            self.driver.retype(ctxt, volume, new_type, diff, host)
            self.assertFalse(update_vdisk_qos.called)
            disable_vdisk_qos.assert_called_with(volume['name'],
                                                 fake_opts_qos['qos'])
            self.driver.delete_volume(volume)

    def test_storwize_svc_retype_only_change_iogrp(self):
        self.driver.do_setup(None)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#openstack', 'capabilities': cap}
        ctxt = context.get_admin_context()

        key_specs_old = {'iogrp': 0}
        key_specs_new = {'iogrp': 1}
        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)

        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])
        volume = self._generate_vol_info(old_type)
        volume['host'] = host['host']
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        self.driver.retype(ctxt, volume, new_type, diff, host)
        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        self.assertEqual('1', attrs['IO_group_id'], 'Volume retype '
                         'failed')
        self.driver.delete_volume(volume)

        # retype a volume in dr_pool
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack3')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#openstack3', 'capabilities': cap}
        volume = testutils.create_volume(
            self.ctxt, volume_type_id=old_type.id,
            host='openstack@svc#hyperswap3')
        volume['host'] = host['host']
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, ctxt, volume,
                          new_type, diff, host)

    @ddt.data(('5000', 'iops', 500, 'iops_per_gb', True),
              ('5000', 'iops', '2000', 'mbps', False),
              ('500', 'iops_per_gb', '5000', 'iops', False),
              ('500', 'iops_per_gb', '2000', 'mbps', False),
              ('2000', 'mbps', '5000', 'iops', False),
              ('2000', 'mbps', '500', 'iops_per_gb', False))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'disable_vdisk_qos')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'update_vdisk_qos')
    @ddt.unpack
    def test_storwize_svc_retype_need_copy(self, old_iothrottling_value,
                                           old_iothrottling_unit,
                                           new_iothrottling_value,
                                           new_iothrottling_unit,
                                           empty_qos,
                                           update_vdisk_qos,
                                           disable_vdisk_qos):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'standard',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#openstack', 'capabilities': cap}
        ctxt = context.get_admin_context()

        key_specs_old = {'compression': True, 'iogrp': 0}
        key_specs_new = {'compression': False, 'iogrp': 1}
        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)

        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])
        volume = self._generate_vol_info(old_type)
        volume['host'] = host['host']
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        self.driver.retype(ctxt, volume, new_type, diff, host)
        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        self.assertEqual('no', attrs['compressed_copy'])
        self.assertEqual('1', attrs['IO_group_id'], 'Volume retype '
                         'failed')
        self.driver.delete_volume(volume)

        fake_opts = self._get_default_opts()
        fake_opts_old = self._get_default_opts()
        fake_opts_old['qos'] = {'IOThrottling': old_iothrottling_value,
                                'IOThrottling_unit': old_iothrottling_unit}
        fake_opts_qos = self._get_default_opts()
        fake_opts_qos['qos'] = {'IOThrottling': new_iothrottling_value,
                                'IOThrottling_unit': new_iothrottling_unit}
        self.driver.create_volume(volume)
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is empty for both the source and target volumes,
            # update_vdisk_qos and disable_vdisk_qos will not be called for
            # retype.
            if empty_qos:
                get_vdisk_params.side_effect = [fake_opts,
                                                fake_opts,
                                                fake_opts]
                self.driver.retype(ctxt, volume, new_type, diff, host)
                self.assertFalse(update_vdisk_qos.called)
                self.assertFalse(disable_vdisk_qos.called)
            self.driver.delete_volume(volume)

        self.driver.create_volume(volume)
        update_vdisk_qos.reset_mock()
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is specified for both source and target volumes,
            # update_vdisk_qos will be called for retype, and disable_vdisk_qos
            # will not be called.
            get_vdisk_params.side_effect = [fake_opts_old, fake_opts_qos,
                                            fake_opts_qos]
            self.driver.retype(ctxt, volume, new_type, diff, host)
            update_vdisk_qos.assert_called_with(volume['name'],
                                                fake_opts_qos['qos'],
                                                volume['size'])
            self.assertFalse(disable_vdisk_qos.called)
            self.driver.delete_volume(volume)

        self.driver.create_volume(volume)
        update_vdisk_qos.reset_mock()
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is empty for source and speficied for target volume,
            # update_vdisk_qos will be called for retype, and disable_vdisk_qos
            # will not be called.
            get_vdisk_params.side_effect = [fake_opts, fake_opts_qos,
                                            fake_opts]
            self.driver.retype(ctxt, volume, new_type, diff, host)
            update_vdisk_qos.assert_called_with(volume['name'],
                                                fake_opts_qos['qos'],
                                                volume['size'])
            self.assertFalse(disable_vdisk_qos.called)
            self.driver.delete_volume(volume)

        self.driver.create_volume(volume)
        update_vdisk_qos.reset_mock()
        with mock.patch.object(storwize_svc_iscsi.StorwizeSVCISCSIDriver,
                               '_get_vdisk_params') as get_vdisk_params:
            # If qos is empty for target volume and specified for source
            # volume, update_vdisk_qos will not be called for retype, and
            # disable_vdisk_qos will be called.
            get_vdisk_params.side_effect = [fake_opts_qos, fake_opts,
                                            fake_opts]
            self.driver.retype(ctxt, volume, new_type, diff, host)
            self.assertFalse(update_vdisk_qos.called)
            disable_vdisk_qos.assert_called_with(volume['name'],
                                                 fake_opts_qos['qos'])
            self.driver.delete_volume(volume)

    def test_set_storage_code_level_success(self):
        res = self.driver._helpers.get_system_info()
        if self.USESIM:
            self.assertEqual((7, 2, 0, 0), res['code_level'],
                             'Get code level error')

    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'rename_vdisk')
    def test_storwize_update_migrated_volume(self, rename_vdisk):
        ctxt = testutils.get_test_admin_context()
        backend_volume = self._create_volume()
        volume = self._create_volume()
        model_update = self.driver.update_migrated_volume(ctxt, volume,
                                                          backend_volume,
                                                          'available')
        rename_vdisk.assert_called_once_with(backend_volume.name, volume.name)
        self.assertEqual({'_name_id': None}, model_update)

        rename_vdisk.reset_mock()
        rename_vdisk.side_effect = exception.VolumeBackendAPIException(data='')
        model_update = self.driver.update_migrated_volume(ctxt, volume,
                                                          backend_volume,
                                                          'available')
        self.assertEqual({'_name_id': backend_volume.id}, model_update)

        rename_vdisk.reset_mock()
        rename_vdisk.side_effect = exception.VolumeBackendAPIException(data='')
        model_update = self.driver.update_migrated_volume(ctxt, volume,
                                                          backend_volume,
                                                          'attached')
        self.assertEqual({'_name_id': backend_volume.id}, model_update)
        rename_vdisk.assert_called_once_with(backend_volume.name, volume.name)

        # Now back to first 'available' test, but with volume names that don't
        # match the driver's name template. Need to use mock vols to set name.
        rename_vdisk.reset_mock()
        rename_vdisk.side_effect = None

        class MockVol(dict):
            def __getattr__(self, attr):
                return self.get(attr, None)

        target_vol = MockVol(id='1', name='new-vol-name', volume_type_id=None)
        orig_vol = MockVol(id='2', name='orig-vol-name', volume_type_id=None)
        model_update = self.driver.update_migrated_volume(ctxt, orig_vol,
                                                          target_vol,
                                                          'available')
        rename_vdisk.assert_called_once_with('new-vol-name', 'orig-vol-name')
        self.assertEqual({'_name_id': None}, model_update)

    def test_storwize_vdisk_copy_ops(self):
        ctxt = testutils.get_test_admin_context()
        volume = self._create_volume()
        driver = self.driver
        dest_pool = volume_utils.extract_host(volume['host'], 'pool')
        new_ops = driver._helpers.add_vdisk_copy(volume['name'], dest_pool,
                                                 None, self.driver._state,
                                                 self.driver.configuration)
        self.driver._add_vdisk_copy_op(ctxt, volume, new_ops)
        admin_metadata = self.db.volume_admin_metadata_get(ctxt, volume['id'])
        self.assertEqual(":".join(x for x in new_ops),
                         admin_metadata['vdiskcopyops'],
                         'Storwize driver add vdisk copy error.')
        self.driver._check_volume_copy_ops()
        self.driver._rm_vdisk_copy_op(ctxt, volume, new_ops[0], new_ops[1])
        admin_metadata = self.db.volume_admin_metadata_get(ctxt, volume['id'])
        self.assertNotIn('vdiskcopyops', admin_metadata,
                         'Storwize driver delete vdisk copy error')
        self._delete_volume(volume)

    def test_storwize_delete_with_vdisk_copy_ops(self):
        volume = self._create_volume()
        self.driver._vdiskcopyops = {volume['id']: [('0', '1')]}
        with mock.patch.object(self.driver, '_vdiskcopyops_loop'):
            self.assertIn(volume['id'], self.driver._vdiskcopyops)
            self.driver.delete_volume(volume)
            self.assertNotIn(volume['id'], self.driver._vdiskcopyops)

    # Test groups operation ####
    @ddt.data(({'group_replication_enabled': '<is> True'}, {}),
              ({'group_replication_enabled': '<is> True',
                'consistent_group_snapshot_enabled': '<is> True'}, {}),
              ({'group_snapshot_enabled': '<is> True'}, {}),
              ({'consistent_group_snapshot_enabled': '<is> True'},
               {'replication_enabled': '<is> True',
                'replication_type': '<in> metro'}))
    @ddt.unpack
    def test_storwize_group_create_with_replication(self, grp_sepc, vol_spec):
        """Test group create."""
        gr_type_ref = group_types.create(self.ctxt, 'gr_type', grp_sepc)
        gr_type = objects.GroupType.get_by_id(self.ctxt, gr_type_ref['id'])
        vol_type_ref = volume_types.create(self.ctxt, 'vol_type', vol_spec)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=gr_type.id,
                                       volume_type_ids=[vol_type_ref['id']])
        if 'group_snapshot_enabled' in grp_sepc:
            self.assertRaises(NotImplementedError,
                              self.driver.create_group, self.ctxt, group)
        else:
            model_update = self.driver.create_group(self.ctxt, group)
            self.assertEqual(fields.GroupStatus.ERROR, model_update['status'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'create_volumegroup')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_volumegroup')
    def test_storwize_create_and_delete_volumegroup(self, delete_volumegroup,
                                                    create_volumegroup):
        """Test volume group creation and deletion"""
        # Seting the storwize_volume_group to True
        self._set_flag('storwize_volume_group', True)
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (8, 5, 1, 0),
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        volumegroup_spec = {'volume_group_enabled': '<is> True'}
        volumegroup_type_ref = group_types.create(self.ctxt,
                                                  'volumegroup_type',
                                                  volumegroup_spec)
        volumegroup_type = objects.GroupType.get_by_id(
            self.ctxt, volumegroup_type_ref['id'])

        vol_type_ref = volume_types.create(self.ctxt, 'non_rep_type', {})
        volumegroup = testutils.create_group(
            self.ctxt, group_type_id=volumegroup_type.id,
            volume_type_ids=[vol_type_ref['id']])

        # Create Volume Group
        model_update = self.driver.create_group(self.ctxt, volumegroup)
        self.assertTrue(create_volumegroup.called)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        # Delete Volume Group
        model_update = self.driver.delete_group(self.ctxt, volumegroup, [])
        self.assertTrue(delete_volumegroup.called)
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'create_volumegroup')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_volumegroup')
    def test_storwize_volume_group_flag_false(self, delete_volumegroup,
                                              create_volumegroup):
        """Test volume group creation with storwize_volume_group as False"""
        # Setting the storwize_volume_group to False
        self._set_flag('storwize_volume_group', False)
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (8, 5, 1, 0),
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        volumegroup_spec = {'volume_group_enabled': '<is> True'}
        volumegroup_type_ref = group_types.create(self.ctxt,
                                                  'volumegroup_type',
                                                  volumegroup_spec)
        volumegroup_type = objects.GroupType.get_by_id(
            self.ctxt, volumegroup_type_ref['id'])

        vol_type_ref = volume_types.create(self.ctxt, 'non_rep_type', {})
        volumegroup = testutils.create_group(
            self.ctxt, group_type_id=volumegroup_type.id,
            volume_type_ids=[vol_type_ref['id']])

        # Create Volume Group
        model_update = self.driver.create_group(self.ctxt, volumegroup)
        self.assertEqual(fields.GroupStatus.ERROR,
                         model_update['status'])
        # Delete Volume Group
        model_update = self.driver.delete_group(self.ctxt, volumegroup, [])
        self.assertTrue(delete_volumegroup.called)
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'create_volumegroup')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_volumegroup')
    def test_storwize_update_volumegroup(self, delete_volumegroup,
                                         create_volumegroup):
        """Test volume group updation"""
        # Seting the storwize_volume_group to True
        self._set_flag('storwize_volume_group', True)
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (8, 5, 1, 0),
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

            # Create volumegroup type
            volumegroup_spec = {'volume_group_enabled': '<is> True'}
            volumegroup_type_ref = group_types.create(self.ctxt,
                                                      'volumegroup_type',
                                                      volumegroup_spec)
            volumegroup_type = objects.GroupType.get_by_id(
                self.ctxt, volumegroup_type_ref['id'])

            # Create volume
            vol_type_ref = volume_types.create(self.ctxt, 'non_rep_type', {})
            vol_type = objects.VolumeType.get_by_id(self.ctxt,
                                                    vol_type_ref['id'])
            volume = self._generate_vol_info(vol_type)
            self.driver.create_volume(volume)

            # Create volumegroup
            volumegroup = testutils.create_group(
                self.ctxt, group_type_id=volumegroup_type.id,
                volume_type_ids=[vol_type_ref['id']])

            model_update = self.driver.create_group(self.ctxt, volumegroup)
            self.assertTrue(create_volumegroup.called)
            self.assertEqual(fields.GroupStatus.AVAILABLE,
                             model_update['status'])

            add_vols = [volume]
            remove_vols = [volume]
            with mock.patch.object(
                    storwize_svc_common.StorwizeSVCCommonDriver,
                    '_update_volumegroup') as _update_volumegroup:
                model_update = {'status': 'available'}
                fake_update_volumegroup_info = [model_update, add_vols, None]
                _update_volumegroup.return_value = fake_update_volumegroup_info
                (model_update, add_volumes_update,
                    remove_volumes_update) = self.driver.update_group(
                        self.ctxt, volumegroup, add_vols, [])

                self.assertTrue(_update_volumegroup.called)
                self.assertEqual(fields.GroupStatus.AVAILABLE,
                                 model_update['status'])

                model_update = {'status': 'available'}
                fake_update_volumegroup_info = [model_update, None,
                                                remove_vols]
                _update_volumegroup.return_value = (
                    fake_update_volumegroup_info)
                (model_update, add_volumes_update,
                    remove_volumes_update) = self.driver.update_group(
                        self.ctxt, volumegroup, [], remove_vols)

                self.assertTrue(_update_volumegroup.called)
                self.assertEqual(fields.GroupStatus.AVAILABLE,
                                 model_update['status'])

            # Delete Volume Group
            model_update = self.driver.delete_group(self.ctxt, volumegroup,
                                                    [])
            self.assertTrue(delete_volumegroup.called)
            self.assertEqual(fields.GroupStatus.DELETED,
                             model_update[0]['status'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_system_info')
    def test_storwize_create_and_delete_volumegroup_snapshot(
            self, get_system_info):
        """Test creation and deletion of volumegroup snapshot"""
        # Seting the storwize_volume_group to True
        self._set_flag('storwize_volume_group', True)
        fake_system_info = {'code_level': (8, 5, 1, 0),
                            'system_name': 'storwize-svc-sim',
                            'system_id': '0123456789ABCDEF'}
        get_system_info.return_value = fake_system_info
        self.driver.do_setup(None)

        # Create volume
        vol_type_ref = volume_types.create(self.ctxt, 'non_rep_type', {})
        vol_type = objects.VolumeType.get_by_id(self.ctxt,
                                                vol_type_ref['id'])
        volume = self._generate_vol_info(vol_type)
        self.driver.create_volume(volume)

        # Create volumegroup type and volumegroup
        (volumegroup_type_ref, volumegroup_type, volumegroup,
            model_update) = self._create_volumegroup_type_and_volumegroup(
                vol_type_ref)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        # Add volumes to volumegroup
        add_vols = [volume]
        remove_vols = [volume]
        (model_update, add_volumes_update, remove_volumes_update) = (
            self.driver.update_group(self.ctxt, volumegroup, add_vols, []))

        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        # Create group-snapshot
        group_snapshot, snapshots = self._create_group_snapshot_in_db(
            volumegroup.id, group_type_id=volumegroup_type_ref.id)

        model_update, snapshots_model = (
            self.driver.create_group_snapshot(self.ctxt, group_snapshot,
                                              snapshots))
        self.assertEqual(fields.GroupSnapshotStatus.AVAILABLE,
                         model_update['status'])

        for snapshot in snapshots_model:
            self.assertEqual(fields.SnapshotStatus.AVAILABLE,
                             snapshot['status'])

        # Validating the snapshot_name property value
        # from metadata of the snapshot
        groupsnapshot_name = self.driver._get_volumegroup_snapshot_name(
            group_snapshot)
        for snapshot in snapshots:
            self.assertEqual(groupsnapshot_name,
                             snapshot.metadata['snapshot_name'])

        # Delete group-snapshot
        model_update, snapshots_model = self.driver.delete_group_snapshot(
            self.ctxt, group_snapshot, snapshots)
        self.assertEqual(fields.GroupSnapshotStatus.DELETED,
                         model_update['status'])

        for snapshot in snapshots_model:
            self.assertEqual(fields.SnapshotStatus.DELETED,
                             snapshot['status'])

        # Remove the volumes from volumegroup
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(
                self.ctxt, volumegroup, [], remove_vols)

        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        # Delete Volume Group
        model_update = self.driver.delete_group(self.ctxt, volumegroup,
                                                [])
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_system_info')
    def test_storwize_delete_volumegroup_with_delete_volumes(self,
                                                             get_system_info):
        """Test volume group creation and deletion"""

        fake_system_info = {'code_level': (8, 5, 1, 0),
                            'system_name': 'storwize-svc-sim',
                            'system_id': '0123456789ABCDEF'}
        get_system_info.return_value = fake_system_info
        self.driver.do_setup(None)

        # Seting the storwize_volume_group to True
        self._set_flag('storwize_volume_group', True)

        # Create volumegroup type
        volumegroup_spec = {'volume_group_enabled': '<is> True'}
        volumegroup_type_ref = group_types.create(self.ctxt,
                                                  'volumegroup_type',
                                                  volumegroup_spec)
        volumegroup_type = objects.GroupType.get_by_id(
            self.ctxt, volumegroup_type_ref['id'])

        # Create source volume
        vol_type_ref = volume_types.create(self.ctxt, 'non_rep_type', {})
        vol_type = objects.VolumeType.get_by_id(self.ctxt,
                                                vol_type_ref['id'])
        source_vol = self._generate_vol_info(vol_type)
        self.driver.create_volume(source_vol)

        # Create source volumegroup
        source_volumegroup = testutils.create_group(
            self.ctxt, group_type_id=volumegroup_type.id,
            volume_type_ids=[vol_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt, source_volumegroup)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        # Add source volumes to source volumegroup
        (model_update, add_volumes_update, remove_volumes_update) = (
            self.driver.update_group(self.ctxt, source_volumegroup,
                                     [source_vol], []))

        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        source_volumegroup_name = self.driver._get_volumegroup_name(
            source_volumegroup)
        self.assertEqual(source_volumegroup_name,
                         source_vol.metadata['Volume Group Name'])

        # Delete Volume Group
        model_update = self.driver.delete_group(self.ctxt,
                                                source_volumegroup,
                                                [source_vol])
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])
        for volume in model_update[1]:
            self.assertEqual('deleted', volume['status'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_system_info')
    @mock.patch.object(cinder.volume.volume_utils,
                       'is_group_a_type')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'create_volumegroup_snapshot')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_volumegroup_snapshot')
    def test_storwize_create_and_delete_volumegroup_snapshot_calls(
            self, delete_volumegroup_snapshot,
            create_volumegroup_snapshot, is_grp_a_cg_snapshot_type,
            vg_type, get_system_info):
        """Test creation and deletion of volumegroup snapshot"""
        fake_system_info = {'code_level': (8, 5, 1, 0),
                            'system_name': 'storwize-svc-sim',
                            'system_id': '0123456789ABCDEF'}
        get_system_info.return_value = fake_system_info
        self.driver.do_setup(None)

        # Mocking volume-group-enabled spec as true
        is_grp_a_cg_snapshot_type.side_effect = [False, False, False]
        vg_type.side_effect = [False, False, True, False, True,
                               False, False, True, False, False, True]

        # Create volume group
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=fake.GROUP_TYPE_ID,
                                       volume_type_ids=[type_ref['id']])

        # Create volume group snapshot
        group_snapshot, snapshots = self._create_group_snapshot_in_db(
            group.id)

        model_update, snapshots_model = (
            self.driver.create_group_snapshot(self.ctxt, group_snapshot,
                                              snapshots))
        self.assertEqual(fields.GroupSnapshotStatus.AVAILABLE,
                         model_update['status'])
        self.assertTrue(create_volumegroup_snapshot.called)

        # Delete volume group snapshot
        model_update, snapshots_model = (
            self.driver.delete_group_snapshot(self.ctxt, group_snapshot,
                                              snapshots))
        self.assertEqual(fields.GroupSnapshotStatus.DELETED,
                         model_update['status'])
        self.assertTrue(delete_volumegroup_snapshot.called)

        # Delete volume group
        self.driver.delete_group(self.ctxt, group, [])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'create_rccg')
    def test_storwize_group_create(self, create_rccg):
        """Test group create."""
        rccg_spec = {'consistent_group_snapshot_enabled': '<is> True'}
        rccg_type_ref = group_types.create(self.ctxt, 'cg_type', rccg_spec)
        rccg_type = objects.GroupType.get_by_id(self.ctxt, rccg_type_ref['id'])

        rep_type_ref = volume_types.create(self.ctxt, 'rep_type', {})
        rep_group = testutils.create_group(
            self.ctxt, group_type_id=rccg_type.id,
            volume_type_ids=[rep_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt, rep_group)
        self.assertFalse(create_rccg.called)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'openstack1'}
        vol_type_ref = volume_types.create(self.ctxt, 'hypertype', spec)
        group = testutils.create_group(
            self.ctxt, name='cggroup',
            group_type_id=rccg_type.id,
            volume_type_ids=[vol_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt, group)
        self.assertEqual(fields.GroupStatus.ERROR,
                         model_update['status'])

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    @mock.patch('cinder.volume.volume_utils.is_group_a_type')
    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_delete_replication_grp')
    def test_storwize_delete_group(self, _del_rep_grp, is_grp_a_cg_rep_type,
                                   is_grp_a_cg_snapshot_type):
        is_grp_a_cg_snapshot_type.side_effect = [True, True, False, True]
        is_grp_a_cg_rep_type.side_effect = [False, False, False, False,
                                            False, False]
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=fake.GROUP_TYPE_ID,
                                       volume_type_ids=[type_ref['id']])

        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), group.id)
        self.assertRaises(NotImplementedError,
                          self.driver.delete_group,
                          self.ctxt, group, volumes)

        model_update = self.driver.delete_group(self.ctxt, group, volumes)
        self.assertFalse(_del_rep_grp.called)
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])
        for volume in model_update[1]:
            self.assertEqual('deleted', volume['status'])

    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    @mock.patch('cinder.volume.volume_utils.is_group_a_type')
    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_update_replication_grp')
    def test_storwize_group_update(self, _update_rep_grp, is_grp_a_cg_rep_type,
                                   is_grp_a_cg_snapshot_type):
        """Test group update."""
        is_grp_a_cg_snapshot_type.side_effect = [False, True, True, False]
        is_grp_a_cg_rep_type.side_effect = [False, False, False, False,
                                            False, True, True]
        group = mock.MagicMock()
        self.assertRaises(NotImplementedError, self.driver.update_group,
                          self.ctxt, group, None, None)

        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(self.ctxt, group)
        self.assertFalse(_update_rep_grp.called)
        self.assertIsNone(model_update)
        self.assertIsNone(add_volumes_update)
        self.assertIsNone(remove_volumes_update)

        self.driver.update_group(self.ctxt, group)
        self.assertTrue(_update_rep_grp.called)

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_storwize_create_group_snapshot(self, is_grp_a_cg_snapshot_type):
        is_grp_a_cg_snapshot_type.side_effect = [True, True, False, True]
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=fake.GROUP_TYPE_ID,
                                       volume_type_ids=[type_ref['id']])

        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        group_snapshot, snapshots = self._create_group_snapshot_in_db(
            group.id)
        self.assertRaises(NotImplementedError,
                          self.driver.create_group_snapshot,
                          self.ctxt, group_snapshot, snapshots)

        (model_update,
         snapshots_model_update) = self.driver.create_group_snapshot(
            self.ctxt, group_snapshot, snapshots)
        self.assertEqual(fields.GroupSnapshotStatus.AVAILABLE,
                         model_update['status'],
                         "CGSnapshot created failed")

        for snapshot in snapshots_model_update:
            self.assertEqual(fields.SnapshotStatus.AVAILABLE,
                             snapshot['status'])

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_storwize_delete_group_snapshot(self, is_grp_a_cg_snapshot_type):
        is_grp_a_cg_snapshot_type.side_effect = [True, True, True, False, True]
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=fake.GROUP_TYPE_ID,
                                       volume_type_ids=[type_ref['id']])

        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)

        group_snapshot, snapshots = self._create_group_snapshot(group.id)
        self.assertRaises(NotImplementedError,
                          self.driver.delete_group_snapshot,
                          self.ctxt, group_snapshot, snapshots)

        model_update = self.driver.delete_group_snapshot(self.ctxt,
                                                         group_snapshot,
                                                         snapshots)
        self.assertEqual(fields.GroupSnapshotStatus.DELETED,
                         model_update[0]['status'])
        for volume in model_update[1]:
            self.assertEqual(fields.SnapshotStatus.DELETED, volume['status'])

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_fc_consistgrp')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_storwize_delete_consistgroup_snapshot(self,
                                                   is_grp_a_cg_snapshot_type,
                                                   delete_fc_consistgrp,
                                                   delete_vdisk):
        is_grp_a_cg_snapshot_type.side_effect = [True, True, True, False, True]
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=fake.GROUP_TYPE_ID,
                                       volume_type_ids=[type_ref['id']])

        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)

        group_snapshot, snapshots = self._create_group_snapshot(group.id)
        cgsnapshot_id = group_snapshot.id
        cg_name = 'cg_snap-' + cgsnapshot_id

        self.driver._helpers.delete_consistgrp_snapshots(cg_name, snapshots)

        delete_fc_consistgrp.assert_has_calls([mock.call(cg_name)])
        self.assertEqual(2, delete_fc_consistgrp.call_count)

        calls = [mock.call(snapshots[0]['name'], force_delete=True,
                           force_unmap=False),
                 mock.call(snapshots[1]['name'], force_delete=True,
                           force_unmap=False)]
        delete_vdisk.assert_has_calls(calls, any_order=True)

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_fc_consistgrp')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_storwize_delete_consistgroup_snapshot_1(self,
                                                     is_grp_a_cg_snapshot_type,
                                                     delete_fc_consistgrp,
                                                     delete_vdisk):
        is_grp_a_cg_snapshot_type.side_effect = [True, True, True, False, True]
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=fake.GROUP_TYPE_ID,
                                       volume_type_ids=[type_ref['id']])

        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)

        group_snapshot, snapshots = self._create_group_snapshot(group.id)
        cgsnapshot_id = group_snapshot.id
        cg_name = 'cg_snap-' + cgsnapshot_id
        delete_vdisk.side_effect = exception.VolumeBackendAPIException(data='')

        (model_update,
         snap_model_update) = self.driver._helpers.delete_consistgrp_snapshots(
            cg_name, snapshots)
        self.assertEqual(fields.GroupSnapshotStatus.ERROR_DELETING,
                         model_update['status'])

        for snapshot in snap_model_update:
            self.assertEqual(fields.SnapshotStatus.ERROR_DELETING,
                             snapshot['status'])

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    def test_storwize_create_group_from_src_invalid(self):
        # Invalid input case for create group from src
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        cg_spec = {'consistent_group_snapshot_enabled': '<is> True'}
        rccg_spec = {'consistent_group_replication_enabled': '<is> True'}
        cg_type_ref = group_types.create(self.ctxt, 'cg_type', cg_spec)
        rccg_type_ref = group_types.create(self.ctxt, 'rccg_type', rccg_spec)
        vg_type_ref = group_types.create(self.ctxt, 'vg_type', None)

        # create group in db
        group = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                         group_type_id=vg_type_ref.id)
        self.assertRaises(NotImplementedError,
                          self.driver.create_group_from_src,
                          self.ctxt, group, None, None, None,
                          None, None)

        group = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                         group_type_id=rccg_type_ref.id)
        vol1 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       group_id=group.id)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_group_from_src,
                          self.ctxt, group, [vol1])

        hyper_specs = {'hyperswap_group_enabled': '<is> False'}
        hyper_type_ref = group_types.create(self.ctxt, 'hypergroup',
                                            hyper_specs)
        group = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                         group_type_id=hyper_type_ref.id)
        vol1 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       group_id=group.id)
        self.assertRaises(NotImplementedError,
                          self.driver.create_group_from_src,
                          self.ctxt, group, [vol1])

        group = self._create_group_in_db(volume_type_id=type_ref.id,
                                         group_type_id=cg_type_ref.id)

        # create volumes in db
        vol1 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       group_id=group.id)
        vol2 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       group_id=group.id)
        volumes = [vol1, vol2]

        source_cg = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                             group_type_id=cg_type_ref.id)

        # Add volumes to source CG
        src_vol1 = self._create_volume(volume_type_id=type_ref.id,
                                       group_id=source_cg['id'])
        src_vol2 = self._create_volume(volume_type_id=type_ref.id,
                                       group_id=source_cg['id'])
        source_vols = [src_vol1, src_vol2]

        group_snapshot, snapshots = self._create_group_snapshot(
            source_cg['id'], group_type_id=cg_type_ref.id)

        # Create group from src with null input
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_group_from_src,
                          self.ctxt, group, volumes, None, None,
                          None, None)

        # Create cg from src with source_cg and empty source_vols
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_group_from_src,
                          self.ctxt, group, volumes, None, None,
                          source_cg, None)

        # Create cg from src with source_vols and empty source_cg
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_group_from_src,
                          self.ctxt, group, volumes, None, None,
                          None, source_vols)

        # Create cg from src with cgsnapshot and empty snapshots
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_group_from_src,
                          self.ctxt, group, volumes, group_snapshot, None,
                          None, None)
        # Create cg from src with snapshots and empty cgsnapshot
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_group_from_src,
                          self.ctxt, group, volumes, None, snapshots,
                          None, None)

        model_update = self.driver.delete_group(self.ctxt, group, volumes)

        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])
        for volume in model_update[1]:
            self.assertEqual('deleted', volume['status'])

        model_update = self.driver.delete_group(self.ctxt,
                                                source_cg, source_vols)

        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])
        for volume in model_update[1]:
            self.assertEqual('deleted', volume['status'])

        model_update = self.driver.delete_group(self.ctxt,
                                                group_snapshot, snapshots)

        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])
        for volume in model_update[1]:
            self.assertEqual('deleted', volume['status'])

    @ddt.data(('5000', 'iops', True),
              ('500', 'iops_per_gb', False),
              ('3000', 'mbps', False))
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'create_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'mkfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_pool')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'add_vdisk_qos')
    @ddt.unpack
    def test_storwize_create_flashcopy_to_consistgrp(self,
                                                     fake_iothrottling_value,
                                                     fake_iothrottling_unit,
                                                     empty_qos,
                                                     add_vdisk_qos,
                                                     _get_pool,
                                                     mkfcmap,
                                                     create_vdisk,
                                                     get_vdisk_attributes):
        source = "volume-36cd5a6f-a13c-456c-8129-c3e8874fb15c"
        target = "volume-55eb6c7e-a13c-456c-8129-c3e8874kl34f"
        consistgrp = "cg_snap-9021b016-ce1e-4145-a1f0-0bd4007a3a78"
        fake_target_size = 0
        config = self.driver.configuration
        pool = "openstack2"
        if empty_qos:
            opts = {'rsize': 2, 'iogrp': 0, 'qos': None, 'flashcopy_rate': 50,
                    'clean_rate': 50}
            self.driver._helpers.create_flashcopy_to_consistgrp(
                source, target, consistgrp, config,
                opts, full_copy=False, pool=pool)
            _get_pool.assert_not_called()
            add_vdisk_qos.assert_not_called()

        qos = {'IOThrottling': fake_iothrottling_value,
               'IOThrottling_unit': fake_iothrottling_unit}
        opts = {'rsize': 2, 'iogrp': 0, 'qos': qos, 'flashcopy_rate': 50,
                'clean_rate': 50}
        self.driver._helpers.create_flashcopy_to_consistgrp(source,
                                                            target, consistgrp,
                                                            config, opts,
                                                            full_copy=False,
                                                            pool=pool)
        add_vdisk_qos.assert_called_with(target, opts['qos'], fake_target_size)
        pool = None
        self.driver._helpers.create_flashcopy_to_consistgrp(source,
                                                            target, consistgrp,
                                                            config, opts,
                                                            full_copy=False,
                                                            pool=pool)
        _get_pool.assert_called_with(get_vdisk_attributes())

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_copies')
    def test_storwize_get_pool(self, get_vdisk_copies):
        vol_attrs = {'mdisk_grp_name': 'openstack', 'IO_group_id': 0,
                     'capacity': 1, 'name': 'vol1'}
        self.driver._helpers._get_pool(vol_attrs)
        get_vdisk_copies.assert_not_called()
        vol_attrs['mdisk_grp_name'] = 'many'
        self.driver._helpers._get_pool(vol_attrs)
        get_vdisk_copies.assert_called_once()

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'create_flashcopy_to_consistgrp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_params')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_fc_consistgrp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'prepare_fc_consistgrp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_fc_consistgrp')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_pool')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_attributes')
    def test_run_consistgrp_snapshots_forhost(
            self,
            get_vdisk_attributes,
            _get_pool,
            is_grp_a_cg_snapshot_type,
            delete_fc_consistgrp,
            prepare_fc_consistgrp,
            start_fc_consistgrp,
            get_vdisk_params,
            create_flashcopy_to_consistgrp):
        fake_opts = self._get_default_opts()
        get_vdisk_params.return_value = fake_opts
        is_grp_a_cg_snapshot_type.side_effect = [True, True, True, False, True]
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=fake.GROUP_TYPE_ID,
                                       volume_type_ids=[type_ref['id']])
        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        self._create_volume(volume_type_id=type_ref['id'], group_id=group.id)
        group_snapshot, snapshots = self._create_group_snapshot(group.id)
        cgsnapshot_id = group_snapshot.id
        fc_consistgrp = 'cg_snap-' + cgsnapshot_id
        config = None
        state = self.driver._state
        timeout = 20
        self.driver._helpers.run_consistgrp_snapshots(fc_consistgrp, snapshots,
                                                      state, config, timeout)
        start_fc_consistgrp.assert_called_with(fc_consistgrp)
        _get_pool.assert_not_called()
        get_vdisk_attributes.assert_not_called()

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'create_flashcopy_to_consistgrp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_params')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_fc_consistgrp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'prepare_fc_consistgrp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_fc_consistgrp')
    @mock.patch('cinder.volume.volume_utils.extract_host')
    def test_create_cg_from_source_forhost(
            self,
            extract_host,
            delete_fc_consistgrp,
            prepare_fc_consistgrp,
            start_fc_consistgrp,
            get_vdisk_params,
            create_flashcopy_to_consistgrp):
        fake_opts = self._get_default_opts()
        get_vdisk_params.return_value = fake_opts
        extract_host.return_value = 'openstack'
        # Valid case for create cg from src
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        spec = {'consistent_group_snapshot_enabled': '<is> True'}
        cg_type_ref = group_types.create(self.ctxt, 'cg_type', spec)
        pool = _get_test_pool()
        # Create cg in db
        tgt_group = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                             group_type_id=cg_type_ref.id)
        # Create volumes in db without hash
        testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                group_id=tgt_group.id,
                                host='openstack@svc%s' % pool)
        testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                consistencygroup_id=tgt_group.id,
                                host='openstack@svc%s' % pool)
        tgt_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), tgt_group.id)

        # Create source CG
        source_cg = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                             group_type_id=cg_type_ref.id)
        # Add volumes to source CG
        self._create_volume(volume_type_id=type_ref.id,
                            group_id=source_cg['id'])
        self._create_volume(volume_type_id=type_ref.id,
                            group_id=source_cg['id'])
        source_vols = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), source_cg['id'])

        fc_consistgrp = 'cg_snap-' + source_cg.id

        config = None
        state = self.driver._state
        timeout = 20

        # test create_cg_from_source from volume group
        self.driver._helpers.create_cg_from_source(tgt_group, fc_consistgrp,
                                                   source_vols, tgt_volumes,
                                                   state, config, timeout)
        start_fc_consistgrp.assert_called_with(fc_consistgrp)
        self.assertEqual(2, extract_host.call_count)

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    def test_storwize_group_from_src(self):
        # Valid case for create cg from src
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        spec = {'consistent_group_snapshot_enabled': '<is> True'}
        cg_type_ref = group_types.create(self.ctxt, 'cg_type', spec)
        pool = _get_test_pool()
        # Create cg in db
        group = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                         group_type_id=cg_type_ref.id)
        # Create volumes in db
        testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                group_id=group.id,
                                host='openstack@svc#%s' % pool)
        testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                consistencygroup_id=group.id,
                                host='openstack@svc#%s' % pool)
        volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), group.id)

        # Create source CG
        source_cg = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                             group_type_id=cg_type_ref.id)
        # Add volumes to source CG
        self._create_volume(volume_type_id=type_ref.id,
                            group_id=source_cg['id'])
        self._create_volume(volume_type_id=type_ref.id,
                            group_id=source_cg['id'])
        source_vols = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), source_cg['id'])

        # Create cgsnapshot
        group_snapshot, snapshots = self._create_group_snapshot(
            source_cg['id'], group_type_id=cg_type_ref.id)

        # Create cg from source cg
        model_update, volumes_model_update = (
            self.driver.create_group_from_src(self.ctxt, group, volumes, None,
                                              None, source_cg, source_vols))
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'],
                         "CG create from src created failed")
        for each_vol in volumes_model_update:
            self.assertEqual('available', each_vol['status'])

        # Delete the Group
        model_update = self.driver.delete_group(self.ctxt, group, volumes)
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])

        for each_vol in model_update[1]:
            self.assertEqual('deleted', each_vol['status'])

        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'create_rccg')) as create_rccg:
            # Create cg from source cg
            model_update, volumes_model_update = (
                self.driver.create_group_from_src(self.ctxt, group, volumes,
                                                  None, None, source_cg,
                                                  source_vols))
            create_rccg.assert_not_called()

        # Delete the Group
        model_update = self.driver.delete_group(self.ctxt, group, volumes)

        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])

        for each_vol in model_update[1]:
            self.assertEqual('deleted', each_vol['status'])

        # Create cg from cg snapshot
        model_update, volumes_model_update = (
            self.driver.create_group_from_src(self.ctxt, group, volumes,
                                              group_snapshot, snapshots,
                                              None, None))
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'],
                         "CG create from src created failed")
        for each_vol in volumes_model_update:
            self.assertEqual('available', each_vol['status'])

        model_update = self.driver.delete_group(self.ctxt, group, volumes)
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])
        for each_vol in model_update[1]:
            self.assertEqual('deleted', each_vol['status'])

        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'create_rccg')) as create_rccg:
            # Create cg from cg snapshot
            model_update, volumes_model_update = (
                self.driver.create_group_from_src(self.ctxt, group, volumes,
                                                  group_snapshot, snapshots,
                                                  None, None))
            create_rccg.assert_not_called()

        model_update = self.driver.delete_group_snapshot(self.ctxt,
                                                         group_snapshot,
                                                         snapshots)
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])
        for volume in model_update[1]:
            self.assertEqual('deleted', volume['status'])

    # mirror/strtch cluster volume test cases
    def test_storwize_svc_create_mirror_volume(self):
        # create mirror volume in invalid pool
        spec = {'mirror_pool': 'invalid_pool'}
        mirror_vol_type = self._create_volume_type(spec, 'invalid_mirror_type')
        vol = self._generate_vol_info(mirror_vol_type)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        spec = {'mirror_pool': 'openstack1'}
        mirror_vol_type = self._create_volume_type(spec, 'test_mirror_type')
        vol = self._generate_vol_info(mirror_vol_type)
        self.driver.create_volume(vol)
        self._assert_vol_exists(vol.name, True)

        copies = self.driver._helpers.get_vdisk_copies(vol.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack1')
        self.driver.delete_volume(vol)
        self._assert_vol_exists(vol['name'], False)

    def test_storwize_svc_snapshots_mirror_volume(self):
        vol1 = self._generate_vol_info(self.mirror_vol_type)
        self.driver.create_volume(vol1)

        snap1 = self._generate_snap_info(vol1.id)
        self._assert_vol_exists(snap1.name, False)

        self.driver.create_snapshot(snap1)
        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self._assert_vol_exists(snap1.name, True)
        copies = self.driver._helpers.get_vdisk_copies(snap1.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack1')

        self.driver.delete_snapshot(snap1)
        self.driver.delete_volume(vol1)

    def test_storwize_svc_create_cloned_mirror_volume(self):
        vol1 = self._generate_vol_info(self.mirror_vol_type)
        self.driver.create_volume(vol1)
        vol2 = self._generate_vol_info(self.mirror_vol_type)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.create_cloned_volume(vol2, vol1)
        self._assert_vol_exists(vol2.name, True)
        copies = self.driver._helpers.get_vdisk_copies(vol2.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack1')

        self.driver.delete_volume(vol2)
        self._assert_vol_exists(vol2.name, False)
        self.driver.delete_volume(vol1)
        self._assert_vol_exists(vol1.name, False)

    def test_storwize_svc_create_mirror_volume_from_snapshot(self):
        vol1 = self._generate_vol_info(self.mirror_vol_type)
        self.driver.create_volume(vol1)
        snap1 = self._generate_snap_info(vol1.id)
        self.driver.create_snapshot(snap1)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')

        vol2 = self._generate_vol_info(self.mirror_vol_type)
        self.driver.create_volume_from_snapshot(vol2, snap1)
        self._assert_vol_exists(vol2.name, True)
        copies = self.driver._helpers.get_vdisk_copies(vol2.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack1')

        self.driver.delete_volume(vol2)
        self._assert_vol_exists(vol2['name'], False)
        self.driver.delete_snapshot(snap1)
        self._assert_vol_exists(snap1['name'], False)
        self.driver.delete_volume(vol1)
        self._assert_vol_exists(vol1['name'], False)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'add_vdisk_copy')
    def test_storwize_svc_mirror_volume_migrate(self, add_vdisk_copy):
        # use migratevdisk for mirror volume migration, rather than
        # addvdiskcopy
        self.driver.do_setup(None)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack2')
        host = {'host': 'openstack@svc#openstack2',
                'capabilities': {'location_info': loc}}
        ctxt = context.get_admin_context()
        vol1 = self._generate_vol_info(self.mirror_vol_type)
        self.driver.create_volume(vol1)
        copies = self.driver._helpers.get_vdisk_copies(vol1.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack1')

        self.driver.migrate_volume(ctxt, vol1, host)
        copies = self.driver._helpers.get_vdisk_copies(vol1.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack2')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack1')
        self.assertFalse(add_vdisk_copy.called)
        self._delete_volume(vol1)

    @ddt.data(({'mirror_pool': 'openstack1'},
               {'mirror_pool': 'openstack1', 'compression': True}),
              ({'compression': False},
               {'mirror_pool': 'openstack1', 'compression': True}),
              ({}, {'mirror_pool': 'invalidpool'}))
    @ddt.unpack
    def test_storwize_svc_retype_mirror_volume_invalid(self, old_opts,
                                                       new_opts):
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}
        ctxt = context.get_admin_context()

        vol_type1 = self._create_volume_type(old_opts, 'old')
        vol_type2 = self._create_volume_type(new_opts, 'new')
        diff, _equal = volume_types.volume_types_diff(ctxt, vol_type1.id,
                                                      vol_type2.id)
        vol1 = self._generate_vol_info(vol_type1)
        self.driver.create_volume(vol1)

        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, self.ctxt, vol1,
                          vol_type2, diff, host)
        self.driver.delete_volume(vol1)

    @ddt.data(({'mirror_pool': 'openstack1'}, {}),
              ({'mirror_pool': 'openstack1'}, {'mirror_pool': ''}))
    @ddt.unpack
    def test_storwize_retype_from_mirror_to_none_mirror(self,
                                                        old_opts, new_opts):
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}
        ctxt = context.get_admin_context()

        vol_type1 = self._create_volume_type(old_opts, 'old')
        vol_type2 = self._create_volume_type(new_opts, 'new')
        diff, _equal = volume_types.volume_types_diff(ctxt, vol_type1.id,
                                                      vol_type2.id)
        vol1 = self._generate_vol_info(vol_type1)
        self.driver.create_volume(vol1)

        self._assert_vol_exists(vol1.name, True)
        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(len(copies), 2)

        self.driver.retype(self.ctxt, vol1, vol_type2, diff, host)
        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(len(copies), 1)
        copies = self.driver._helpers.get_vdisk_copies(vol1.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')

        self.driver.delete_volume(vol1)

    @ddt.data(({}, {'mirror_pool': 'openstack1'}),
              ({'mirror_pool': ''}, {'mirror_pool': 'openstack1'}))
    @ddt.unpack
    def test_storwize_retype_from_none_to_mirror_volume(self,
                                                        old_opts, new_opts):
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}
        ctxt = context.get_admin_context()

        old_opts = {}
        new_opts = {'mirror_pool': 'openstack1'}
        vol_type1 = self._create_volume_type(old_opts, 'old')
        vol_type2 = self._create_volume_type(new_opts, 'new')
        diff, _equal = volume_types.volume_types_diff(ctxt, vol_type1.id,
                                                      vol_type2.id)
        vol1 = self._generate_vol_info(vol_type1)
        self.driver.create_volume(vol1)

        self._assert_vol_exists(vol1.name, True)
        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(len(copies), 1)

        self.driver.retype(self.ctxt, vol1, vol_type2, diff, host)
        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(len(copies), 2)
        copies = self.driver._helpers.get_vdisk_copies(vol1.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack1')

        self.driver.delete_volume(vol1)

    @ddt.data(({'mirror_pool': 'openstack1'}, {'mirror_pool': 'openstack2'}))
    @ddt.unpack
    def test_storwize_retype_from_mirror_to_different_mirror(self,
                                                             old_opts,
                                                             new_opts):
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}
        ctxt = context.get_admin_context()

        vol_type1 = self._create_volume_type(old_opts, 'old')
        vol_type2 = self._create_volume_type(new_opts, 'new')
        diff, _equal = volume_types.volume_types_diff(ctxt, vol_type1.id,
                                                      vol_type2.id)
        vol1 = self._generate_vol_info(vol_type1)
        self.driver.create_volume(vol1)

        self._assert_vol_exists(vol1.name, True)
        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(len(copies), 2)
        copies = self.driver._helpers.get_vdisk_copies(vol1.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack1')

        self.driver.retype(self.ctxt, vol1, vol_type2, diff, host)
        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(len(copies), 2)
        copies = self.driver._helpers.get_vdisk_copies(vol1.name)
        self.assertEqual(copies['primary']['mdisk_grp_name'], 'openstack')
        self.assertEqual(copies['secondary']['mdisk_grp_name'], 'openstack2')

        self.driver.delete_volume(vol1)

    @ddt.data(({}, {'mirror_pool': 'openstack1'}),
              ({'mirror_pool': ''}, {'mirror_pool': 'openstack1'}),
              ({'mirror_pool': 'openstack1'}, {}),
              ({'mirror_pool': 'openstack1'}, {'mirror_pool': ''}),
              ({'mirror_pool': 'openstack1'}, {'mirror_pool': 'invalidpool'}))
    @ddt.unpack
    def test_storwize_manage_existing_mismatch_with_mirror_volume(
            self, opts1, opts2):
        self.driver.do_setup(self.ctxt)
        vol_type1 = self._create_volume_type(opts1, 'vol_type1')
        vol_type2 = self._create_volume_type(opts2, 'vol_type2')
        vol1 = self._generate_vol_info(vol_type1)
        self.driver.create_volume(vol1)
        vol2 = self._generate_vol_info(vol_type2)

        ref = {'source-name': vol1.name}
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, vol2, ref)

        self.driver.delete_volume(vol1)

    def test_storwize_manage_existing_with_mirror_volume(self):
        self.driver.do_setup(self.ctxt)
        vol1 = self._generate_vol_info(self.mirror_vol_type)
        self.driver.create_volume(vol1)
        uid_of_vol1 = self._get_vdisk_uid(vol1.name)

        opts1 = {'mirror_pool': 'openstack1'}
        new_volume_type = self._create_volume_type(opts1, 'new_mirror_type')
        new_volume = self._generate_vol_info(new_volume_type)
        ref = {'source-name': vol1.name}
        self.driver.manage_existing(new_volume, ref)

        # Check the uid of the volume which has been renamed.
        uid_of_new_vol = self._get_vdisk_uid(new_volume.name)
        self.assertEqual(uid_of_vol1, uid_of_new_vol)

        self.driver.delete_volume(new_volume)

    def _create_volume_type_qos(self, extra_specs, fake_qos):
        # Generate a QoS volume type for volume.
        if extra_specs:
            spec = fake_qos
            type_ref = volume_types.create(self.ctxt, "qos_extra_specs", spec)
        else:
            type_ref = volume_types.create(self.ctxt, "qos_associate", None)
            if fake_qos:
                qos_ref = qos_specs.create(self.ctxt, 'qos-specs', fake_qos)
                qos_specs.associate_qos_with_type(self.ctxt, qos_ref['id'],
                                                  type_ref['id'])

        qos_type = volume_types.get_volume_type(self.ctxt, type_ref['id'])
        return qos_type

    def _create_volume_type_qos_both(self, fake_qos, fake_qos_associate):
        type_ref = volume_types.create(self.ctxt, "qos_extra_specs", fake_qos)
        qos_ref = qos_specs.create(self.ctxt, 'qos-specs', fake_qos_associate)
        qos_specs.associate_qos_with_type(self.ctxt, qos_ref['id'],
                                          type_ref['id'])
        qos_type = volume_types.get_volume_type(self.ctxt, type_ref['id'])
        return qos_type

    def _create_replication_volume_type(self, enable):
        # Generate a volume type for volume repliation.
        if enable:
            spec = {'capabilities:replication': '<is> True'}
            type_ref = volume_types.create(self.ctxt, "replication_1", spec)
        else:
            spec = {'capabilities:replication': '<is> False'}
            type_ref = volume_types.create(self.ctxt, "replication_2", spec)

        replication_type = objects.VolumeType.get_by_id(self.ctxt,
                                                        type_ref['id'])
        return replication_type

    def _create_consistency_group_volume_type(self):
        # Generate a volume type for volume consistencygroup.
        spec = {'capabilities:consistencygroup_support': '<is> True'}
        type_ref = volume_types.create(self.ctxt, "cg", spec)

        cg_type = volume_types.get_volume_type(self.ctxt, type_ref['id'])

        return cg_type

    def _get_vdisk_uid(self, vdisk_name):
        """Return vdisk_UID for given vdisk.

        Given a vdisk by name, performs an lvdisk command that extracts
        the vdisk_UID parameter and returns it.
        Returns None if the specified vdisk does not exist.
        """
        vdisk_properties, _err = self.sim._cmd_lsvdisk(obj=vdisk_name,
                                                       delim='!')

        # Iterate through each row until we find the vdisk_UID entry
        for row in vdisk_properties.split('\n'):
            words = row.split('!')
            if words[0] == 'vdisk_UID':
                return words[1]
        return None

    def _create_volume_and_return_uid(self, volume_name):
        """Creates a volume and returns its UID.

        Creates a volume with the specified name, and returns the UID that
        the Storwize controller allocated for it.  We do this by executing a
        create_volume and then calling into the simulator to perform an
        lsvdisk directly.
        """
        volume = self._generate_vol_info()
        self.driver.create_volume(volume)

        return (volume, self._get_vdisk_uid(volume['name']))

    def test_manage_existing_get_size_bad_ref(self):
        """Error on manage with bad reference.

        This test case attempts to manage an existing volume but passes in
        a bad reference that the Storwize driver doesn't understand.  We
        expect an exception to be raised.
        """
        volume = self._generate_vol_info()
        ref = {}
        self.assertRaises(exception.ManageExistingInvalidReference,
                          self.driver.manage_existing_get_size, volume, ref)

    def test_manage_existing_get_size_bad_uid(self):
        """Error when the specified UUID does not exist."""
        volume = self._generate_vol_info()
        ref = {'source-id': 'bad_uid'}
        self.assertRaises(exception.ManageExistingInvalidReference,
                          self.driver.manage_existing_get_size, volume, ref)
        pass

    def test_manage_existing_get_size_bad_name(self):
        """Error when the specified name does not exist."""
        volume = self._generate_vol_info()
        ref = {'source-name': 'bad_name'}
        self.assertRaises(exception.ManageExistingInvalidReference,
                          self.driver.manage_existing_get_size, volume, ref)

    def test_manage_existing_bad_ref(self):
        """Error on manage with bad reference.

        This test case attempts to manage an existing volume but passes in
        a bad reference that the Storwize driver doesn't understand.  We
        expect an exception to be raised.
        """

        # Error when neither UUID nor name are specified.
        volume = self._generate_vol_info()
        ref = {}
        self.assertRaises(exception.ManageExistingInvalidReference,
                          self.driver.manage_existing, volume, ref)

        # Error when the specified UUID does not exist.
        volume = self._generate_vol_info()
        ref = {'source-id': 'bad_uid'}
        self.assertRaises(exception.ManageExistingInvalidReference,
                          self.driver.manage_existing, volume, ref)

        # Error when the specified name does not exist.
        volume = self._generate_vol_info()
        ref = {'source-name': 'bad_name'}
        self.assertRaises(exception.ManageExistingInvalidReference,
                          self.driver.manage_existing, volume, ref)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_copy_attrs')
    def test_manage_existing_mismatch(self,
                                      get_vdisk_copy_attrs):
        ctxt = testutils.get_test_admin_context()
        _volume, uid = self._create_volume_and_return_uid('manage_test')

        opts = {'rsize': -1}
        type_thick_ref = volume_types.create(ctxt, 'testtype1', opts)

        opts = {'rsize': 2}
        type_thin_ref = volume_types.create(ctxt, 'testtype2', opts)

        opts = {'rsize': 2, 'compression': True}
        type_comp_ref = volume_types.create(ctxt, 'testtype3', opts)

        opts = {'rsize': -1, 'iogrp': 1}
        type_iogrp_ref = volume_types.create(ctxt, 'testtype4', opts)

        new_volume = self._generate_vol_info()
        ref = {'source-name': _volume['name']}

        fake_copy_thin = self._get_default_opts()
        fake_copy_thin['autoexpand'] = 'on'

        fake_copy_comp = self._get_default_opts()
        fake_copy_comp['autoexpand'] = 'on'
        fake_copy_comp['compressed_copy'] = 'yes'

        fake_copy_thick = self._get_default_opts()
        fake_copy_thick['autoexpand'] = ''
        fake_copy_thick['compressed_copy'] = 'no'

        fake_copy_no_comp = self._get_default_opts()
        fake_copy_no_comp['compressed_copy'] = 'no'

        valid_iogrp = self.driver._state['available_iogrps']
        self.driver._state['available_iogrps'] = [9999]
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)
        self.driver._state['available_iogrps'] = valid_iogrp

        get_vdisk_copy_attrs.side_effect = [fake_copy_thin,
                                            fake_copy_thick,
                                            fake_copy_no_comp,
                                            fake_copy_comp,
                                            fake_copy_thick,
                                            fake_copy_thick
                                            ]
        new_volume['volume_type_id'] = type_thick_ref['id']
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        new_volume['volume_type_id'] = type_thin_ref['id']
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        new_volume['volume_type_id'] = type_comp_ref['id']
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        new_volume['volume_type_id'] = type_thin_ref['id']
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        new_volume['volume_type_id'] = type_iogrp_ref['id']
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        new_volume['volume_type_id'] = type_thick_ref['id']
        no_exist_pool = 'i-dont-exist-%s' % random.randint(10000, 99999)
        new_volume['host'] = 'openstack@svc#%s' % no_exist_pool
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        self._reset_flags()
        volume_types.destroy(ctxt, type_thick_ref['id'])
        volume_types.destroy(ctxt, type_comp_ref['id'])
        volume_types.destroy(ctxt, type_iogrp_ref['id'])

    def test_manage_existing_good_uid_not_mapped(self):
        """Tests managing a volume with no mappings.

        This test case attempts to manage an existing volume by UID, and
        we expect it to succeed.  We verify that the backend volume was
        renamed to have the name of the Cinder volume that we asked for it to
        be associated with.
        """

        # Create a volume as a way of getting a vdisk created, and find out the
        # UID of that vdisk.
        _volume, uid = self._create_volume_and_return_uid('manage_test')

        # Descriptor of the Cinder volume that we want to own the vdisk
        # referenced by uid.
        new_volume = self._generate_vol_info()

        # Submit the request to manage it.
        ref = {'source-id': uid}
        size = self.driver.manage_existing_get_size(new_volume, ref)
        self.assertEqual(10, size)
        self.driver.manage_existing(new_volume, ref)

        # Assert that there is a disk named after the new volume that has the
        # ID that we passed in, indicating that the disk has been renamed.
        uid_of_new_volume = self._get_vdisk_uid(new_volume['name'])
        self.assertEqual(uid, uid_of_new_volume)

    def test_manage_existing_good_name_not_mapped(self):
        """Tests managing a volume with no mappings.

        This test case attempts to manage an existing volume by name, and
        we expect it to succeed.  We verify that the backend volume was
        renamed to have the name of the Cinder volume that we asked for it to
        be associated with.
        """

        # Create a volume as a way of getting a vdisk created, and find out the
        # UID of that vdisk.
        _volume, uid = self._create_volume_and_return_uid('manage_test')

        # Descriptor of the Cinder volume that we want to own the vdisk
        # referenced by uid.
        new_volume = self._generate_vol_info()

        # Submit the request to manage it.
        ref = {'source-name': _volume['name']}
        size = self.driver.manage_existing_get_size(new_volume, ref)
        self.assertEqual(10, size)
        self.driver.manage_existing(new_volume, ref)

        # Assert that there is a disk named after the new volume that has the
        # ID that we passed in, indicating that the disk has been renamed.
        uid_of_new_volume = self._get_vdisk_uid(new_volume['name'])
        self.assertEqual(uid, uid_of_new_volume)

    def test_manage_existing_mapped(self):
        """Tests managing a mapped volume with no override.

        This test case attempts to manage an existing volume by UID, but
        the volume is mapped to a host, so we expect to see an exception
        raised.
        """
        # Create a volume as a way of getting a vdisk created, and find out the
        # UUID of that vdisk.
        # Set replication target.
        volume, uid = self._create_volume_and_return_uid('manage_test')

        # Map a host to the disk
        conn = {'initiator': u'unicode:initiator3',
                'ip': '10.10.10.12',
                'host': u'unicode.foo}.bar}.baz'}
        self.driver.initialize_connection(volume, conn)

        # Descriptor of the Cinder volume that we want to own the vdisk
        # referenced by uid.
        volume = self._generate_vol_info()
        ref = {'source-id': uid}

        # Attempt to manage this disk, and except an exception beause the
        # volume is already mapped.
        self.assertRaises(exception.ManageExistingInvalidReference,
                          self.driver.manage_existing_get_size, volume, ref)

        ref = {'source-name': volume['name']}
        self.assertRaises(exception.ManageExistingInvalidReference,
                          self.driver.manage_existing_get_size, volume, ref)

    def test_manage_existing_good_uid_mapped_with_override(self):
        """Tests managing a mapped volume with override.

        This test case attempts to manage an existing volume by UID, when it
        already mapped to a host, but the ref specifies that this is OK.
        We verify that the backend volume was renamed to have the name of the
        Cinder volume that we asked for it to be associated with.
        """
        # Create a volume as a way of getting a vdisk created, and find out the
        # UUID of that vdisk.
        volume, uid = self._create_volume_and_return_uid('manage_test')

        # Map a host to the disk
        conn = {'initiator': u'unicode:initiator3',
                'ip': '10.10.10.12',
                'host': u'unicode.foo}.bar}.baz'}
        self.driver.initialize_connection(volume, conn)

        # Descriptor of the Cinder volume that we want to own the vdisk
        # referenced by uid.
        new_volume = self._generate_vol_info()

        # Submit the request to manage it, specifying that it is OK to
        # manage a volume that is already attached.
        ref = {'source-id': uid, 'manage_if_in_use': True}
        size = self.driver.manage_existing_get_size(new_volume, ref)
        self.assertEqual(10, size)
        self.driver.manage_existing(new_volume, ref)

        # Assert that there is a disk named after the new volume that has the
        # ID that we passed in, indicating that the disk has been renamed.
        uid_of_new_volume = self._get_vdisk_uid(new_volume['name'])
        self.assertEqual(uid, uid_of_new_volume)

    def test_manage_existing_good_name_mapped_with_override(self):
        """Tests managing a mapped volume with override.

        This test case attempts to manage an existing volume by name, when it
        already mapped to a host, but the ref specifies that this is OK.
        We verify that the backend volume was renamed to have the name of the
        Cinder volume that we asked for it to be associated with.
        """
        # Create a volume as a way of getting a vdisk created, and find out the
        # UUID of that vdisk.
        volume, uid = self._create_volume_and_return_uid('manage_test')

        # Map a host to the disk
        conn = {'initiator': u'unicode:initiator3',
                'ip': '10.10.10.12',
                'host': u'unicode.foo}.bar}.baz'}
        self.driver.initialize_connection(volume, conn)

        # Descriptor of the Cinder volume that we want to own the vdisk
        # referenced by uid.
        new_volume = self._generate_vol_info()

        # Submit the request to manage it, specifying that it is OK to
        # manage a volume that is already attached.
        ref = {'source-name': volume['name'], 'manage_if_in_use': True}
        size = self.driver.manage_existing_get_size(new_volume, ref)
        self.assertEqual(10, size)
        self.driver.manage_existing(new_volume, ref)

        # Assert that there is a disk named after the new volume that has the
        # ID that we passed in, indicating that the disk has been renamed.
        uid_of_new_volume = self._get_vdisk_uid(new_volume['name'])
        self.assertEqual(uid, uid_of_new_volume)

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'mkfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_prepare_fc_map')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'startfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'stop_relationship')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_relationship')
    def test_revert_to_snapshot(self, start_relationship,
                                stop_relationship, startfcmap,
                                prepare_fc_map, mkfcmap):
        mkfcmap.side_effect = ['1']
        vol1 = self._generate_vol_info()
        snap1 = self._generate_snap_info(vol1.id)
        vol1.size = '11'
        self.assertRaises(exception.InvalidInput,
                          self.driver.revert_to_snapshot, self.ctxt,
                          vol1, snap1)
        vol2 = self._generate_vol_info()
        snap2 = self._generate_snap_info(vol2.id)
        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_get_volume_replicated_type') as vol_rep_type:
            vol_rep_type.side_effect = [False]
            self.driver.revert_to_snapshot(self.ctxt, vol2, snap2)
            mkfcmap.assert_called_once_with(snap2.name, vol2.name, True,
                                            self.driver.configuration.
                                            storwize_svc_flashcopy_rate,
                                            self.driver.configuration.
                                            storwize_svc_clean_rate)
            prepare_fc_map.assert_called_once_with(
                '1', self.driver.configuration.storwize_svc_flashcopy_timeout,
                True)
            startfcmap.assert_called_once_with('1', True)

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'mkfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_prepare_fc_map')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'startfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_relationship')
    def test_revert_to_snapshot_replication_type(self, start_relationship,
                                                 startfcmap,
                                                 prepare_fc_map, mkfcmap):
        vol1 = self._generate_vol_info()
        snap1 = self._generate_snap_info(vol1.id)
        vol1.size = '11'
        self.assertRaises(exception.InvalidInput,
                          self.driver.revert_to_snapshot, self.ctxt,
                          vol1, snap1)
        vol2 = self._generate_vol_info()
        snap2 = self._generate_snap_info(vol2.id)
        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_get_volume_replicated_type') as vol_rep_type:
            vol_rep_type.side_effect = [True]
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.revert_to_snapshot, self.ctxt,
                              vol2, snap2)
            mkfcmap.assert_not_called()
            prepare_fc_map.assert_not_called()
            startfcmap.assert_not_called()
            with mock.patch.object(
                    storwize_svc_common.StorwizeHelpers,
                    'get_rccg_name_by_volume_name') as rccg_name:
                vol_rep_type.side_effect = [True]
                rccg_name.side_effect = ["fake_rccg-1"]
                self.assertRaises(exception.VolumeBackendAPIException,
                                  self.driver.revert_to_snapshot, self.ctxt,
                                  vol2, snap2)
                mkfcmap.assert_not_called()
                prepare_fc_map.assert_not_called()
                startfcmap.assert_not_called()

    def test_storwize_create_volume_with_group_id(self):
        """Tests creating volume with gorup_id."""

        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        cg_spec = {'consistent_group_snapshot_enabled': '<is> True'}
        rccg_spec = {'consistent_group_replication_enabled': '<is> True'}
        cg_type_ref = group_types.create(self.ctxt, 'cg_type_1', cg_spec)
        rccg_type_ref = group_types.create(self.ctxt, 'rccg_type_2', rccg_spec)

        group1 = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                          group_type_id=rccg_type_ref.id)

        group2 = self._create_group_in_db(volume_type_ids=[type_ref.id],
                                          group_type_id=cg_type_ref.id)

        # Create volume with replication group id will be success
        vol1 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       host='openstack@svc#openstack',
                                       group_id=group1.id)
        self.driver.create_volume(vol1)
        # Create volume with cg_snapshot group id will success.
        vol2 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       host='openstack@svc#openstack',
                                       group_id=group2.id)
        self.driver.create_volume(vol2)

        # Create cloned volume with replication group id will be success
        vol3 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       host='openstack@svc#openstack',
                                       group_id=group1.id,
                                       source_volid=vol2.id)

        self.driver.create_cloned_volume(vol3, vol2)
        # Create cloned volume with cg_snapshot group id will be success.
        vol4 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       group_id=group2.id,
                                       host='openstack@svc#openstack',
                                       source_volid=vol2.id)
        self.driver.create_cloned_volume(vol4, vol2)

        snapshot = self._generate_snap_info(vol2.id)
        self.driver.create_snapshot(snapshot)
        # Create volume from snapshot with replication group id will be success
        vol5 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       host='openstack@svc#openstack',
                                       group_id=group1.id,
                                       snapshot_id=snapshot.id)
        self.driver.create_volume_from_snapshot(vol5, snapshot)
        # Create volume from snapshot with cg_snapshot group id will success.
        vol6 = testutils.create_volume(self.ctxt, volume_type_id=type_ref.id,
                                       group_id=group2.id,
                                       host='openstack@svc#openstack',
                                       snapshot_id=snapshot.id)
        self.driver.create_volume_from_snapshot(vol6, snapshot)

    @ddt.data({'pool': 'openstack2', 'peer_pool': 'openstack3'},
              {'pool': 'openstack', 'peer_pool': None},
              {'pool': None, 'peer_pool': 'openstack1'})
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsmdiskgrp')
    def test_storwize_svc_get_hyperswap_pool_io_grp(self, pools, lsmdiskgrp):
        lsmdiskgrp.side_effect = [{'site_id': '1'},
                                  {'site_id': '2'}]

        if pools['pool'] and pools['peer_pool']:
            iogrp_list = self.driver._helpers.get_hyperswap_pool_io_grp(
                self.driver._state, pools['pool'], pools['peer_pool'])
            lsmdiskgrp.assert_called()
            self.assertEqual(2, lsmdiskgrp.call_count)
            self.assertEqual(['0', '1'], iogrp_list)
        else:
            self.assertRaises(exception.InvalidInput,
                              self.driver._helpers.get_hyperswap_pool_io_grp,
                              self.driver._state, pools['pool'],
                              pools['peer_pool'])
            lsmdiskgrp.assert_not_called()
            self.assertEqual(0, lsmdiskgrp.call_count)

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsmdiskgrp')
    def test_storwize_svc_select_iogrp_with_pool_site(self, lsmdiskgrp):
        opts = {}
        state = self.driver._state
        lsmdiskgrp.side_effect = [{'site_id': ''},
                                  {'site_id': '1'},
                                  {'site_id': '2'},
                                  {'site_id': '2'}]
        state['storage_nodes']['1']['site_id'] = '1'
        state['storage_nodes']['1']['IO_group'] = '0'
        state['storage_nodes']['2']['site_id'] = '1'
        state['storage_nodes']['2']['IO_group'] = '1'

        pool = 'openstack2'
        opts['iogrp'] = '0,1'
        opts['volume_topology'] = 'hyperswap'
        state['available_iogrps'] = [0, 1, 2, 3]
        iog = self.driver._helpers.select_io_group(state, opts, pool)
        self.assertEqual(0, iog)

        pool = 'openstack2'
        opts['iogrp'] = '0,1'
        state['available_iogrps'] = [0, 1, 2, 3]
        iog = self.driver._helpers.select_io_group(state, opts, pool)
        self.assertEqual(0, iog)

        pool = 'openstack3'
        opts['iogrp'] = '0,1'
        state['available_iogrps'] = [0, 1, 2, 3]
        iog = self.driver._helpers.select_io_group(state, opts, pool)
        self.assertEqual(0, iog)
        state['storage_nodes']['2']['site_id'] = '2'

        pool = 'openstack2'
        opts['iogrp'] = '0,1'
        state['available_iogrps'] = [0, 1, 2, 3]
        iog = self.driver._helpers.select_io_group(state, opts, pool)
        self.assertEqual(1, iog)

    # test hyperswap volume
    def test_create_hyperswap_volume(self):
        # create hyperswap volume on code_level less than 7.7.0.0
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'openstack1'}
        invalid_release_type = self._create_volume_type(
            spec, 'invalid_release_type')
        vol = self._generate_vol_info(invalid_release_type)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        # create hyperswap on svc topology not 'hyperswap'
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'standard',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'openstack1'}
        invalid_topo_type = self._create_volume_type(
            spec, 'invalid_topo_type')
        vol = self._generate_vol_info(invalid_topo_type)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        # create hyperswap volume vith invalid pool
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'invalid_pool'}
        invalid_pool_type = self._create_volume_type(spec,
                                                     'invalid_pool_type')
        vol = self._generate_vol_info(invalid_pool_type)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        # create hyperswap volume vith easytier off
        spec = {'drivers:volume_topology': 'hyperswap',
                'drivers:easytier': False}
        easytier_type = self._create_volume_type(spec,
                                                 'easytier_type')
        vol = self._generate_vol_info(easytier_type)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_volume, vol)

        # create hyperswap volume without peer_pool
        spec = {'drivers:volume_topology': 'hyperswap'}
        no_peerpool_type = self._create_volume_type(spec,
                                                    'no_peerpool_type')
        vol = self._generate_vol_info(no_peerpool_type)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        # Create hyperswap volume, there is no site_id on peer_pool
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'openstack'}
        same_pool_type = self._create_volume_type(spec,
                                                  'same_pool_type')
        vol = self._generate_vol_info(same_pool_type)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        # Create hyperswap volume, pool and peer pool are on the same site
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'hyperswap1'}
        same_site_type = self._create_volume_type(spec,
                                                  'same_site_type')
        vol = testutils.create_volume(self.ctxt,
                                      host='openstack@svc#hyperswap1',
                                      volume_type_id=same_site_type.id)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        # create hyperswap volume with strech cluster
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'openstack1',
                'mirror_pool': 'openstack1'}
        invalid_vol_type = self._create_volume_type(spec,
                                                    'invalid_hyperswap_type')
        vol = self._generate_vol_info(invalid_vol_type)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        # create hyperswap volume with replication
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'openstack1',
                'replication_enabled': '<is> True',
                'replication_type': '<in> metro'}
        invalid_vol_type = self._create_volume_type(spec,
                                                    'invalid_hyperswap_type_2')
        vol = self._generate_vol_info(invalid_vol_type)
        self.assertRaises(exception.InvalidInput,
                          self.driver.create_volume, vol)

        hyper_type = self._create_hyperswap_type('test_hyperswap_type')
        vol = self._create_hyperswap_volume(hyper_type)
        self._assert_vol_exists(vol.name, True)
        self._assert_vol_exists('site2' + vol.name, True)
        self._assert_vol_exists('fcsite1' + vol.name, True)
        self._assert_vol_exists('fcsite2' + vol.name, True)
        self.driver.delete_volume(vol)
        self._assert_vol_exists(vol.name, False)
        self._assert_vol_exists('site2' + vol.name, False)
        self._assert_vol_exists('fcsite1' + vol.name, False)
        self._assert_vol_exists('fcsite2' + vol.name, False)

        # Validate that _update_replication_properties is being called on
        # Hyperswap volume creation
        with mock.patch.object(
                storwize_svc_common.StorwizeSVCCommonDriver,
                '_update_replication_properties') as update_rep_properties:
            vol = self._create_hyperswap_volume(hyper_type)
            self.assertEqual(fields.VolumeStatus.AVAILABLE, vol['status'])
            self.assertTrue(update_rep_properties.called)
            self.driver.delete_volume(vol)

        # Validate that _update_replication_properties is handling
        # the exception from get_relationship_info call on Hyperswap volume
        # creation by raising an exception
        with mock.patch.object(
                storwize_svc_common.StorwizeHelpers,
                'get_relationship_info') as get_relationship_info:
            get_relationship_info.side_effect = [
                exception.VolumeBackendAPIException]
            volume = self._generate_hyperswap_vol_info(hyper_type)
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_volume, volume)
            self.assertTrue(get_relationship_info.called)
            self.assertEqual(3, get_relationship_info.call_count)
            self.driver.delete_volume(volume)

    def test_create_snapshot_to_hyperswap_volume(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        hyper_type = self._create_hyperswap_type('test_hyperswap_type')
        vol = self._create_hyperswap_volume(hyper_type)
        self._assert_vol_exists(vol.name, True)

        snap = testutils.create_snapshot(self.ctxt, vol.id)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
            self.sim.error_injection('startfcmap', 'bad_id')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_snapshot, snap)
            self._assert_vol_exists(snap['name'], False)
            self.sim.error_injection('prestartfcmap', 'bad_id')
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.create_snapshot, snap)
            self._assert_vol_exists(snap['name'], False)

        self.driver.create_snapshot(snap)
        self._assert_vol_exists(snap['name'], True)

        self.driver.delete_volume(vol)
        self._assert_vol_exists(vol.name, False)
        self.driver.delete_snapshot(snap)
        self._assert_vol_exists(snap['name'], False)

    def test_create_hyperswap_volume_from_snapshot(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        hyper_type = self._create_hyperswap_type('test_hyperswap_type')
        vol = self._create_hyperswap_volume(hyper_type)
        self._assert_vol_exists(vol.name, True)

        snap = testutils.create_snapshot(self.ctxt, vol.id)
        self.driver.create_snapshot(snap)
        self._assert_vol_exists(snap['name'], True)

        vol1 = testutils.create_volume(self.ctxt,
                                       host='openstack@svc#hyperswap1',
                                       volume_type_id=hyper_type.id)

        self.driver.create_volume_from_snapshot(vol1, snap)
        self._assert_vol_exists(vol1.name, True)
        self._assert_vol_exists('site2' + vol1.name, True)
        self._assert_vol_exists('fcsite1' + vol1.name, True)
        self._assert_vol_exists('fcsite2' + vol1.name, True)

        vol2 = testutils.create_volume(self.ctxt,
                                       host='openstack@svc#hyperswap1',
                                       volume_type_id=hyper_type.id)
        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'convert_volume_to_hyperswap')) as convert_volume_to_hyperswap, \
             (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'ensure_vdisk_no_fc_mappings')) as ensure_vdisk_no_fc_mappings:
            self.driver.create_volume_from_snapshot(vol2, snap)
            ensure_vdisk_no_fc_mappings.assert_called()
            convert_volume_to_hyperswap.assert_called()
            self.assertEqual(1, convert_volume_to_hyperswap.call_count)
            self.assertEqual(1, ensure_vdisk_no_fc_mappings.call_count)
            self._assert_vol_exists(vol2.name, True)

        self.driver.delete_volume(vol)
        self._assert_vol_exists(vol.name, False)
        self._assert_vol_exists('site2' + vol.name, False)
        self._assert_vol_exists('fcsite1' + vol.name, False)
        self._assert_vol_exists('fcsite2' + vol.name, False)
        self.driver.delete_snapshot(snap)
        self._assert_vol_exists(snap['name'], False)
        self.driver.delete_volume(vol1)
        self._assert_vol_exists(vol1.name, False)

    def test_create_cloned_hyperswap_volume(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        hyper_type = self._create_hyperswap_type('test_hyperswap_type')
        vol = self._create_hyperswap_volume(hyper_type)
        self._assert_vol_exists(vol.name, True)

        vol2 = testutils.create_volume(self.ctxt,
                                       host='openstack@svc#hyperswap1',
                                       volume_type_id=vol.volume_type_id)
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_vdisk_attributes') as vdisk_attr:
            vdisk_attr.return_value = None
            self.assertRaises(exception.VolumeDriverException,
                              self.driver.create_cloned_volume,
                              vol2, vol)
        self.driver.create_cloned_volume(vol2, vol)
        self._assert_vol_exists(vol2.name, True)
        self._assert_vol_exists('site2' + vol2.name, True)
        self._assert_vol_exists('fcsite1' + vol2.name, True)
        self._assert_vol_exists('fcsite2' + vol2.name, True)

        self.driver.delete_volume(vol)
        self._assert_vol_exists(vol.name, False)
        self.driver.delete_volume(vol2)
        self._assert_vol_exists(vol2.name, False)

    def test_extend_hyperswap_volume(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)
        vol_type_ref = self._create_hyperswap_type('test_hyperswap_type')
        vol = self._create_hyperswap_volume(vol_type_ref)
        self._assert_vol_exists(vol.name, True)

        self.driver.extend_volume(vol, '13')
        attrs = self.driver._helpers.get_vdisk_attributes(vol['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 13)
        self.driver.delete_volume(vol)

        # Extend hyperswap volume that added to group.
        group_specs = {'hyperswap_group_enabled': '<is> True'}
        group_type_ref = group_types.create(self.ctxt, 'testgroup',
                                            group_specs)
        hyper_group = testutils.create_group(
            self.ctxt, name='hypergroup',
            group_type_id=group_type_ref['id'],
            volume_type_ids=[vol_type_ref['id']])
        model_update = self.driver.create_group(self.ctxt, hyper_group)
        self.assertEqual(fields.GroupStatus.AVAILABLE, model_update['status'])

        vol = self._create_hyperswap_volume(vol_type_ref)
        self.db.volume_update(context.get_admin_context(), vol['id'],
                              {'group_id': hyper_group.id})
        add_volumes = [vol]
        del_volumes = []
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                           hyper_group,
                                                           add_volumes,
                                                           del_volumes)
        self.assertEqual(fields.GroupStatus.AVAILABLE, model_update['status'])
        self.assertEqual([{'id': vol.id, 'group_id': hyper_group.id}],
                         add_volumes_update)
        self.assertEqual([], remove_volumes_update)

        self.driver.extend_volume(vol, '15')
        attrs = self.driver._helpers.get_vdisk_attributes(vol['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 15)
        self.driver.delete_volume(vol)

        # Extend hyperswap volume with thick_provisioning_support.
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'hyperswap2',
                'drivers:rsize': -1}
        hs_thick_type = volume_types.create(
            self.ctxt, 'test_hyperswap_thick_type', spec)
        hs_vol = self._create_hyperswap_volume(hs_thick_type)
        self._assert_vol_exists(hs_vol.name, True)
        if self.USESIM:
            # tell expandvdisksize to fail while called extend_volume
            # because volume is fast formatting
            self.sim.error_injection('expandvdisksize', 'fast_formatting')
            self.assertRaises(exception.VolumeDriverException,
                              self.driver.extend_volume, hs_vol, 15)
            attrs = self.driver._helpers.get_vdisk_attributes(hs_vol['name'])
            vol_size = int(attrs['capacity']) / units.Gi
            self.assertAlmostEqual(vol_size, 1)
        self.driver.delete_volume(hs_vol)

    def test_migrate_hyperswap_volume(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        hyper_type = self._create_hyperswap_type('test_hyperswap_type')
        vol = self._create_hyperswap_volume(hyper_type)
        self._assert_vol_exists(vol.name, True)

        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack2')
        cap = {'location_info': loc, 'extent_size': '256'}
        host = {'host': 'openstack@svc#openstack2', 'capabilities': cap}
        ctxt = context.get_admin_context()
        self.assertRaises(exception.InvalidInput,
                          self.driver.migrate_volume, ctxt, vol, host)
        self._delete_volume(vol)

    def test_manage_existing_hyperswap_volume(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        hyperswap_vol_type = self._create_hyperswap_type('test_hyperswap_type')
        hyper_volume = self._create_hyperswap_volume(hyperswap_vol_type)
        self._assert_vol_exists(hyper_volume.name, True)

        spec1 = {}
        non_hyper_type = self._create_volume_type(spec1, 'non_hyper_type')
        non_hyper_volume = self._create_volume()

        # test volume is hyperswap volume but volume type is non-hyper type
        new_volume = self._generate_vol_info()

        ref = {'source-name': hyper_volume['name']}
        new_volume['volume_type_id'] = non_hyper_type['id']
        new_volume['volume_type'] = non_hyper_type
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        # test volume is non hyperswap volume but volum type is hyper type
        ref = {'source-name': non_hyper_volume['name']}
        new_volume['volume_type_id'] = hyperswap_vol_type['id']
        new_volume['volume_type'] = hyperswap_vol_type
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        # Test hyperswap volume peer_pool and backend peer_pool does not match
        new_volume = testutils.create_volume(self.ctxt,
                                             host='openstack@svc#hyperswap1')
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'hyperswap1'}
        hyper_type_2 = self._create_volume_type(spec, 'hyper_type_2')
        ref = {'source-name': hyper_volume['name']}
        new_volume['volume_type_id'] = hyper_type_2['id']
        new_volume['volume_type'] = hyper_type_2
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        # test volume type match
        uid_of_master = self._get_vdisk_uid(hyper_volume.name)

        new_volume = testutils.create_volume(self.ctxt,
                                             host='openstack@svc#hyperswap1')
        ref = {'source-name': hyper_volume['name']}
        new_volume['volume_type_id'] = hyperswap_vol_type['id']
        new_volume['volume_type'] = hyperswap_vol_type
        self.driver.manage_existing(new_volume, ref)

    # Check the uid of the volume which has been renamed.
        uid_of_master_volume = self._get_vdisk_uid(new_volume['name'])
        self.assertEqual(uid_of_master, uid_of_master_volume)

        self.driver.delete_volume(hyper_volume)

    def test_retype_hyperswap_volume(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        hyperswap_vol_type = self._create_hyperswap_type('test_hyperswap_type')

        spec1 = {'drivers:iogrp': '0,1'}
        non_hyper_type = self._create_volume_type(spec1, 'non_hyper_type')

        volume = testutils.create_volume(self.ctxt,
                                         volume_type_id=non_hyper_type.id,
                                         host='openstack@svc#hyperswap1')
        self.driver.create_volume(volume)
        host = {'host': 'openstack@svc#hyperswap1'}

        # Retype from non hyperswap volume type to
        # hyperswap volume type without peer_pool
        spec = {'drivers:volume_topology': 'hyperswap'}
        hyper_type_no_peer = self._create_volume_type(spec,
                                                      'hypertypenopeer')
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, non_hyper_type['id'], hyper_type_no_peer['id'])
        self.assertRaises(exception.InvalidInput, self.driver.retype,
                          self.ctxt, volume, hyper_type_no_peer, diff, host)

        spec = {'drivers:volume_topology': 'hyperswap',
                'drivers:easytier': False}
        easytier_type = self._create_volume_type(spec,
                                                 'easytier_type')
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, non_hyper_type['id'], easytier_type['id'])
        self.assertRaises(exception.InvalidInput, self.driver.retype,
                          self.ctxt, volume, easytier_type, diff, host)

        # retype from normal volume with snapshot to hyperswap volume
        snap = testutils.create_snapshot(self.ctxt, volume.id)
        self.driver.create_snapshot(snap)
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, non_hyper_type['id'], hyperswap_vol_type['id'])
        self.assertRaises(exception.InvalidInput, self.driver.retype,
                          self.ctxt, volume, hyperswap_vol_type,
                          diff, host)
        self.driver.delete_snapshot(snap)

        # Retype from non-hyperswap volume to hyperswap volume
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, non_hyper_type['id'], hyperswap_vol_type['id'])
        self.driver.retype(
            self.ctxt, volume, hyperswap_vol_type, diff, host)
        volume['volume_type_id'] = hyperswap_vol_type['id']
        volume['volume_type'] = hyperswap_vol_type
        self._assert_vol_exists(volume.name, True)
        self._assert_vol_exists('site2' + volume.name, True)
        self._assert_vol_exists('fcsite1' + volume.name, True)
        self._assert_vol_exists('fcsite2' + volume.name, True)

        # Retype from hyperswap volume to non hyperswap volume---move site2
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, hyperswap_vol_type['id'], non_hyper_type['id'])
        self.driver.retype(
            self.ctxt, volume, non_hyper_type, diff, host)
        volume['volume_type_id'] = non_hyper_type['id']
        volume['volume_type'] = non_hyper_type
        self.driver.delete_volume(volume)

        # Retype from hyperswap volume to non hyperswap volume---move site1
        host2 = {'host': 'openstack@svc#hyperswap2'}
        volume = self._create_hyperswap_volume(hyperswap_vol_type)
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, hyperswap_vol_type['id'], non_hyper_type['id'])
        self.driver.retype(
            self.ctxt, volume, non_hyper_type, diff, host2)
        volume['volume_type_id'] = non_hyper_type['id']
        volume['volume_type'] = non_hyper_type
        self.driver.delete_volume(volume)

        # Retype a hyperswap volume to hyperswap volume with keys change
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'hyperswap2',
                'drivers:warning': '50'}
        warning_type = self._create_volume_type(spec,
                                                'warning_type')
        volume = self._create_hyperswap_volume(hyperswap_vol_type)
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, hyperswap_vol_type['id'], warning_type['id'])
        self.driver.retype(self.ctxt, volume, warning_type, diff, host)

    def test_storwize_update_replication_properties_on_retype_hyperswap_volume(
            self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        hyperswap_vol_type = self._create_hyperswap_type('test_hyperswap_type')

        spec1 = {'drivers:iogrp': '0,1'}
        non_hyper_type = self._create_volume_type(spec1, 'non_hyper_type')

        volume1 = testutils.create_volume(self.ctxt,
                                          volume_type_id=non_hyper_type.id,
                                          host='openstack@svc#hyperswap1')
        self.driver.create_volume(volume1)
        volume2 = testutils.create_volume(self.ctxt,
                                          volume_type_id=non_hyper_type.id,
                                          host='openstack@svc#hyperswap1')
        self.driver.create_volume(volume2)
        volume3 = self._create_hyperswap_volume(hyperswap_vol_type)
        host = {'host': 'openstack@svc#hyperswap1'}

        # Validate that _update_replication_properties is handling
        # the exception from get_relationship_info call while retyping a
        # normal volume to Hyperswap volume by raising an exception
        with mock.patch.object(
                storwize_svc_common.StorwizeHelpers,
                'get_relationship_info') as get_relationship_info:
            get_relationship_info.side_effect = [
                exception.VolumeBackendAPIException]
            diff, _equal = volume_types.volume_types_diff(
                self.ctxt, non_hyper_type['id'], hyperswap_vol_type['id'])
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.retype, self.ctxt, volume1,
                              hyperswap_vol_type, diff, host)
            self.assertTrue(get_relationship_info.called)
            self.assertEqual(3, get_relationship_info.call_count)
            volume1['volume_type_id'] = hyperswap_vol_type['id']
            volume1['volume_type'] = hyperswap_vol_type
            self.driver.delete_volume(volume1)

        # Validate that _update_replication_properties is being called while
        # retyping a normal volume to Hyperswap volume
        with mock.patch.object(
                storwize_svc_common.StorwizeSVCCommonDriver,
                '_update_replication_properties') as update_rep_properties:
            diff, _equal = volume_types.volume_types_diff(
                self.ctxt, non_hyper_type['id'], hyperswap_vol_type['id'])
            self.driver.retype(
                self.ctxt, volume2, hyperswap_vol_type, diff, host)
            volume2['volume_type_id'] = hyperswap_vol_type['id']
            volume2['volume_type'] = hyperswap_vol_type
            self._assert_vol_exists(volume2.name, True)
            self._assert_vol_exists('site2' + volume2.name, True)
            self._assert_vol_exists('fcsite1' + volume2.name, True)
            self._assert_vol_exists('fcsite2' + volume2.name, True)
            self.assertTrue(update_rep_properties.called)

        # Validate that _update_replication_properties is being called while
        # retyping a Hyperswap volume to normal volume
        with mock.patch.object(
                storwize_svc_common.StorwizeSVCCommonDriver,
                '_update_replication_properties') as update_rep_properties:
            diff, _equal = volume_types.volume_types_diff(
                self.ctxt, hyperswap_vol_type['id'], non_hyper_type['id'])
            self.driver.retype(
                self.ctxt, volume2, non_hyper_type, diff, host)
            volume2['volume_type_id'] = non_hyper_type['id']
            volume2['volume_type'] = non_hyper_type
            self.assertTrue(update_rep_properties.called)
            self.driver.delete_volume(volume2)

        # Validate that _update_replication_properties is handling
        # the exception from get_relationship_info call while retyping a
        # Hyperswap volume to normal volume by raising an exception
        with mock.patch.object(
                storwize_svc_common.StorwizeHelpers,
                'get_relationship_info') as get_relationship_info:
            get_relationship_info.side_effect = [
                exception.VolumeBackendAPIException]
            diff, _equal = volume_types.volume_types_diff(
                self.ctxt, hyperswap_vol_type['id'], non_hyper_type['id'])
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver.retype,
                              self.ctxt, volume3, non_hyper_type, diff, host)
            self.assertTrue(get_relationship_info.called)
            self.assertEqual(3, get_relationship_info.call_count)
            volume3['volume_type_id'] = non_hyper_type['id']
            volume3['volume_type'] = non_hyper_type
            self.driver.delete_volume(volume3)

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_update_replication_properties')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_hyperswap_pool_io_grp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_system_info')
    def test_retype_hyperswap_inuse_volume_fc(self,
                                              get_system_info,
                                              get_hyperswap_pool_io_grp,
                                              update_rep_prop):
        get_system_info.return_value = {'code_level': (7, 7, 0, 0),
                                        'topology': 'hyperswap',
                                        'system_name': 'storwize-svc-sim',
                                        'system_id': '0123456789ABCDEF'}

        self.fcdriver.do_setup(None)

        spec1 = {'drivers:iogrp': '0,1'}
        non_hyper_type = self._create_volume_type(spec1, 'non_hyper_type')

        volume = testutils.create_volume(self.ctxt,
                                         volume_type_id=non_hyper_type.id,
                                         host='openstack@svc#hyperswap1')
        self.fcdriver.create_volume(volume)
        volume.previous_status = 'in-use'
        host = {'host': 'openstack@svc#hyperswap1'}

        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'hyperswap2'}

        hyper_type = self._create_volume_type(spec, 'hypertype')
        diff, _equal = volume_types.volume_types_diff(self.ctxt,
                                                      non_hyper_type['id'],
                                                      hyper_type['id'])

        self.fcdriver.retype(self.ctxt, volume, hyper_type, diff, host)

        self._assert_vol_exists(volume.name, True)
        self._assert_vol_exists('site2' + volume.name, True)
        self._assert_vol_exists('fcsite1' + volume.name, True)
        self._assert_vol_exists('fcsite2' + volume.name, True)

        get_hyperswap_pool_io_grp.assert_called()

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_hyperswap_pool_io_grp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_system_info')
    def test_retype_hyperswap_inuse_volume_iscsi(self,
                                                 get_system_info,
                                                 get_hyperswap_pool_io_grp):
        get_system_info.return_value = {'code_level': (7, 7, 0, 0),
                                        'topology': 'hyperswap',
                                        'system_name': 'storwize-svc-sim',
                                        'system_id': '0123456789ABCDEF'}

        self.driver.do_setup(None)

        spec1 = {'drivers:iogrp': '0,1'}
        non_hyper_type = self._create_volume_type(spec1, 'non_hyper_type')

        volume = testutils.create_volume(self.ctxt,
                                         volume_type_id=non_hyper_type.id,
                                         host='openstack@svc#hyperswap1')
        self.driver.create_volume(volume)
        volume.previous_status = 'in-use'
        host = {'host': 'openstack@svc#hyperswap1'}

        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'hyperswap2'}

        hyper_type = self._create_volume_type(spec, 'hypertype')
        diff, _equal = volume_types.volume_types_diff(self.ctxt,
                                                      non_hyper_type['id'],
                                                      hyper_type['id'])

        self.driver.retype(self.ctxt, volume, hyper_type, diff, host)

        self._assert_vol_exists(volume.name, True)
        self._assert_vol_exists('site2' + volume.name, True)
        self._assert_vol_exists('fcsite1' + volume.name, True)
        self._assert_vol_exists('fcsite2' + volume.name, True)

        get_hyperswap_pool_io_grp.assert_not_called()

    def test_retype_hyperswap_volume_failure_case(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        hyperswap_vol_type = self._create_hyperswap_type('test_hyperswap_type')
        host = {'host': 'openstack@svc#hyperswap1'}
        # Retype a hyperswap volume to hyperswap volume with peer_pool changes
        spec = {'drivers:volume_topology': 'hyperswap'}
        peer_type = self._create_volume_type(spec,
                                             'peer_type')
        volume = self._create_hyperswap_volume(hyperswap_vol_type)
        self._assert_vol_exists(volume.name, True)
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, hyperswap_vol_type['id'], peer_type['id'])
        self.assertRaises(exception.InvalidInput,
                          self.driver.retype,
                          self.ctxt, volume, peer_type, diff,
                          host)

        # Retype a hyperswap volume to hyperswap volume with iogrp changes
        spec = {'drivers:volume_topology': 'hyperswap',
                'drivers:iogrp': '1'}
        hyperswap_vol_type_2 = self._create_volume_type(spec,
                                                        'hyperswap_type_2')
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'select_io_group') as select_io_group:
            select_io_group.return_value = {1}
            diff, _equal = volume_types.volume_types_diff(
                self.ctxt, hyperswap_vol_type['id'],
                hyperswap_vol_type_2['id'])

            self.assertRaises(exception.InvalidInput,
                              self.driver.retype,
                              self.ctxt, volume, hyperswap_vol_type_2, diff,
                              host)

        host2 = {'host': 'openstack@svc#hyperswap2'}
        # Retype a hyperswap volume to hyperswap volume with pool change
        spec = {'drivers:volume_topology': 'hyperswap',
                'drivers:iogrp': '0,1'}
        hyperswap_type_3 = self._create_volume_type(spec,
                                                    'hyperswap_type_3')
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, hyperswap_vol_type['id'], hyperswap_type_3['id'])
        self.assertRaises(exception.InvalidInput, self.driver.retype,
                          self.ctxt, volume, hyperswap_type_3, diff, host2)

        # Retype a hyperswap volume in-use
        inuse_type = self._create_hyperswap_type('in-use_type')
        volume.previous_status = 'in-use'
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, hyperswap_vol_type['id'],
            inuse_type['id'])
        self.driver.retype(self.ctxt, volume, inuse_type, diff, host)

        # retype from hyperswap volume to replication volume
        spec3 = {'replication_enabled': '<is> True',
                 'replication_type': '<in> metro'}
        self.driver._replica_target['pool_name'] = 'openstack2'
        replication_type = self._create_volume_type(spec3,
                                                    'test_replication_type')
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, hyperswap_vol_type['id'], replication_type['id'])
        self.assertRaises(exception.InvalidInput, self.driver.retype,
                          self.ctxt, volume, replication_type, diff, host)

        # retype from hyperswap volume to streched cluster volume
        spec4 = {'mirror_pool': 'openstack1'}
        mirror_type = self._create_volume_type(spec4,
                                               'test_mirror_type')
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, hyperswap_vol_type['id'], mirror_type['id'])
        self.assertRaises(exception.InvalidInput, self.driver.retype,
                          self.ctxt, volume, mirror_type, diff, host)

        # retype from streched cluster volume to hyperswap volume
        host3 = {'host': 'openstack@svc#openstack'}
        mirror_volume = self._create_volume(volume_type_id=mirror_type.id)
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, mirror_type['id'], hyperswap_vol_type['id'])
        self.assertRaises(exception.InvalidInput, self.driver.retype,
                          self.ctxt, mirror_volume, hyperswap_vol_type, diff,
                          host3)

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_get_rccg_name')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'create_rccg')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_storwize_hyperswap_group_create(self, is_grp_a_cg_snapshot_type,
                                             create_rccg, get_rccg_name):
        """Test group create."""
        is_grp_a_cg_snapshot_type.side_effect = [False, False, False, False]
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        vol_type_ref = volume_types.create(self.ctxt, 'nonhypertype', None)
        group_specs = {'hyperswap_group_enabled': '<is> True'}
        group_type_ref = group_types.create(self.ctxt, 'testgroup',
                                            group_specs)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=group_type_ref['id'],
                                       volume_type_ids=[vol_type_ref['id']])

        # create hyperswap group with nonhyper volume type
        model_update = self.driver.create_group(self.ctxt, group)
        self.assertEqual(fields.GroupStatus.ERROR,
                         model_update['status'])
        create_rccg.assert_not_called()
        get_rccg_name.assert_not_called()

        # create hyperswap group with hyper volume type.
        spec = {'drivers:volume_topology': 'hyperswap',
                'peer_pool': 'openstack1'}
        vol_type_ref = volume_types.create(self.ctxt, 'hypertype', spec)
        hyper_group = testutils.create_group(
            self.ctxt, name='hypergroup',
            group_type_id=group_type_ref['id'],
            volume_type_ids=[vol_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt, hyper_group)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        create_rccg.assert_not_called()
        get_rccg_name.assert_not_called()

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_get_rccg_name')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'delete_rccg')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_storwize_hyperswap_group_delete(self, is_grp_a_cg_snapshot_type,
                                             delete_rccg, get_rccg_name):
        """Test group create."""
        is_grp_a_cg_snapshot_type.side_effect = [False, False, False]

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        group_specs = {'hyperswap_group_enabled': '<is> True'}
        group_type_ref = group_types.create(self.ctxt, 'testgroup',
                                            group_specs)

        # create hyperswap group with hyper volume type.
        vol_type_ref = self._create_hyperswap_type(
            'hyper_type')
        hyper_group = testutils.create_group(
            self.ctxt, name='hypergroup',
            group_type_id=group_type_ref['id'],
            volume_type_ids=[vol_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt, hyper_group)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        vol1 = self._create_hyperswap_volume(vol_type_ref)
        vol2 = self._create_hyperswap_volume(vol_type_ref)
        ctxt = context.get_admin_context()
        self.db.volume_update(ctxt, vol1['id'], {'group_id': hyper_group.id})
        self.db.volume_update(ctxt, vol2['id'], {'group_id': hyper_group.id})
        volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), hyper_group.id)

        model_update = self.driver.delete_group(self.ctxt, hyper_group,
                                                volumes)
        self.assertEqual(fields.GroupStatus.DELETED,
                         model_update[0]['status'])
        for volume in model_update[1]:
            self.assertEqual('deleted', volume['status'])

        delete_rccg.assert_not_called()
        get_rccg_name.assert_not_called()

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_get_rccg_name')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_relationship_info')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'chrcrelationship')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_storwize_hyperswap_group_update(self, is_grp_a_cg_snapshot_type,
                                             chrcrelationship,
                                             get_relationship_info,
                                             get_rccg_name):
        """Test group create."""
        is_grp_a_cg_snapshot_type.side_effect = [False, False, False,
                                                 False, False]
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        group_specs = {'hyperswap_group_enabled': '<is> True'}
        group_type_ref = group_types.create(self.ctxt, 'testgroup',
                                            group_specs)

        # create hyperswap group with hyper volume type.
        volume_type_ref = self._create_hyperswap_type(
            'hyper_type')
        hyper_group = testutils.create_group(
            self.ctxt, name='hypergroup',
            group_type_id=group_type_ref['id'],
            volume_type_ids=[volume_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt, hyper_group)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        vol1 = self._create_hyperswap_volume(volume_type_ref)
        vol2 = self._create_hyperswap_volume(volume_type_ref)
        ctxt = context.get_admin_context()
        self.db.volume_update(ctxt, vol1['id'], {'group_id': hyper_group.id})
        self.db.volume_update(ctxt, vol2['id'], {'group_id': hyper_group.id})
        add_volumes = [vol1, vol2]
        del_volumes = []

        get_relationship_info.assert_called()
        self.assertEqual(2, get_relationship_info.call_count)

        # add hyperswap volume
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                           hyper_group,
                                                           add_volumes,
                                                           del_volumes)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        self.assertEqual([{'id': vol1.id, 'group_id': hyper_group.id},
                          {'id': vol2.id, 'group_id': hyper_group.id}],
                         add_volumes_update, )
        self.assertEqual([], remove_volumes_update)
        chrcrelationship.assert_not_called()
        get_relationship_info.assert_called()
        self.assertEqual(2, get_relationship_info.call_count)
        get_rccg_name.assert_not_called()

        # del hyperswap volume from volume group
        add_volumes = []
        del_volumes = [vol1, vol2]
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                           hyper_group,
                                                           add_volumes,
                                                           del_volumes)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        self.assertEqual([], add_volumes_update)
        self.assertEqual([{'id': vol1.id, 'group_id': None},
                          {'id': vol2.id, 'group_id': None}],
                         remove_volumes_update)
        chrcrelationship.assert_not_called()
        get_relationship_info.assert_called()
        self.assertEqual(2, get_relationship_info.call_count)
        get_rccg_name.assert_not_called()

        # add non-hyper volume
        non_type_ref = volume_types.create(self.ctxt, 'nonhypertype', None)
        add_vol3 = self._create_volume(volume_type_id=non_type_ref['id'])
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(
            self.ctxt, hyper_group, [add_vol3, vol1], [])
        self.assertEqual(fields.GroupStatus.ERROR,
                         model_update['status'])
        self.assertEqual([{'id': vol1.id, 'group_id': hyper_group.id}],
                         add_volumes_update)
        self.assertEqual([], remove_volumes_update)

        # del non-hyper volume
        vol4 = self._create_volume(volume_type_id=non_type_ref['id'])
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(
            self.ctxt, hyper_group, [], [vol4, vol1])
        self.assertEqual(fields.GroupStatus.ERROR,
                         model_update['status'])
        self.assertEqual([{'id': vol1.id, 'group_id': None}],
                         remove_volumes_update)
        self.assertEqual([], add_volumes_update)

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_hyperswap_create_group_from_grp(self, is_group_a_cg_snap_type):
        # Valid case for create hyperswap group from src
        is_group_a_cg_snap_type.return_value = False
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        group_specs = {'hyperswap_group_enabled': '<is> True'}
        group_type_ref = group_types.create(self.ctxt, 'testgroup',
                                            group_specs)

        # create hyperswap group with hyper volume type.
        volume_type_ref = self._create_hyperswap_type(
            'hyper_type')
        source_hyper_group = testutils.create_group(
            self.ctxt, name='src_hypergroup',
            group_type_id=group_type_ref['id'],
            volume_type_ids=[volume_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt, source_hyper_group)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        src_vol1 = self._create_hyperswap_volume(volume_type_ref)
        src_vol2 = self._create_hyperswap_volume(volume_type_ref)
        ctxt = context.get_admin_context()
        self.db.volume_update(ctxt, src_vol1['id'],
                              {'group_id': source_hyper_group.id})
        self.db.volume_update(ctxt, src_vol2['id'],
                              {'group_id': source_hyper_group.id})
        add_volumes = [src_vol1, src_vol2]
        del_volumes = []

        # add hyperswap volume
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                           source_hyper_group,
                                                           add_volumes,
                                                           del_volumes)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        self.assertEqual([{'id': src_vol1.id,
                           'group_id': source_hyper_group.id},
                          {'id': src_vol2.id,
                           'group_id': source_hyper_group.id}],
                         add_volumes_update, )
        source_vols = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), source_hyper_group['id'])

        # clone hyper group
        Clone_hyper_group = testutils.create_group(
            self.ctxt, name='clon_hypergroup',
            group_type_id=group_type_ref['id'],
            volume_type_ids=[volume_type_ref['id']])

        clone_vol1 = testutils.create_volume(
            self.ctxt, host='openstack@svc#hyperswap1',
            volume_type_id=volume_type_ref.id, group_id=Clone_hyper_group.id)
        clone_vol2 = testutils.create_volume(
            self.ctxt, host='openstack@svc#hyperswap1',
            volume_type_id=volume_type_ref.id, group_id=Clone_hyper_group.id)

        clone_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), Clone_hyper_group['id'])

        # Create hyperswap group from source hyperswap group
        model_update, volumes_model_update = (
            self.driver.create_group_from_src(self.ctxt, Clone_hyper_group,
                                              clone_volumes, None, None,
                                              source_hyper_group,
                                              source_vols))
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'],
                         "CG create from src created failed")
        for each_vol in volumes_model_update:
            self.assertEqual('available', each_vol['status'])

        for vol in clone_volumes:
            self._assert_vol_exists(vol.name, True)
            self._assert_vol_exists('site2' + vol.name, True)
            self._assert_vol_exists('fcsite1' + vol.name, True)
            self._assert_vol_exists('fcsite2' + vol.name, True)

        self.driver.delete_group(self.ctxt, Clone_hyper_group,
                                 [clone_vol1, clone_vol2])

        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'convert_volume_to_hyperswap')) as convert_volume_to_hyperswap, \
             (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'ensure_vdisk_no_fc_mappings')) as ensure_vdisk_no_fc_mappings:

            # Create cg from source cg
            model_update, volumes_model_update = (
                self.driver.create_group_from_src(self.ctxt,
                                                  Clone_hyper_group,
                                                  clone_volumes, None,
                                                  None, source_hyper_group,
                                                  source_vols))
            ensure_vdisk_no_fc_mappings.assert_called()
            self.assertEqual(2, ensure_vdisk_no_fc_mappings.call_count)
            convert_volume_to_hyperswap.assert_called()
            self.assertEqual(2, convert_volume_to_hyperswap.call_count)

        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'],
                         "CG create from src created failed")
        for each_vol in volumes_model_update:
            self.assertEqual('available', each_vol['status'])

        for vol in clone_volumes:
            self._assert_vol_exists(vol.name, True)
            self._assert_vol_exists('site2' + vol.name, False)
            self._assert_vol_exists('fcsite1' + vol.name, False)
            self._assert_vol_exists('fcsite2' + vol.name, False)

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_hyperswap_create_group_from_snapshot(self,
                                                  is_group_a_cg_snap_type):
        # Valid case for create hyperswap group from src
        is_group_a_cg_snap_type.return_value = False
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        group_specs = {'hyperswap_group_enabled': '<is> True'}
        group_type_ref = group_types.create(self.ctxt, 'testgroup',
                                            group_specs)

        # create hyperswap group with hyper volume type.
        volume_type_ref = self._create_hyperswap_type(
            'hyper_type')
        source_hyper_group = testutils.create_group(
            self.ctxt, name='src_hypergroup',
            group_type_id=group_type_ref['id'],
            volume_type_ids=[volume_type_ref['id']])

        model_update = self.driver.create_group(self.ctxt,
                                                source_hyper_group)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])

        src_vol1 = self._create_hyperswap_volume(volume_type_ref)
        src_vol2 = self._create_hyperswap_volume(volume_type_ref)
        ctxt = context.get_admin_context()
        self.db.volume_update(ctxt, src_vol1['id'],
                              {'group_id': source_hyper_group.id})
        self.db.volume_update(ctxt, src_vol2['id'],
                              {'group_id': source_hyper_group.id})
        add_volumes = [src_vol1, src_vol2]
        del_volumes = []

        # add hyperswap volume
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                           source_hyper_group,
                                                           add_volumes,
                                                           del_volumes)
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        self.assertEqual([{'id': src_vol1.id,
                           'group_id': source_hyper_group.id},
                          {'id': src_vol2.id,
                           'group_id': source_hyper_group.id}],
                         add_volumes_update, )

        # clone hyper group
        Clone_hyper_group = testutils.create_group(
            self.ctxt, name='clon_hypergroup',
            group_type_id=group_type_ref['id'],
            volume_type_ids=[volume_type_ref['id']])

        clone_vol1 = testutils.create_volume(
            self.ctxt, host='openstack@svc#hyperswap1',
            volume_type_id=volume_type_ref.id, group_id=Clone_hyper_group.id)
        clone_vol2 = testutils.create_volume(
            self.ctxt, host='openstack@svc#hyperswap1',
            volume_type_id=volume_type_ref.id, group_id=Clone_hyper_group.id)

        clone_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), Clone_hyper_group['id'])

        # Create hyperswap group snapshot
        group_snapshot, snapshots = self._create_group_snapshot(
            source_hyper_group['id'], group_type_id=group_type_ref.id)

        # Create hyperswap group from hyperswap group snapshot
        model_update, volumes_model_update = (
            self.driver.create_group_from_src(self.ctxt, Clone_hyper_group,
                                              clone_volumes, group_snapshot,
                                              snapshots, None, None))
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'],
                         "CG create from src created failed")
        for each_vol in volumes_model_update:
            self.assertEqual('available', each_vol['status'])

        for vol in clone_volumes:
            self._assert_vol_exists(vol.name, True)
            self._assert_vol_exists('site2' + vol.name, True)
            self._assert_vol_exists('fcsite1' + vol.name, True)
            self._assert_vol_exists('fcsite2' + vol.name, True)

        self.driver.delete_group(self.ctxt, Clone_hyper_group,
                                 [clone_vol1, clone_vol2])

        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'convert_volume_to_hyperswap')) as convert_volume_to_hyperswap, \
             (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'ensure_vdisk_no_fc_mappings')) as ensure_vdisk_no_fc_mappings:

            # Create cg from source cg
            model_update, volumes_model_update = (
                self.driver.create_group_from_src(self.ctxt,
                                                  Clone_hyper_group,
                                                  clone_volumes,
                                                  group_snapshot,
                                                  snapshots, None, None))
            ensure_vdisk_no_fc_mappings.assert_called()
            self.assertEqual(2, ensure_vdisk_no_fc_mappings.call_count)
            convert_volume_to_hyperswap.assert_called()
            self.assertEqual(2, convert_volume_to_hyperswap.call_count)

        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'],
                         "CG create from src created failed")
        for each_vol in volumes_model_update:
            self.assertEqual('available', each_vol['status'])

        for vol in clone_volumes:
            self._assert_vol_exists(vol.name, True)
            self._assert_vol_exists('site2' + vol.name, False)
            self._assert_vol_exists('fcsite1' + vol.name, False)
            self._assert_vol_exists('fcsite2' + vol.name, False)

    @ddt.data({'spec': {'rsize': -1}},
              {'spec': {'mirror_pool': 'dr_pool2'}},
              {'spec': {'drivers:volume_topology': 'hyperswap',
                        'peer_pool': 'dr_pool2'}})
    @ddt.unpack
    def test_storwize_volumes_on_dr_pool_success_case(self, spec):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        dr_type = self._create_volume_type(spec, 'type_dr')
        vol = testutils.create_volume(self.ctxt, volume_type_id=dr_type.id,
                                      host='openstack@svc#hyperswap1')
        self.driver.create_volume(vol)

        vol2 = testutils.create_volume(self.ctxt, volume_type_id=dr_type.id,
                                       host='openstack@svc#hyperswap1')
        ref = {'source-name': vol.name}
        self.driver.manage_existing(vol2, ref)

    @ddt.data({'spec': {'warning': 30}},
              {'spec': {'rsize': 5}},
              {'spec': {'easytier': False}},
              {'spec': {'autoexpand': False}},
              {'spec': {'grainsize': 128}})
    @ddt.unpack
    def test_storwize_create_thin_volume_on_dr_pool_failure_case(self, spec):
        # create basic thin volume on dr_pool
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        thin_dr_type = self._create_volume_type(spec, 'type_thin')
        vol = self._generate_vol_info_on_dr_pool(thin_dr_type)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_volume, vol)

        # create mirror volume on dr_pool
        self._set_flag('storwize_svc_mirror_pool', 'dr_pool1')
        mirror_dr_type = self._create_volume_type(spec, 'type_mirror')
        vol = self._generate_vol_info(mirror_dr_type)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_volume, vol)
        self._reset_flags()

        # create hyperswap volume on dr_pool
        spec.update({'drivers:volume_topology': 'hyperswap',
                     'peer_pool': 'dr_pool2'})
        hyper_dr_type = self._create_volume_type(spec, 'hyper_dr_type')
        self.assertRaises(exception.VolumeDriverException,
                          self._create_hyperswap_volume, hyper_dr_type)

    @ddt.data({'spec': {'warning': 30}},
              {'spec': {'rsize': 5}},
              {'spec': {'easytier': False}},
              {'spec': {'autoexpand': False}},
              {'spec': {'grainsize': 128}})
    @ddt.unpack
    def test_storwize_manage_volume_on_dr_pool_failure_case(self, spec):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)

        extra_spec = {}
        thin_type = self._create_volume_type(extra_spec, 'thin_type')
        vol_type1 = self._create_volume_type(spec, 'vol_type1')
        thin_volume = self._generate_vol_info_on_dr_pool(thin_type)
        self.driver.create_volume(thin_volume)
        vol1 = self._generate_vol_info_on_dr_pool(vol_type1)
        ref1 = {'source-name': thin_volume.name}
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, vol1, ref1)

        extra_spec = {'mirror_pool': 'dr_pool1'}
        mirror_type = self._create_volume_type(extra_spec, 'type_mirror')
        mirror_volume = self._generate_vol_info(mirror_type)
        self.driver.create_volume(mirror_volume)
        spec.update({'mirror_pool': 'dr_pool1'})
        vol_type2 = self._create_volume_type(spec, 'vol_type2')
        vol2 = self._generate_vol_info(vol_type2)
        ref2 = {'source-name': mirror_volume.name}
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, vol2, ref2)
        spec.pop('mirror_pool')

        extra_spec = {'drivers:volume_topology': 'hyperswap',
                      'peer_pool': 'dr_pool2'}
        hyper_type = self._create_volume_type(extra_spec, 'type_hyper')
        hyper_volume = testutils.create_volume(
            self.ctxt, volume_type_id=hyper_type.id,
            host='openstack@svc#hyperswap1')
        self.driver.create_volume(hyper_volume)
        spec.update(extra_spec)
        vol_type3 = self._create_volume_type(spec, 'vol_type3')
        vol3 = testutils.create_volume(
            self.ctxt, volume_type_id=vol_type3.id,
            host='openstack@svc#hyperswap1')
        ref3 = {'source-name': hyper_volume.name}
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, vol3, ref3)

    def test_storwize_migrate_volume_between_regular_dr_pool(self):
        spec = {'mirror_pool': 'openstack1'}
        mirror_vol_type = self._create_volume_type(spec, 'test_mirror_type')
        vol = self._generate_vol_info(mirror_vol_type)
        self.driver.create_volume(vol)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':dr_pool2')
        cap = {'location_info': loc, 'extent_size': '256'}
        host = {'host': 'openstack@svc#dr_pool2', 'capabilities': cap}
        ctxt = context.get_admin_context()
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.migrate_volume, ctxt, vol, host)

        vol2 = self._generate_vol_info_on_dr_pool(mirror_vol_type)
        self.driver.create_volume(vol2)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.migrate_volume, ctxt, vol2, host)

        spec = {'mirror_pool': 'dr_pool1'}
        mirror_vol_type1 = self._create_volume_type(spec, 'test_mirror_type1')
        vol3 = self._generate_vol_info(mirror_vol_type1)
        self.driver.create_volume(vol3)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.migrate_volume, ctxt, vol3, host)

        spec.update({'rsize': -1})
        thick_vol_type = self._create_volume_type(spec, 'thick_mirror_type')
        vol3 = self._generate_vol_info_on_dr_pool(thick_vol_type)
        self.driver.create_volume(vol3)
        self.driver.migrate_volume(ctxt, vol3, host)

        vol4 = self._create_volume()
        self.driver.migrate_volume(ctxt, vol4, host)

        spec = {'rsize': '10'}
        rsize_type = self._create_volume_type(spec, 'rsize_type')
        vol5 = self._generate_vol_info(rsize_type)
        self.driver.create_volume(vol5)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.migrate_volume, ctxt, vol5, host)

    @ddt.data(({}, {'easytier': True, 'warning': 5, 'autoexpand': False}),
              ({}, {'grainsize': 128}),
              ({'mirror_pool': 'dr_pool2'}, {'mirror_pool': 'hyperswap1'}))
    @ddt.unpack
    def test_storwize_svc_retype_old_type_dr_pool(self, key_specs_old,
                                                  key_specs_new):
        self.driver.do_setup(None)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':dr_pool1')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#dr_pool1', 'capabilities': cap}
        ctxt = context.get_admin_context()

        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)

        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])

        volume = self._generate_vol_info_on_dr_pool(old_type)
        volume['host'] = host['host']
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, ctxt, volume,
                          new_type, diff, host)

    @ddt.data(({}, {'mirror_pool': 'dr_pool2', 'warning': 5}),
              ({'mirror_pool': 'openstack2'}, {'mirror_pool': 'dr_pool2'}),
              ({'mirror_pool': 'dr_pool2'}, {'mirror_pool': 'hyperswap1'}),
              ({'autoexpand': False}, {'drivers:volume_topology': 'hyperswap',
                                       'peer_pool': 'dr_pool2',
                                       'autoexpand': False}))
    @ddt.unpack
    def test_storwize_svc_retype_new_type_dr_pool(self, key_specs_old,
                                                  key_specs_new):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 7, 0, 0),
                                'topology': 'hyperswap',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#openstack', 'capabilities': cap}
        ctxt = context.get_admin_context()

        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)

        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])

        volume = self._generate_vol_info(old_type)
        volume['host'] = host['host']
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, ctxt, volume,
                          new_type, diff, host)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_flashcopy_mapping_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    def test_revert_to_snapshot_with_uncompleted_clone(
            self,
            _get_vdisk_fc_mappings,
            _get_flashcopy_mapping_attributes):
        vol1 = self._generate_vol_info()
        snap1 = self._generate_snap_info(vol1.id)

        self.driver._helpers._get_vdisk_fc_mappings.return_value = ['4']
        self.driver._helpers._get_flashcopy_mapping_attributes.return_value = {
            'copy_rate': '50',
            'progress': '3',
            'status': 'copying',
            'target_vdisk_name': 'testvol'}

        self.assertRaises(exception.VolumeBackendAPIException,
                          self.driver.revert_to_snapshot,
                          self.ctxt,
                          vol1, snap1)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'delete_vdisk')
    def test_storwize_svc_delete_volume_with_lower_code(self, delete_vdisk):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 6, 0, 0),
                                'topology': 'standard',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            self.driver.do_setup(None)
            volume = self._generate_vol_info()
            snap = self._generate_snap_info(volume.id)
            self.driver.create_volume(volume)
            self.driver.create_snapshot(snap)
            self.driver.delete_snapshot(snap)
            snap_call = [mock.call(snap.name, force_delete=False,
                                   force_unmap=False)]
            delete_vdisk.assert_has_calls(snap_call)
            self.driver.delete_volume(volume)
            vol_call = [mock.call(volume.name, force_delete=False,
                                  force_unmap=False)]
            delete_vdisk.assert_has_calls(vol_call)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'delete_vdisk')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    @mock.patch('cinder.volume.volume_utils.is_group_a_type')
    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_delete_replication_grp')
    def test_storwize_delete_group_with_lower_code(
            self, _del_rep_grp, is_grp_a_cg_rep_type,
            is_grp_a_cg_snapshot_type, delete_vdisk):
        is_grp_a_cg_snapshot_type.return_valume = True
        is_grp_a_cg_rep_type.return_value = False
        type_ref = volume_types.create(self.ctxt, 'testtype', None)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=fake.GROUP_TYPE_ID,
                                       volume_type_ids=[type_ref['id']])

        vol1 = self._create_volume(volume_type_id=type_ref['id'],
                                   group_id=group.id)
        vol2 = self._create_volume(volume_type_id=type_ref['id'],
                                   group_id=group.id)
        volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), group.id)
        self.driver.delete_group(self.ctxt, group, volumes)
        calls = [mock.call(vol1.name, force_unmap=False, force_delete=True),
                 mock.call(vol2.name, force_unmap=False, force_delete=True)]

        delete_vdisk.assert_has_calls(calls, any_order=True)

    def test_storwize_svc_retype_between_iogrps(self):
        self.driver.do_setup(None)
        ctxt = context.get_admin_context()

        key_specs_old = {'iogrp': 0}
        key_specs_new = {'iogrp': 1}
        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)

        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])
        volume = self._generate_vol_info(old_type)
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        conn = {'initiator': u'iqn.1993-08.org.debian:01:eac5ccc1aaa',
                'ip': '10.10.10.12',
                'host': u'openstack@svc#openstack'}
        self.driver.initialize_connection(volume, conn)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack2')
        cap = {'location_info': loc, 'extent_size': '128'}
        host_name = self.driver._helpers.get_host_from_connector(
            conn, iscsi=True)
        self.assertIsNotNone(host_name)
        host = {'host': host_name, 'capabilities': cap}
        volume['host'] = host['host']

        self.driver.retype(ctxt, volume, new_type, diff, host)
        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        self.assertEqual('1', attrs['IO_group_id'], 'Volume retype '
                         'failed')

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'addvdiskaccess')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'rmvdiskaccess')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'movevdisk')
    def test_storwize_svc_retype_between_iogrps_invalid(
            self, movevdisk, rmvdiskaccess, addvdiskaccess):
        self.driver.do_setup(None)
        ctxt = context.get_admin_context()

        key_specs_old = {'iogrp': 0}
        key_specs_new = {'iogrp': 1}
        old_type_ref = volume_types.create(ctxt, 'old', key_specs_old)
        new_type_ref = volume_types.create(ctxt, 'new', key_specs_new)

        diff, _equal = volume_types.volume_types_diff(ctxt, old_type_ref['id'],
                                                      new_type_ref['id'])

        old_type = objects.VolumeType.get_by_id(ctxt,
                                                old_type_ref['id'])
        volume = self._generate_vol_info(old_type)
        new_type = objects.VolumeType.get_by_id(ctxt,
                                                new_type_ref['id'])

        self.driver.create_volume(volume)
        conn = {'initiator': u'iqn.1993-08.org.debian:01:eac5ccc1aaa',
                'ip': '10.10.10.12',
                'host': u'openstack@svc#openstack'}
        self.driver.initialize_connection(volume, conn)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack2')
        cap = {'location_info': loc, 'extent_size': '128'}
        host_name = self.driver._helpers.get_host_from_connector(
            conn, iscsi=True)
        self.assertIsNotNone(host_name)
        host = {'host': host_name, 'capabilities': cap}
        volume['host'] = host['host']
        ex = exception.VolumeBackendAPIException(data='CMMVC5879E')
        movevdisk.side_effect = ex

        self.assertRaises(exception.VolumeBackendAPIException,
                          self.driver.retype, ctxt,
                          volume, new_type, diff, host)
        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        self.assertEqual(int(key_specs_old['iogrp']),
                         int(attrs['IO_group_id']), 'Volume retype failed')
        addvdiskaccess.assert_called()
        movevdisk.assert_called()
        rmvdiskaccess.assert_called_with(
            volume['name'], str(key_specs_new['iogrp']))


class CLIResponseTestCase(test.TestCase):
    def test_empty(self):
        self.assertEqual(0, len(
            storwize_svc_common.CLIResponse('')))
        self.assertEqual(0, len(
            storwize_svc_common.CLIResponse(('', 'stderr'))))

    def test_header(self):
        raw = r'''id!name
1!node1
2!node2
'''
        resp = storwize_svc_common.CLIResponse(raw, with_header=True)
        self.assertEqual(2, len(resp))
        self.assertEqual('1', resp[0]['id'])
        self.assertEqual('2', resp[1]['id'])

    def test_select(self):
        raw = r'''id!123
name!Bill
name!Bill2
age!30
home address!s1
home address!s2

id! 7
name!John
name!John2
age!40
home address!s3
home address!s4
'''
        resp = storwize_svc_common.CLIResponse(raw, with_header=False)
        self.assertEqual([('s1', 'Bill', 's1'), ('s2', 'Bill2', 's2'),
                          ('s3', 'John', 's3'), ('s4', 'John2', 's4')],
                         list(resp.select('home address', 'name',
                                          'home address')))

    def test_lsnode_all(self):
        raw = r'''id!name!UPS_serial_number!WWNN!status
1!node1!!500507680200C744!online
2!node2!!500507680200C745!online
'''
        resp = storwize_svc_common.CLIResponse(raw)
        self.assertEqual(2, len(resp))
        self.assertEqual('1', resp[0]['id'])
        self.assertEqual('500507680200C744', resp[0]['WWNN'])
        self.assertEqual('2', resp[1]['id'])
        self.assertEqual('500507680200C745', resp[1]['WWNN'])

    def test_lsnode_single(self):
        raw = r'''id!1
port_id!500507680210C744
port_status!active
port_speed!8Gb
port_id!500507680240C744
port_status!inactive
port_speed!8Gb
'''
        resp = storwize_svc_common.CLIResponse(raw, with_header=False)
        self.assertEqual(1, len(resp))
        self.assertEqual('1', resp[0]['id'])
        self.assertEqual([('500507680210C744', 'active'),
                          ('500507680240C744', 'inactive')],
                         list(resp.select('port_id', 'port_status')))


@ddt.ddt
class StorwizeHelpersTestCase(test.TestCase):
    def setUp(self):
        super(StorwizeHelpersTestCase, self).setUp()
        self.storwize_svc_common = storwize_svc_common.StorwizeHelpers(None)
        self.mock_wait_time = mock.patch.object(
            storwize_svc_common.StorwizeHelpers, "WAIT_TIME", 0)

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lslicense')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsguicapabilities')
    def test_compression_enabled(self, lsguicapabilities, lslicense):
        fake_license_without_keys = {}
        fake_license = {
            'license_compression_enclosures': '1',
            'license_compression_capacity': '1'
        }
        fake_license_scheme = {
            'license_scheme': '9846'
        }
        fake_9100_license_scheme = {
            'license_scheme': 'flex'
        }
        fake_license_invalid_scheme = {
            'license_scheme': '0000'
        }
        lslicense.side_effect = [fake_license_without_keys,
                                 fake_license_without_keys,
                                 fake_license,
                                 fake_license_without_keys]
        lsguicapabilities.side_effect = [fake_license_without_keys,
                                         fake_license_invalid_scheme,
                                         fake_license_scheme,
                                         fake_9100_license_scheme]
        self.assertFalse(self.storwize_svc_common.compression_enabled())

        self.assertFalse(self.storwize_svc_common.compression_enabled())

        self.assertTrue(self.storwize_svc_common.compression_enabled())

        self.assertTrue(self.storwize_svc_common.compression_enabled())

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsguicapabilities')
    def test_replication_licensed(self, lsguicapabilities):
        lsguicapabilities.side_effect = [
            {'product_key': '0000'},
            {'product_key':
                storwize_const.DEV_MODEL_STORWIZE_V3500},
            {'product_key':
                storwize_const.DEV_MODEL_STORWIZE_V5000E},
            {'product_key':
                storwize_const.DEV_MODEL_SVC},
            {'product_key':
                storwize_const.DEV_MODEL_STORWIZE},
            {'product_key':
                storwize_const.DEV_MODEL_STORWIZE_V7000},
            {'product_key':
                storwize_const.DEV_MODEL_STORWIZE_V5000},
            {'product_key':
                storwize_const.DEV_MODEL_STORWIZE_V5000_1YR},
            {'product_key':
                storwize_const.DEV_MODEL_FLASH_V9000},
            {'product_key':
                storwize_const.DEV_MODEL_FLEX}]
        for i in range(2):
            self.assertFalse(self.storwize_svc_common.replication_licensed())

        for i in range(8):
            self.assertTrue(self.storwize_svc_common.replication_licensed())

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsmdiskgrp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_count_by_io_group')
    def test_select_io_group(self, get_vdisk_count_by_io_group, lsmdiskgrp):
        # given io groups
        opts = {}
        # system io groups
        state = {}

        lsmdiskgrp.return_value = {}
        fake_iog_vdc1 = {0: 10, 1: 50, 2: 50, 3: 300}
        fake_iog_vdc2 = {0: 2, 1: 1, 2: 200}
        fake_iog_vdc3 = {0: 2, 2: 200}
        fake_iog_vdc4 = {0: 100, 1: 100, 2: 100, 3: 100}
        fake_iog_vdc5 = {0: 10, 1: 1, 2: 200, 3: 300}

        get_vdisk_count_by_io_group.side_effect = [fake_iog_vdc1,
                                                   fake_iog_vdc2,
                                                   fake_iog_vdc3,
                                                   fake_iog_vdc4,
                                                   fake_iog_vdc5]
        pool = _get_test_pool(False)
        opts['volume_topology'] = None
        opts['iogrp'] = '0,2'
        state['available_iogrps'] = [0, 1, 2, 3]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        self.assertEqual(0, iog)

        opts['iogrp'] = '0'
        state['available_iogrps'] = [0, 1, 2]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        self.assertEqual(0, iog)

        opts['iogrp'] = '1,2'
        state['available_iogrps'] = [0, 2]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        self.assertEqual(2, iog)

        opts['iogrp'] = ' 0, 1, 2 '
        state['available_iogrps'] = [0, 1, 2, 3]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        # since vdisk count in all iogroups is same, it will pick the first
        self.assertEqual(0, iog)

        opts['iogrp'] = '0,1,2, 3'
        state['available_iogrps'] = [0, 1, 2, 3]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        self.assertEqual(1, iog)

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsmdiskgrp')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_count_by_io_group')
    def test_select_io_group_hyperswap(self, get_vdisk_count_by_io_group,
                                       lsmdiskgrp):
        # given io groups
        opts = {}
        # system io groups
        state = {}

        lsmdiskgrp.return_value = {}
        fake_iog_vdc1 = {0: 100, 1: 50, 2: 50, 3: 300}
        fake_iog_vdc2 = {0: 2, 1: 1, 2: 200}
        fake_iog_vdc3 = {0: 2, 2: 200}
        fake_iog_vdc4 = {0: 100, 1: 100, 2: 100, 3: 100}
        fake_iog_vdc5 = {0: 10, 1: 1, 2: 200, 3: 300}

        get_vdisk_count_by_io_group.side_effect = [fake_iog_vdc1,
                                                   fake_iog_vdc2,
                                                   fake_iog_vdc3,
                                                   fake_iog_vdc4,
                                                   fake_iog_vdc5]
        pool = _get_test_pool(False)
        opts['iogrp'] = '0,2'
        opts['volume_topology'] = 'hyperswap'
        state['available_iogrps'] = [0, 1, 2, 3]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        self.assertEqual(2, iog)

        opts['iogrp'] = '0'
        state['available_iogrps'] = [0, 1, 2]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        self.assertEqual(0, iog)

        opts['iogrp'] = '1,2'
        state['available_iogrps'] = [0, 2]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        self.assertEqual(2, iog)

        opts['iogrp'] = ' 0, 1, 2 '
        state['available_iogrps'] = [0, 1, 2, 3]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        # since vdisk count in all iogroups is same, it will pick the first
        self.assertEqual(0, iog)

        opts['iogrp'] = '0,1,2, 3'
        state['available_iogrps'] = [0, 1, 2, 3]

        iog = self.storwize_svc_common.select_io_group(state, opts, pool)
        self.assertIn(iog, state['available_iogrps'])
        self.assertEqual(1, iog)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_flashcopy_mapping_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    def test_pretreatment_before_revert_uncompleted_clone(
            self,
            _get_vdisk_fc_mappings,
            _get_flashcopy_mapping_attributes):
        vol = 'testvol'
        _get_vdisk_fc_mappings.return_value = ['4']
        _get_flashcopy_mapping_attributes.return_value = {
            'copy_rate': '50',
            'progress': '3',
            'status': 'copying',
            'target_vdisk_name': 'testvol'}

        self.assertRaises(exception.VolumeDriverException,
                          self.storwize_svc_common.pretreatment_before_revert,
                          vol)

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'stopfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_flashcopy_mapping_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    def test_pretreatment_before_revert_completed_clone(
            self,
            _get_vdisk_fc_mappings,
            _get_flashcopy_mapping_attributes,
            stopfcmap):
        vol = 'testvol'
        _get_vdisk_fc_mappings.return_value = ['4']
        _get_flashcopy_mapping_attributes.return_value = {
            'copy_rate': '50',
            'progress': '100',
            'status': 'copying',
            'target_vdisk_name': 'testvol'}
        self.storwize_svc_common.pretreatment_before_revert(vol)
        stopfcmap.assert_called_once_with('4')

    @ddt.data({'copy_rate': '50', 'progress': '3', 'status': 'copying'},
              {'copy_rate': '50', 'progress': '100', 'status': 'copying'},
              {'copy_rate': '0', 'progress': '0', 'status': 'copying'},
              {'copy_rate': '50', 'progress': '0', 'status': 'copying'},
              {'copy_rate': '0', 'progress': '0', 'status': 'idle_or_copied'})
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'chfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'stopfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'rmfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_flashcopy_mapping_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_relationship_info')
    def test_check_vdisk_fc_mappings(self,
                                     fc_data,
                                     get_relationship_info,
                                     get_vdisk_fc_mappings,
                                     get_fc_mapping_attributes,
                                     rmfcmap, stopfcmap, chfcmap):

        vol = 'testvol'
        get_vdisk_fc_mappings.return_value = ['4']
        get_fc_mapping_attributes.return_value = {
            'copy_rate': fc_data['copy_rate'],
            'progress': fc_data['progress'],
            'status': fc_data['status'],
            'target_vdisk_name': 'tar-testvol',
            'rc_controlled': 'no',
            'source_vdisk_name': 'testvol'}
        get_relationship_info.return_value = None
        if (fc_data['copy_rate'] != '0' and fc_data['progress'] == '100'
           and fc_data['status'] == 'copying'):
            (self.assertRaises(loopingcall.LoopingCallDone,
             self.storwize_svc_common._check_vdisk_fc_mappings, vol, True,
             False))
            stopfcmap.assert_called_with('4', False)
            self.assertEqual(1, stopfcmap.call_count)
        else:
            self.storwize_svc_common._check_vdisk_fc_mappings(vol, True,
                                                              False)
            stopfcmap.assert_not_called()
            self.assertEqual(0, stopfcmap.call_count)

        get_vdisk_fc_mappings.assert_called()
        get_fc_mapping_attributes.assert_called_with('4')
        rmfcmap.assert_not_called()
        self.assertEqual(1, get_fc_mapping_attributes.call_count)
        self.assertEqual(0, rmfcmap.call_count)

        if (fc_data['copy_rate'] == '0' and fc_data['progress'] == '0'
           and fc_data['status'] in ['copying', 'idle_or_copied']):
            chfcmap.assert_called_with('4', copyrate='50', autodel='on')
            self.assertEqual(1, chfcmap.call_count)
        else:
            chfcmap.assert_not_called()
            self.assertEqual(0, chfcmap.call_count)

    @ddt.data({'copy_rate': '50', 'progress': '3', 'status': 'copying'},
              {'copy_rate': '50', 'progress': '100', 'status': 'copying'},
              {'copy_rate': '0', 'progress': '0', 'status': 'copying'},
              {'copy_rate': '50', 'progress': '0', 'status': 'copying'},
              {'copy_rate': '0', 'progress': '0', 'status': 'idle_or_copied'})
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'chfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'stopfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'rmfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_flashcopy_mapping_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_relationship_info')
    def test_check_vdisk_fc_mappings_with_rcrel(self,
                                                fc_data,
                                                get_relationship_info,
                                                get_vdisk_fc_mappings,
                                                get_fc_mapping_attributes,
                                                rmfcmap, stopfcmap, chfcmap):

        vol = 'testvol'
        get_vdisk_fc_mappings.return_value = ['4']
        get_fc_mapping_attributes.return_value = {
            'copy_rate': fc_data['copy_rate'],
            'progress': fc_data['progress'],
            'status': fc_data['status'],
            'target_vdisk_name': 'tar-testvol',
            'rc_controlled': 'no',
            'source_vdisk_name': 'testvol'}
        rel_info = {'name': 'rcrel232'}
        get_relationship_info.return_value = rel_info
        if (fc_data['copy_rate'] != '0' and fc_data['progress'] == '100'
                and fc_data['status'] == 'copying'):
            (self.assertRaises(loopingcall.LoopingCallDone,
             self.storwize_svc_common._check_vdisk_fc_mappings, vol, True,
             False, rel_info))
            stopfcmap.assert_called_with('4', True)
            self.assertEqual(1, stopfcmap.call_count)
        else:
            self.storwize_svc_common._check_vdisk_fc_mappings(vol, True,
                                                              False)
            stopfcmap.assert_not_called()
            self.assertEqual(0, stopfcmap.call_count)

        get_vdisk_fc_mappings.assert_called()
        get_fc_mapping_attributes.assert_called_with('4')
        rmfcmap.assert_not_called()
        self.assertEqual(1, get_fc_mapping_attributes.call_count)
        self.assertEqual(0, rmfcmap.call_count)

        if (fc_data['copy_rate'] == '0' and fc_data['progress'] == '0'
           and fc_data['status'] in ['copying', 'idle_or_copied']):
            chfcmap.assert_called_with('4', copyrate='50', autodel='on')
            self.assertEqual(1, chfcmap.call_count)
        else:
            chfcmap.assert_not_called()
            self.assertEqual(0, chfcmap.call_count)

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'chfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'stopfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'rmfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_flashcopy_mapping_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    def test_check_vdisk_fc_mappings_tarisvol(self,
                                              get_vdisk_fc_mappings,
                                              get_fc_mapping_attributes,
                                              rmfcmap, stopfcmap, chfcmap):
        vol = 'tar-testvol'
        get_vdisk_fc_mappings.return_value = ['4']
        get_fc_mapping_attributes.return_value = {
            'copy_rate': '0',
            'progress': '0',
            'status': 'idle_or_copied',
            'target_vdisk_name': 'tar-testvol',
            'rc_controlled': 'no',
            'source_vdisk_name': 'testvol'}

        self.assertRaises(loopingcall.LoopingCallDone,
                          self.storwize_svc_common._check_vdisk_fc_mappings,
                          vol, True, False)

        get_vdisk_fc_mappings.assert_called()
        get_fc_mapping_attributes.assert_called_with('4')
        stopfcmap.assert_not_called()
        rmfcmap.assert_called_with('4')
        chfcmap.assert_not_called()
        self.assertEqual(1, get_fc_mapping_attributes.call_count)
        self.assertEqual(0, stopfcmap.call_count)
        self.assertEqual(1, rmfcmap.call_count)
        self.assertEqual(0, chfcmap.call_count)

    @ddt.data(([{'cp_rate': '0', 'prgs': '0', 'status': 'idle_or_copied',
                 'trg_vdisk': 'testvol', 'src_vdisk': 'tar_testvol'},
                {'cp_rate': '50', 'prgs': '100', 'status': 'copying',
                 'trg_vdisk': 'tar_testvol', 'src_vdisk': 'testvol'},
                {'cp_rate': '50', 'prgs': '3', 'status': 'copying',
                 'trg_vdisk': 'tar_testvol', 'src_vdisk': 'testvol'}], 1),
              ([{'cp_rate': '50', 'prgs': '100', 'status': 'idle_or_copied',
                 'trg_vdisk': 'testvol', 'src_vdisk': 'tar_testvol'},
                {'cp_rate': '50', 'prgs': '100', 'status': 'copying',
                 'trg_vdisk': 'tar_testvol', 'src_vdisk': 'testvol'},
                {'cp_rate': '50', 'prgs': '100', 'status': 'copying',
                 'trg_vdisk': 'testvol', 'src_vdisk': 'tar_testvol'}], 1),
              ([{'cp_rate': '50', 'prgs': '100', 'status': 'idle_or_copied',
                 'trg_vdisk': 'testvol', 'src_vdisk': 'tar_testvol'},
                {'cp_rate': '50', 'prgs': '100', 'status': 'copying',
                 'trg_vdisk': 'tar_testvol', 'src_vdisk': 'testvol'},
                {'cp_rate': '50', 'prgs': '100', 'status': 'copying',
                 'trg_vdisk': 'tar_testvol_1', 'src_vdisk': 'testvol'}], 2),
              ([{'cp_rate': '0', 'prgs': '0', 'status': 'copying',
                 'trg_vdisk': 'testvol', 'src_vdisk': 'snap_testvol'},
                {'cp_rate': '50', 'prgs': '0', 'status': 'copying',
                 'trg_vdisk': 'tar_testvol', 'src_vdisk': 'testvol'},
                {'cp_rate': '50', 'prgs': '0', 'status': 'copying',
                 'trg_vdisk': 'tar_testvol_1', 'src_vdisk': 'testvol'}], 0))
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'chfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'stopfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'rmfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_flashcopy_mapping_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    @ddt.unpack
    def test_check_vdisk_fc_mappings_mul_fcs(self,
                                             fc_data, stopfc_count,
                                             get_vdisk_fc_mappings,
                                             get_fc_mapping_attributes,
                                             rmfcmap, stopfcmap, chfcmap):
        vol = 'testvol'
        get_vdisk_fc_mappings.return_value = ['4', '5', '7']
        get_fc_mapping_attributes.side_effect = [
            {
                'copy_rate': fc_data[0]['cp_rate'],
                'progress': fc_data[0]['prgs'],
                'status': fc_data[0]['status'],
                'target_vdisk_name': fc_data[0]['trg_vdisk'],
                'rc_controlled': 'no',
                'source_vdisk_name': fc_data[0]['src_vdisk']},
            {
                'copy_rate': fc_data[1]['cp_rate'],
                'progress': fc_data[1]['prgs'],
                'status': fc_data[1]['status'],
                'target_vdisk_name': fc_data[1]['trg_vdisk'],
                'rc_controlled': 'no',
                'source_vdisk_name': fc_data[1]['src_vdisk']},
            {
                'copy_rate': fc_data[2]['cp_rate'],
                'progress': fc_data[2]['prgs'],
                'status': fc_data[2]['status'],
                'target_vdisk_name': fc_data[2]['trg_vdisk'],
                'rc_controlled': 'no',
                'source_vdisk_name': fc_data[2]['src_vdisk']}]

        self.storwize_svc_common._check_vdisk_fc_mappings(vol, True, True)
        get_vdisk_fc_mappings.assert_called()
        get_fc_mapping_attributes.assert_called()
        rmfcmap.assert_not_called()
        chfcmap.assert_not_called()
        self.assertEqual(3, get_fc_mapping_attributes.call_count)
        self.assertEqual(stopfc_count, stopfcmap.call_count)
        self.assertEqual(0, rmfcmap.call_count)
        self.assertEqual(0, chfcmap.call_count)

    @ddt.data(([{'cp_rate': '0', 'prgs': '0', 'status': 'idle_or_copied',
                 'trg_vdisk': 'vdisk', 'src_vdisk': 'Hyp_vol'},
                {'cp_rate': '50', 'prgs': '0', 'status': 'idle_or_copied',
                 'trg_vdisk': 'Hyp_vol', 'src_vdisk': 'vdisk'},
                {'cp_rate': '50', 'prgs': '3', 'status': 'copying',
                 'trg_vdisk': 'Snap_vol', 'src_vdisk': 'Hyp_vol'},
                {'cp_rate': '50', 'prgs': '0', 'status': 'copying',
                 'trg_vdisk': 'Snap_vol_1', 'src_vdisk': 'Hyp_vol'}], 0),
              ([{'cp_rate': '0', 'prgs': '0', 'status': 'idle_or_copied',
                 'trg_vdisk': 'vdisk', 'src_vdisk': 'Hyp_vol'},
                {'cp_rate': '50', 'prgs': '0', 'status': 'idle_or_copied',
                 'trg_vdisk': 'Hyp_vol', 'src_vdisk': 'vdisk'},
                {'cp_rate': '50', 'prgs': '100', 'status': 'copying',
                 'trg_vdisk': 'Snap_vol', 'src_vdisk': 'Hyp_vol'},
                {'cp_rate': '50', 'prgs': '0', 'status': 'copying',
                 'trg_vdisk': 'Snap_vol_1', 'src_vdisk': 'Hyp_vol'}], 1))
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'chfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'stopfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'rmfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_flashcopy_mapping_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    @ddt.unpack
    def test_check_vdisk_fc_mappings_rc_cont(self,
                                             fc_data, stopfc_count,
                                             get_vdisk_fc_mappings,
                                             get_fc_mapping_attributes,
                                             rmfcmap, stopfcmap, chfcmap):
        vol = 'Hyp_vol'
        get_vdisk_fc_mappings.return_value = ['4', '5', '7', '9']
        get_fc_mapping_attributes.side_effect = [
            {
                'copy_rate': fc_data[0]['cp_rate'],
                'progress': fc_data[0]['prgs'],
                'status': fc_data[0]['status'],
                'target_vdisk_name': fc_data[0]['trg_vdisk'],
                'rc_controlled': 'yes',
                'source_vdisk_name': fc_data[0]['src_vdisk']},
            {
                'copy_rate': fc_data[1]['cp_rate'],
                'progress': fc_data[1]['prgs'],
                'status': fc_data[1]['status'],
                'target_vdisk_name': fc_data[1]['trg_vdisk'],
                'rc_controlled': 'yes',
                'source_vdisk_name': fc_data[1]['src_vdisk']},
            {
                'copy_rate': fc_data[2]['cp_rate'],
                'progress': fc_data[2]['prgs'],
                'status': fc_data[2]['status'],
                'target_vdisk_name': fc_data[2]['trg_vdisk'],
                'rc_controlled': 'no',
                'source_vdisk_name': fc_data[2]['src_vdisk']},
            {
                'copy_rate': fc_data[3]['cp_rate'],
                'progress': fc_data[3]['prgs'],
                'status': fc_data[3]['status'],
                'target_vdisk_name': fc_data[3]['trg_vdisk'],
                'rc_controlled': 'no',
                'source_vdisk_name': fc_data[3]['src_vdisk']}]

        self.storwize_svc_common._check_vdisk_fc_mappings(vol, True, True)
        get_vdisk_fc_mappings.assert_called()
        get_fc_mapping_attributes.assert_called()
        rmfcmap.assert_not_called()
        chfcmap.assert_not_called()
        self.assertEqual(4, get_fc_mapping_attributes.call_count)
        self.assertEqual(stopfc_count, stopfcmap.call_count)
        self.assertEqual(0, rmfcmap.call_count)
        self.assertEqual(0, chfcmap.call_count)

    def test_storwize_check_flashcopy_rate_invalid1(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 6, 0, 0),
                                'topology': 'standard',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            flashcopy_rate = 120
            self.assertRaises(exception.VolumeDriverException,
                              self.storwize_svc_common.check_flashcopy_rate,
                              flashcopy_rate)

    def test_storwize_check_flashcopy_rate_invalid2(self):
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_system_info:
            fake_system_info = {'code_level': (7, 8, 1, 2),
                                'topology': 'standard',
                                'system_name': 'storwize-svc-sim',
                                'system_id': '0123456789ABCDEF'}
            get_system_info.return_value = fake_system_info
            flashcopy_rate = 200
            self.assertRaises(exception.InvalidInput,
                              self.storwize_svc_common.check_flashcopy_rate,
                              flashcopy_rate)

    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'chfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_get_vdisk_fc_mappings')
    def test_storwize_update_clean_rate(self,
                                        chfcmap,
                                        get_vdisk_fc_mappings):
        get_vdisk_fc_mappings.return_value = ['4']
        vol = 'test_vol'
        new_clean_rate = 50
        self.storwize_svc_common.update_clean_rate(vol, new_clean_rate)
        chfcmap.assert_called()

    @ddt.data(({'mirror_pool': 'openstack2',
                'volume_topology': None,
                'peer_pool': None}, True, 1),
              ({'mirror_pool': 'openstack2',
                'volume_topology': None,
                'peer_pool': None}, False, 2),
              ({'mirror_pool': None,
                'volume_topology': 'hyperswap',
                'peer_pool': 'openstack1'}, True, 1),
              ({'mirror_pool': None,
                'volume_topology': 'hyperswap',
                'peer_pool': 'openstack1'}, False, 2))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'is_data_reduction_pool')
    @ddt.unpack
    def test_is_volume_type_dr_pools_dr_pool(self, opts, is_drp, call_count,
                                             is_data_reduction_pool):
        is_data_reduction_pool.return_value = is_drp
        pool = 'openstack'
        rep_type = None
        rep_target_pool = None

        isdrpool = (self.storwize_svc_common.
                    is_volume_type_dr_pools(pool, opts, rep_type,
                                            rep_target_pool))
        self.assertEqual(is_drp, isdrpool)
        is_data_reduction_pool.assert_called()
        self.assertEqual(call_count, is_data_reduction_pool.call_count)

    @ddt.data(({'RC_name': None,
                'name': 'volume-12d-5'}, True),
              ({'RC_name': 'fake_rcrel',
                'name': 'rep_volume-12d-6'}, False))
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'startrcrelationship')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'stoprcrelationship')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_attributes')
    @ddt.unpack
    def test_stop_and_start_rc_relationship(self, opts, access,
                                            get_vdisk_attributes,
                                            stoprcrelationship,
                                            startrcrelationship):
        get_vdisk_attributes.side_effect = [{'RC_name': opts['RC_name']},
                                            {'RC_name': opts['RC_name']}]
        self.storwize_svc_common.stop_relationship(opts['name'])
        self.storwize_svc_common.start_relationship(opts['name'])
        get_vdisk_attributes.assert_called_with(opts['name'])
        if not opts['RC_name']:
            stoprcrelationship.assert_called()
            startrcrelationship.assert_called()
        else:
            stoprcrelationship.assert_called_once_with(opts['RC_name'],
                                                       access=access)
            startrcrelationship.assert_called_once_with(opts['RC_name'], None)

    @ddt.data(({'RC_name': 'fake_rcrel',
                'consistency_group_name': 'fake_rccg-1',
                'name': 'rep_volume-12d-6'}),
              ({'RC_name': 'fake_rcrel',
                'consistency_group_name': None,
                'name': 'rep_volume-12d-6'}))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'lsrcrelationship')
    def test_get_rccg_name_by_volume_name(self, opts, lsrcrelationship,
                                          get_vdisk_attributes):
        get_vdisk_attributes.side_effect = [{'RC_name': opts['RC_name']}]
        lsrcrelationship.side_effect = \
            [[{'consistency_group_name': opts['consistency_group_name']}]]
        rccg_name = self.storwize_svc_common.get_rccg_name_by_volume_name(
            opts['name'])
        get_vdisk_attributes.assert_called_with(opts['name'])
        lsrcrelationship.assert_called_with(opts['RC_name'])
        self.assertEqual(rccg_name, opts['consistency_group_name'])

    @ddt.data(({'name': 'rep_volume-12d-6'}))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_attributes')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'lsrcrelationship')
    def test_get_rccg_name_by_volume_name_with_None_volume_attributes(
            self, opts, lsrcrelationship, get_vdisk_attributes):
        get_vdisk_attributes.side_effect = [None]
        rccg_name = self.storwize_svc_common.get_rccg_name_by_volume_name(
            opts['name'])
        get_vdisk_attributes.assert_called_with(opts['name'])
        lsrcrelationship.assert_not_called()
        self.assertIsNone(rccg_name)

    @ddt.data(({'RC_name': None,
                'name': 'volume-12d-7'}, 'multi'),
              ({'RC_name': 'fake_rcrel',
                'name': 'rep_volume-12d-8'}, 'multi'),
              ({'RC_name': 'fake_rcrel-2',
                'name': 'rep_volume-12d-9'}, 'none'),
              ({'RC_name': 'fake_rcrel-3',
                'name': 'rep_volume-12d-10'}, None))
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'ch_rcrelationship_cyclingmode')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_vdisk_attributes')
    @ddt.unpack
    def test_change_relationship_cyclingmode(self, opts, cycling_mode,
                                             get_vdisk_attributes,
                                             ch_rcrelationship_cyclingmode):
        get_vdisk_attributes.side_effect = [{'RC_name': opts['RC_name']}]
        self.storwize_svc_common.change_relationship_cyclingmode(
            opts['name'], cycling_mode)
        get_vdisk_attributes.assert_called_with(opts['name'])
        if not opts['RC_name'] or not cycling_mode:
            ch_rcrelationship_cyclingmode.assert_not_called()
        else:
            ch_rcrelationship_cyclingmode.assert_called_once_with(
                opts['RC_name'], cycling_mode)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_host_from_host_info')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lshost')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsvdiskhostmap')
    def test_get_host_from_connector_with_vol(self,
                                              lsvdishosmap,
                                              lshost,
                                              get_host_from_host_info):
        vol = 'testvol'
        connector = {"wwpns": ["10000090fa3870d7", "C050760825191B00"]}
        get_host_from_host_info.return_value = "test_host", []
        raw = "id!name!host_id!host_name!vdisk_UID\n2594!testvol!315!"\
              "test_host!60050768028110A4700000000001168E"
        ssh_cmd = ['svcinfo', 'lsvdiskhostmap', '-delim', '!', '"%s"' % vol]
        lsvdishosmap.return_value = storwize_svc_common.CLIResponse(raw,
                                                                    ssh_cmd,
                                                                    '!',
                                                                    True)
        host = self.storwize_svc_common.get_host_from_connector(connector,
                                                                vol)
        self.assertEqual(host, "test_host")
        get_host_from_host_info.assert_called_with(connector, False)
        self.assertEqual(1, get_host_from_host_info.call_count)
        lsvdishosmap.assert_called_with(vol)
        self.assertEqual(1, lsvdishosmap.call_count)
        lshost.assert_not_called()

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_host_from_host_info')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lshost')
    @mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsvdiskhostmap')
    def test_get_host_from_connector_wo_vol(self,
                                            lsvdishosmap,
                                            lshost,
                                            get_host_from_host_info):
        vol = 'testvol'
        connector = {"wwpns": ["10000090fa3870d7", "C050760825191B00"]}
        get_host_from_host_info.return_value = "test_host", []
        raw = "id!name!host_id!host_name!vdisk_UID\n2594!testvol!315!"\
              "test_host!60050768028110A4700000000001168E"
        ssh_cmd = ['svcinfo', 'lsvdiskhostmap', '-delim', '!', '"%s"' % vol]
        lsvdishosmap.return_value = storwize_svc_common.CLIResponse(raw,
                                                                    ssh_cmd,
                                                                    '!',
                                                                    True)
        host = self.storwize_svc_common.get_host_from_connector(connector)
        self.assertEqual(host, "test_host")
        get_host_from_host_info.assert_called_with(connector, False)
        self.assertEqual(1, get_host_from_host_info.call_count)
        lsvdishosmap.assert_not_called()
        self.assertEqual(0, lsvdishosmap.call_count)
        lshost.assert_not_called()


@ddt.ddt
class StorwizeSSHTestCase(test.TestCase):
    def setUp(self):
        super(StorwizeSSHTestCase, self).setUp()
        self.fake_driver = StorwizeSVCISCSIFakeDriver(
            configuration=conf.Configuration(None))
        sim = StorwizeSVCManagementSimulator(['openstack'])
        self.fake_driver.set_fake_storage(sim)
        self.storwize_ssh = storwize_svc_common.StorwizeSSH(
            self.fake_driver._run_ssh)

    def test_ch_rcrelationship_cyclingmode(self):
        with mock.patch.object(
                storwize_svc_common.StorwizeSSH,
                'run_ssh_assert_no_output') as run_ssh_assert_no_output:
            run_ssh_assert_no_output.return_value = None
            ret = self.storwize_ssh.ch_rcrelationship_cyclingmode('rcrel0',
                                                                  'multi')
            self.assertIsNone(ret)
            ret = self.storwize_ssh.ch_rcrelationship_cyclingmode('rcrel1',
                                                                  'none')
            self.assertIsNone(ret)

    def test_ch_rcconsistgrp_cyclingmode(self):
        with mock.patch.object(
                storwize_svc_common.StorwizeSSH,
                'run_ssh_assert_no_output') as run_ssh_assert_no_output:
            run_ssh_assert_no_output.return_value = None
            ret = self.storwize_ssh.ch_rcconsistgrp_cyclingmode('fake_rccg-1',
                                                                'multi')
            self.assertIsNone(ret)
            ret = self.storwize_ssh.ch_rcconsistgrp_cyclingmode('fake_rccg-1')
            self.assertIsNone(ret)

    def test_mkvdiskhostmap(self):
        # mkvdiskhostmap should not be returning anything
        with mock.patch.object(
                storwize_svc_common.StorwizeSSH,
                'run_ssh_check_created') as run_ssh_check_created:
            run_ssh_check_created.return_value = None
            ret = self.storwize_ssh.mkvdiskhostmap('HOST1', 9999, 511, False)
            self.assertIsNone(ret)
            ret = self.storwize_ssh.mkvdiskhostmap('HOST2', 9999, 511, True)
            self.assertIsNone(ret)
            ex = exception.VolumeBackendAPIException(data='CMMVC6071E')
            run_ssh_check_created.side_effect = ex
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.storwize_ssh.mkvdiskhostmap,
                              'HOST3', 9999, 511, True)

    @ddt.data((exception.VolumeBackendAPIException(data='CMMVC6372W'), None),
              (exception.VolumeBackendAPIException(data='CMMVC6372W'),
               {'name': 'fakevol', 'id': '0', 'uid': '0', 'IO_group_id': '0',
                'IO_group_name': 'fakepool'}),
              (exception.VolumeBackendAPIException(data='error'), None))
    @ddt.unpack
    def test_mkvdisk_with_warning(self, run_ssh_check, lsvol):
        opt = {'iogrp': 0}
        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'run_ssh_check_created',
                               side_effect=run_ssh_check), \
            mock.patch.object(storwize_svc_common.StorwizeSSH, 'lsvdisk',
                              return_value=lsvol):
            if lsvol:
                ret = self.storwize_ssh.mkvdisk('fakevol', '1', 'gb',
                                                'fakepool', opt, [])
                self.assertEqual('0', ret)
            else:
                self.assertRaises(exception.VolumeBackendAPIException,
                                  self.storwize_ssh.mkvdisk,
                                  'fakevol', '1', 'gb', 'fakepool', opt, [])


@ddt.ddt
class StorwizeSVCReplicationTestCase(test.TestCase):
    @mock.patch.object(time, 'sleep')
    def setUp(self, mock_sleep):
        super(StorwizeSVCReplicationTestCase, self).setUp()

        def _run_ssh_aux(cmd, check_exit_code=True, attempts=1):
            utils.check_ssh_injection(cmd)
            if len(cmd) > 2 and cmd[1] == 'lssystem':
                cmd[1] = 'lssystem_aux'
            ret = self.sim.execute_command(cmd, check_exit_code)
            return ret
        aux_connect_patcher = mock.patch(
            'cinder.volume.drivers.ibm.storwize_svc.'
            'replication.StorwizeSVCReplicationManager._run_ssh')
        self.aux_ssh_mock = aux_connect_patcher.start()
        self.addCleanup(aux_connect_patcher.stop)
        self.aux_ssh_mock.side_effect = _run_ssh_aux

        self.USESIM = True
        if self.USESIM:
            self.driver = StorwizeSVCFcFakeDriver(
                configuration=conf.Configuration(None))
            self.rep_target = {"backend_id": "svc_aux_target_1",
                               "san_ip": "192.168.10.22",
                               "san_login": "admin",
                               "san_password": "admin",
                               "pool_name": _get_test_pool()}
            self.fake_target = {"backend_id": "svc_id_target",
                                "san_ip": "192.168.10.23",
                                "san_login": "admin",
                                "san_password": "admin",
                                "pool_name": _get_test_pool()}
            self._def_flags = {'san_ip': '192.168.10.21',
                               'san_login': 'user',
                               'san_password': 'pass',
                               'storwize_svc_volpool_name':
                               SVC_POOLS,
                               'replication_device': [self.rep_target]}
            wwpns = [
                str(random.randint(0, 9999999999999999)).zfill(16),
                str(random.randint(0, 9999999999999999)).zfill(16)]
            initiator = 'test.initiator.%s' % str(
                random.randint(10000, 99999))
            self._connector = {'ip': '1.234.56.78',
                               'host': 'storwize-svc-test',
                               'wwpns': wwpns,
                               'initiator': initiator}
            self.sim = StorwizeSVCManagementSimulator(SVC_POOLS)

            self.driver.set_fake_storage(self.sim)
            self.ctxt = context.get_admin_context()

        self._reset_flags()
        self.ctxt = context.get_admin_context()
        self.db = cinder.db
        self.driver.db = self.db
        self.driver.do_setup(None)
        self.driver.check_for_setup_error()
        self._create_test_volume_types()
        self.rccg_type = self._create_consistent_rep_grp_type()

    def _create_group_snapshot_in_db(self, group_id, **kwargs):
        group_snapshot = testutils.create_group_snapshot(self.ctxt,
                                                         group_id=group_id,
                                                         **kwargs)
        snapshots = []
        volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), group_id)

        if not volumes:
            msg = _("Group is empty. No cgsnapshot will be created.")
            raise exception.InvalidGroup(reason=msg)

        for volume in volumes:
            snapshots.append(testutils.create_snapshot(
                self.ctxt, volume['id'],
                group_snapshot.id,
                group_snapshot.name,
                group_snapshot.id,
                fields.SnapshotStatus.CREATING))
        return group_snapshot, snapshots

    def _create_group_snapshot(self, cg_id, **kwargs):
        group_snapshot, snapshots = self._create_group_snapshot_in_db(
            cg_id, **kwargs)

        model_update, snapshots_model = (
            self.driver.create_group_snapshot(self.ctxt, group_snapshot,
                                              snapshots))
        self.assertEqual(fields.GroupSnapshotStatus.AVAILABLE,
                         model_update['status'],
                         "CGSnapshot created failed")

        for snapshot in snapshots_model:
            self.assertEqual(fields.SnapshotStatus.AVAILABLE,
                             snapshot['status'])
        return group_snapshot, snapshots

    def _set_flag(self, flag, value):
        group = self.driver.configuration.config_group
        self.driver.configuration.set_override(flag, value, group)

    def _reset_flags(self):
        for k, v in self._def_flags.items():
            self._set_flag(k, v)
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])

    def _assert_vol_exists(self, name, exists):
        is_vol_defined = self.driver._helpers.is_vdisk_defined(name)
        self.assertEqual(exists, is_vol_defined)

    def _generate_vol_info(self, vol_type=None, **kwargs):
        pool = _get_test_pool()
        volume_type = vol_type if vol_type else self.non_replica_type
        prop = {'size': 1,
                'volume_type_id': volume_type.id,
                'host': 'openstack@svc#%s' % pool
                }
        for p in prop.keys():
            if p not in kwargs:
                kwargs[p] = prop[p]
        vol = testutils.create_volume(self.ctxt, **kwargs)
        return vol

    def _generate_snap_info(self, vol_id):
        prop = {'volume_id': vol_id}
        snap = testutils.create_snapshot(self.ctxt, **prop)
        return snap

    def _create_replica_volume_type(self, enable,
                                    rep_type=storwize_const.METRO,
                                    opts=None, vol_type_name=None,
                                    cycle_period_seconds=None):
        # Generate a volume type for volume repliation.
        if enable:
            if rep_type == storwize_const.METRO:
                spec = {'replication_enabled': '<is> True',
                        'replication_type': '<in> metro'}
                type_name = 'rep_metro'
            elif rep_type == storwize_const.GMCV:
                if cycle_period_seconds:
                    spec = {'replication_enabled': '<is> True',
                            'replication_type': '<in> gmcv',
                            'drivers:cycle_period_seconds':
                                cycle_period_seconds}
                    type_name = 'rep_gmcv_with_cps' + cycle_period_seconds
                else:
                    spec = {'replication_enabled': '<is> True',
                            'replication_type': '<in> gmcv'}
                    type_name = 'rep_gmcv_default'
            else:
                spec = {'replication_enabled': '<is> True',
                        'replication_type': '<in> global'}
                type_name = 'rep_global'
        elif opts:
            spec = opts
            type_name = vol_type_name
        else:
            spec = {'replication_enabled': '<is> False'}
            type_name = "non_rep"

        type_ref = volume_types.create(self.ctxt, type_name, spec)
        replication_type = objects.VolumeType.get_by_id(self.ctxt,
                                                        type_ref['id'])
        return replication_type

    def _create_test_volume_types(self):
        self.mm_type = self._create_replica_volume_type(
            True, rep_type=storwize_const.METRO)
        self.gm_type = self._create_replica_volume_type(
            True, rep_type=storwize_const.GLOBAL)
        self.gmcv_default_type = self._create_replica_volume_type(
            True, rep_type=storwize_const.GMCV)
        self.gmcv_with_cps600_type = self._create_replica_volume_type(
            True, rep_type=storwize_const.GMCV, cycle_period_seconds="600")
        self.gmcv_with_cps900_type = self._create_replica_volume_type(
            True, rep_type=storwize_const.GMCV, cycle_period_seconds="900")
        self.gmcv_with_cps86401_type = self._create_replica_volume_type(
            True, rep_type=storwize_const.GMCV, cycle_period_seconds="86401")
        self.non_replica_type = self._create_replica_volume_type(False)

    def _create_test_volume(self, rep_type, **kwargs):
        volume = self._generate_vol_info(rep_type, **kwargs)
        model_update = self.driver.create_volume(volume)
        return volume, model_update

    def _create_consistent_rep_grp_type(self):
        rccg_spec = {'consistent_group_replication_enabled': '<is> True'}
        rccg_type_ref = group_types.create(self.ctxt, 'cg_type', rccg_spec)
        rccg_type = objects.GroupType.get_by_id(self.ctxt, rccg_type_ref['id'])
        return rccg_type

    def _create_test_rccg(self, rccg_type, vol_type_ids):
        # create group in db
        group = testutils.create_group(self.ctxt,
                                       volume_type_ids=vol_type_ids,
                                       group_type_id=rccg_type.id)
        if self.rccg_type == rccg_type:
            group.replication_status = fields.ReplicationStatus.ENABLED
        self.driver.create_group(self.ctxt, group)
        return group

    def _get_vdisk_uid(self, vdisk_name):
        vdisk_properties, _err = self.sim._cmd_lsvdisk(obj=vdisk_name,
                                                       delim='!')
        for row in vdisk_properties.split('\n'):
            words = row.split('!')
            if words[0] == 'vdisk_UID':
                return words[1]
        return None

    def _get_pool_volumes(self, pool):
        vdisks = self.sim._cmd_lsvdisks_from_filter('mdisk_grp_name', pool)
        return vdisks

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_build_pool_stats')
    def test_update_volume_stats_replication(self, _build_pool_stats):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver._update_volume_stats()
        self.assertTrue(self.driver._replica_enabled)
        target_pools = [self.driver._replica_target.get('pool_name')]
        # Expected call count = Number of primary Pools and Secondary Pools
        expected_call_count = len(SVC_POOLS) + len(target_pools)
        self.assertEqual(expected_call_count, _build_pool_stats.call_count)
        self.assertIsNotNone(self.driver._master_backend_helpers.stats)
        self.assertIsNotNone(self.driver._aux_backend_helpers.stats)

    @ddt.data((False, False), (True, True))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_pool_attrs')
    @ddt.unpack
    def test_build_pool_stats_calls(self, replication_enabled,
                                    target, get_pool_attrs):
        pool = "openstack"
        master_helper = self.driver._master_backend_helpers
        target_helper = self.driver._aux_backend_helpers
        if replication_enabled:
            self.driver.configuration.set_override('replication_device',
                                                   [self.rep_target])
        self.driver._build_pool_stats(pool, target)
        if target:
            target_helper.get_pool_attrs.assert_called_once_with(pool)
        else:
            master_helper.get_pool_attrs.assert_called_once_with(pool)

    def test_storwize_do_replication_setup_error(self):
        fake_targets = [self.rep_target, self.rep_target]
        self.driver.configuration.set_override('replication_device',
                                               [{"backend_id":
                                                "svc_id_target"}])
        self.assertRaises(exception.InvalidInput,
                          self.driver._do_replication_setup)

        self.driver.configuration.set_override('replication_device',
                                               fake_targets)
        self.assertRaises(exception.InvalidInput,
                          self.driver._do_replication_setup)

        self.driver._active_backend_id = 'fake_id'
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.assertRaises(exception.InvalidInput,
                          self.driver._do_replication_setup)

        self.driver._active_backend_id = None

        self.driver._do_replication_setup()
        self.assertEqual(self.driver._replica_target, self.rep_target)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'replication_licensed')
    def test_storwize_setup_replication(self,
                                        replication_licensed):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver._active_backend_id = None
        replication_licensed.side_effect = [False, True, True, True, True]

        self.driver._get_storwize_config()
        self.assertEqual(self.driver._helpers,
                         self.driver._master_backend_helpers)
        self.assertFalse(self.driver._replica_enabled)

        self.driver._get_storwize_config()
        self.assertEqual(self.driver._replica_target, self.rep_target)
        self.assertTrue(self.driver._replica_enabled)

        self.driver._active_backend_id = self.rep_target['backend_id']
        self.driver._get_storwize_config()
        self.assertEqual(self.driver._helpers,
                         self.driver._aux_backend_helpers)
        self.assertTrue(self.driver._replica_enabled)

        self.driver._active_backend_id = None
        self.driver._get_storwize_config()

        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_update_storwize_state') as update_state:
            update_state.side_effect = [
                exception.VolumeBackendAPIException(data='CMMVC6372W'),
                exception.VolumeBackendAPIException(data='CMMVC6372W'), None]
            self.driver._active_backend_id = None
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver._get_storwize_config)
            self.driver._active_backend_id = self.rep_target['backend_id']
            self.driver._get_storwize_config()
        self.assertEqual(self.driver._helpers,
                         self.driver._aux_backend_helpers)
        self.assertTrue(self.driver._replica_enabled)

    def test_storwize_create_volume_with_mirror_replication(self):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create metro mirror replication.
        volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume)
        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume)

        # Create global mirror replication.
        volume, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume)
        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume)

        # Create global mirror with change volumes replication.
        volume, model_update = self._create_test_volume(
            self.gmcv_default_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)
        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume, True)
        # gmcv with specified cycle_period_seconds
        volume, model_update = self._create_test_volume(
            self.gmcv_with_cps600_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)
        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume, True)
        # gmcv with invalid cycle_period_seconds
        self.assertRaises(exception.InvalidInput,
                          self._create_test_volume,
                          self.gmcv_with_cps86401_type)

    def test_storwize_create_replication_volume_with_mirror_pool(self):
        """Create a replication volume with mirror_pool option"""
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create MM volume with mirror_pool
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> metro',
                'drivers:mirror_pool': 'openstack1'}
        mm_mirror_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='mm_mirror_type')
        mm_volume, model_update = self._create_test_volume(
            mm_mirror_type)
        # Check the parameters and their values
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self.assertEqual('inconsistent_copying',
                         model_update['metadata']['Mirroring State'])
        # Delete the MM volume
        self.driver.delete_volume(mm_volume)

        # Create GM volume with mirror_pool
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> global',
                'drivers:mirror_pool': 'openstack1'}
        gm_mirror_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='gm_mirror_type')
        gm_volume, model_update = self._create_test_volume(
            gm_mirror_type)
        # Check the parameters and their values
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self.assertEqual('inconsistent_copying',
                         model_update['metadata']['Mirroring State'])
        # Delete the GM volume
        self.driver.delete_volume(gm_volume)

    def test_storwize_retype_mm_replication_volume_with_mirror_pool(self):
        """Create a MM replication volume with mirror_pool option and retype

        it to MM replication volume type without mirror pool option
        """
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}

        # Create MM volume
        mm_volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create MM volume-type with mirror_pool option
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> metro',
                'drivers:mirror_pool': 'openstack1'}
        mm_mirror_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='mm_mirror_type')

        # Retype the MM-volume to volume-type with mirror_pool option
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, mm_mirror_type['id'], self.mm_type['id'])
        retyped, model_update = self.driver.retype(
            self.ctxt, mm_volume, mm_mirror_type, diff, host)
        self.driver.delete_volume(mm_volume)

    def test_storwize_retype_gm_replication_volume_with_mirror_pool(self):
        """Create a GM replication volume with mirror_pool option and retype

        it to GM replication volume type without mirror pool option
        """
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}

        # Create GM volume
        gm_volume, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create GM volume-type with mirror_pool option
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> global',
                'drivers:mirror_pool': 'openstack1'}
        gm_mirror_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='gm_mirror_type')

        # Retype the GM-volume to volume-type with mirror_pool option
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, gm_mirror_type['id'], self.gm_type['id'])
        retyped, model_update = self.driver.retype(
            self.ctxt, gm_volume, gm_mirror_type, diff, host)
        self.driver.delete_volume(gm_volume)

    @ddt.data((None, None),
              (None, SVC_TARGET_CHILD_POOL), (SVC_SOURCE_CHILD_POOL, None),
              (SVC_SOURCE_CHILD_POOL, SVC_TARGET_CHILD_POOL))
    @ddt.unpack
    def test_storwize_create_gmcv_volume_with_childpool(
            self, svc_src_childpool, svc_tgt_childpool):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create gmcv volume with change volumes on child pools
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> gmcv',
                'drivers:storwize_svc_src_child_pool': svc_src_childpool,
                'drivers:storwize_svc_target_child_pool': svc_tgt_childpool}
        gmcv_childpool_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='test_gmcv_childpool_type')
        volume, model_update = self._create_test_volume(
            gmcv_childpool_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)

        src_chg_vol_storage_pool = (
            svc_src_childpool if svc_src_childpool else _get_test_pool())
        tgt_chg_vol_storage_pool = (
            svc_tgt_childpool if svc_tgt_childpool else _get_test_pool())
        src_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               volume.name)
        src_childpool_vols = self._get_pool_volumes(src_chg_vol_storage_pool)
        self.assertIn(src_change_vol_name, src_childpool_vols)

        tgt_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               storwize_const.REPLICA_AUX_VOL_PREFIX +
                               volume.name)
        tgt_childpool_vols = self._get_pool_volumes(tgt_chg_vol_storage_pool)
        self.assertIn(tgt_change_vol_name, tgt_childpool_vols)

        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume, True)

    @ddt.data(({"backend_id": "svc_aux_target_1",
                "san_ip": "192.168.10.22",
                "san_login": "admin",
                "san_password": "admin",
                "pool_name": "openstack"}, 'openstack@svc#dr_pool1'),
              ({"backend_id": "svc_aux_target_1",
                "san_ip": "192.168.10.22",
                "san_login": "admin",
                "san_password": "admin",
                "pool_name": "dr_pool1"}, 'openstack@svc#openstack'))
    @ddt.unpack
    def test_storwize_replication_volume_with_dr_pools(self, target, vol_host):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [target])

        self.driver.do_setup(self.ctxt)

        # Create metro mirror replication volume on dr_pool.
        volume = testutils.create_volume(
            self.ctxt, volume_type_id=self.mm_type.id,
            host=vol_host)
        model_update = self.driver.create_volume(volume)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        volume1 = testutils.create_volume(
            self.ctxt, volume_type_id=self.mm_type.id,
            host=vol_host)
        ref = {'source-name': volume.name}
        self.driver.manage_existing(volume1, ref)

        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> metro',
                'easytier': 'False'}
        type_ref = volume_types.create(self.ctxt, 'type_dr', spec)
        dr_type = objects.VolumeType.get_by_id(self.ctxt, type_ref['id'])
        volume2 = testutils.create_volume(
            self.ctxt, volume_type_id=dr_type.id,
            host=vol_host)
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_volume, volume2)

        volume3 = testutils.create_volume(
            self.ctxt, volume_type_id=self.mm_type.id,
            host=vol_host)
        model_update = self.driver.create_volume(volume3)
        ref2 = {'source-name': volume3.name}
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, volume2, ref2)

        volume4 = testutils.create_volume(
            self.ctxt, volume_type_id=self.non_replica_type.id,
            host=vol_host)
        self.driver.create_volume(volume4)
        # Retype to mm replica
        host = {'host': vol_host}
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.non_replica_type['id'], self.mm_type['id'])
        retyped, model_update = self.driver.retype(
            self.ctxt, volume4, self.mm_type, diff, host)
        volume4['volume_type_id'] = self.mm_type['id']
        volume4['volume_type'] = self.mm_type
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume4)

        volume5 = testutils.create_volume(
            self.ctxt, volume_type_id=self.non_replica_type.id,
            host=vol_host)
        self.driver.create_volume(volume5)
        # retype with check dr_pool params failure
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.non_replica_type['id'], dr_type['id'])
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, self.ctxt, volume5,
                          dr_type, diff, host)

    def _validate_replic_vol_creation(self, volume, isGMCV=False):
        self._assert_vol_exists(volume['name'], True)
        self._assert_vol_exists(
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], True)
        if isGMCV:
            self._assert_vol_exists(
                storwize_const.REPLICA_CHG_VOL_PREFIX + volume['name'], True)
            self._assert_vol_exists(
                storwize_const.REPLICA_CHG_VOL_PREFIX +
                storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], True)

        rel_info = self.driver._helpers.get_relationship_info(volume['name'])
        self.assertIsNotNone(rel_info)
        if isGMCV:
            vol_rep_type = rel_info['copy_type']
            cycling_mode = rel_info['cycling_mode']
            cycle_period_seconds = rel_info['cycle_period_seconds']
            rep_type = self.driver._get_volume_replicated_type(
                self.ctxt, volume)
            src_opts = self.driver._get_vdisk_params(volume['volume_type_id'])
            opt_cycle_period_seconds = str(
                src_opts.get('cycle_period_seconds'))
            self.assertEqual(opt_cycle_period_seconds, cycle_period_seconds)
            self.assertEqual(storwize_const.GMCV_MULTI, cycling_mode)
            self.assertEqual(storwize_const.GLOBAL, vol_rep_type)
            self.assertEqual(storwize_const.GMCV, rep_type)
            self.assertEqual('master', rel_info['primary'])
            self.assertEqual(volume['name'], rel_info['master_vdisk_name'])
            self.assertEqual(
                storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'],
                rel_info['aux_vdisk_name'])
            self.assertIn(rel_info['state'], ['consistent_copying'])
            self.assertEqual(
                storwize_const.REPLICA_CHG_VOL_PREFIX + volume['name'],
                rel_info['master_change_vdisk_name'])
            self.assertEqual(
                storwize_const.REPLICA_CHG_VOL_PREFIX +
                storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'],
                rel_info['aux_change_vdisk_name'])
            self.assertIn(rel_info['state'], ['consistent_copying'])
            self.sim._rc_state_transition('wait', rel_info)
            self.assertEqual('consistent_copying', rel_info['state'])
        else:
            vol_rep_type = rel_info['copy_type']
            rep_type = self.driver._get_volume_replicated_type(
                self.ctxt, volume)
            self.assertEqual(rep_type, vol_rep_type)

            self.assertEqual('master', rel_info['primary'])
            self.assertEqual(volume['name'], rel_info['master_vdisk_name'])
            self.assertEqual(
                storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'],
                rel_info['aux_vdisk_name'])
            self.assertIn(rel_info['state'],
                          ['consistent_synchronized', 'inconsistent_copying'])

            self.sim._rc_state_transition('wait', rel_info)
            self.assertEqual('consistent_synchronized', rel_info['state'])

    def _validate_gmcv_vol_retype(self, volume):
        self._assert_vol_exists(volume['name'], True)
        self._assert_vol_exists(
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], True)
        self._assert_vol_exists(storwize_const.REPLICA_CHG_VOL_PREFIX +
                                volume['name'], True)
        self._assert_vol_exists(
            storwize_const.REPLICA_CHG_VOL_PREFIX +
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], True)

        rel_info = self.driver._helpers.get_relationship_info(volume['name'])
        self.assertIsNotNone(rel_info)

        src_opts = self.driver._get_vdisk_params(volume['volume_type_id'])
        opt_cycle_period_seconds = str(
            src_opts.get('cycle_period_seconds'))
        self.assertEqual(opt_cycle_period_seconds,
                         rel_info['cycle_period_seconds'])
        self.assertEqual(storwize_const.GMCV_MULTI, rel_info['cycling_mode'])
        self.assertEqual(storwize_const.GLOBAL, rel_info['copy_type'])
        self.assertEqual(storwize_const.GMCV,
                         self.driver._get_volume_replicated_type(
                             self.ctxt, volume))
        self.assertEqual('master', rel_info['primary'])
        self.assertEqual(volume['name'], rel_info['master_vdisk_name'])
        self.assertEqual((storwize_const.REPLICA_CHG_VOL_PREFIX
                          + volume['name']),
                         rel_info['master_change_vdisk_name'])
        aux_vdisk_name = (storwize_const.REPLICA_AUX_VOL_PREFIX
                          + volume['name'])
        self.assertEqual(aux_vdisk_name,
                         rel_info['aux_vdisk_name'])
        self.assertEqual((storwize_const.REPLICA_CHG_VOL_PREFIX
                          + aux_vdisk_name),
                         rel_info['aux_change_vdisk_name'])

    def _validate_replic_vol_deletion(self, volume, isGMCV=False):
        self._assert_vol_exists(volume['name'], False)
        self._assert_vol_exists(
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], False)
        if isGMCV:
            # All change volumes should be deleted
            self._assert_vol_exists(
                storwize_const.REPLICA_CHG_VOL_PREFIX + volume['name'], False)
            self._assert_vol_exists(
                storwize_const.REPLICA_CHG_VOL_PREFIX +
                storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], False)
        rel_info = self.driver._helpers.get_relationship_info(volume['name'])
        self.assertIsNone(rel_info)

    @ddt.data(({'mirror_type': 'mm_type'}),
              ({'mirror_type': 'gm_type'}),
              ({'mirror_type': 'gmcv_default_type'}),
              ({'mirror_type': 'gmcv_with_cps900_type'}))
    def test_storwize_create_snapshot_volume_with_mirror_replica(self,
                                                                 vol_spec):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        rep_type = getattr(self, vol_spec['mirror_type'])

        # Create mirror replication volume.
        is_gmcv = True if "gmcv" in vol_spec['mirror_type'] else False
        vol1, model_update = self._create_test_volume(rep_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(vol1, isGMCV=is_gmcv)

        snap = testutils.create_snapshot(self.ctxt, vol1.id)
        self.driver.create_snapshot(snap)
        self._assert_vol_exists(snap['name'], True)
        vol2 = self._generate_vol_info(rep_type)
        model_update = self.driver.create_volume_from_snapshot(vol2, snap)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(vol2, isGMCV=is_gmcv)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.delete_volume(vol2)
        self.driver.delete_snapshot(snap)
        self.driver.delete_volume(vol1)

    @ddt.data((None, None),
              (None, SVC_TARGET_CHILD_POOL), (SVC_SOURCE_CHILD_POOL, None),
              (SVC_SOURCE_CHILD_POOL, SVC_TARGET_CHILD_POOL))
    @ddt.unpack
    def test_storwize_create_snapshot_gmcv_volume_with_childpool(
            self, svc_src_childpool, svc_tgt_childpool):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create gmcv replication volume with change volumes on child pools
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> gmcv',
                'drivers:storwize_svc_src_child_pool': svc_src_childpool,
                'drivers:storwize_svc_target_child_pool': svc_tgt_childpool}
        gmcv_childpool_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='test_gmcv_childpool_type')
        vol1, model_update = self._create_test_volume(gmcv_childpool_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(vol1, True)

        snap = testutils.create_snapshot(self.ctxt, vol1.id)
        self.driver.create_snapshot(snap)

        vol2 = self._generate_vol_info(gmcv_childpool_type)
        model_update = self.driver.create_volume_from_snapshot(vol2, snap)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(vol2, True)

        src_chg_vol_storage_pool = (
            svc_src_childpool if svc_src_childpool else _get_test_pool())
        tgt_chg_vol_storage_pool = (
            svc_tgt_childpool if svc_tgt_childpool else _get_test_pool())
        src_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               vol2.name)
        src_childpool_vols = self._get_pool_volumes(src_chg_vol_storage_pool)
        self.assertIn(src_change_vol_name, src_childpool_vols)

        tgt_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               storwize_const.REPLICA_AUX_VOL_PREFIX +
                               vol2.name)
        tgt_childpool_vols = self._get_pool_volumes(tgt_chg_vol_storage_pool)
        self.assertIn(tgt_change_vol_name, tgt_childpool_vols)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.delete_volume(vol2)
        self._validate_replic_vol_deletion(vol2, True)
        self.driver.delete_snapshot(snap)
        self.driver.delete_volume(vol1)
        self._validate_replic_vol_deletion(vol1, True)

    def test_storwize_create_cloned_volume_with_mirror_replica(self):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create a source metro mirror replication volume.
        src_volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        volume = self._generate_vol_info(self.mm_type)

        # Create a cloned volume from source volume.
        model_update = self.driver.create_cloned_volume(volume, src_volume)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.delete_volume(src_volume)
        self.driver.delete_volume(volume)
        # Create a source gmcv replication volume.
        src_volume, model_update = self._create_test_volume(
            self.gmcv_default_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        volume = self._generate_vol_info(self.gmcv_default_type)

        # Create a cloned volume from source volume.
        model_update = self.driver.create_cloned_volume(volume, src_volume)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.delete_volume(src_volume)
        self.driver.delete_volume(volume)

        # Create a source gmcv volume with specified cycle_period_seconds
        src_volume, model_update = self._create_test_volume(
            self.gmcv_with_cps600_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        volume = self._generate_vol_info(self.gmcv_with_cps600_type)

        # Create a cloned volume from source volume.
        model_update = self.driver.create_cloned_volume(volume, src_volume)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.delete_volume(src_volume)
        self.driver.delete_volume(volume)

    @ddt.data((None, None),
              (None, SVC_TARGET_CHILD_POOL), (SVC_SOURCE_CHILD_POOL, None),
              (SVC_SOURCE_CHILD_POOL, SVC_TARGET_CHILD_POOL))
    @ddt.unpack
    def test_storwize_create_cloned_volume_from_gmcv_with_childpool(
            self, svc_src_childpool, svc_tgt_childpool):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create gmcv volume with change volumes on child pools
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> gmcv',
                'drivers:storwize_svc_src_child_pool': svc_src_childpool,
                'drivers:storwize_svc_target_child_pool': svc_tgt_childpool}
        gmcv_childpool_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='test_gmcv_childpool_type')
        src_volume, model_update = self._create_test_volume(
            gmcv_childpool_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(src_volume, True)

        # Create a cloned volume from source volume.
        volume = self._generate_vol_info(gmcv_childpool_type)
        model_update = self.driver.create_cloned_volume(volume, src_volume)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)

        src_chg_vol_storage_pool = (
            svc_src_childpool if svc_src_childpool else _get_test_pool())
        tgt_chg_vol_storage_pool = (
            svc_tgt_childpool if svc_tgt_childpool else _get_test_pool())
        src_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               volume.name)
        src_childpool_vols = self._get_pool_volumes(src_chg_vol_storage_pool)
        self.assertIn(src_change_vol_name, src_childpool_vols)

        tgt_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               storwize_const.REPLICA_AUX_VOL_PREFIX +
                               volume.name)
        tgt_childpool_vols = self._get_pool_volumes(tgt_chg_vol_storage_pool)
        self.assertIn(tgt_change_vol_name, tgt_childpool_vols)

        if self.USESIM:
            self.sim.error_injection('lsfcmap', 'speed_up')
        self.driver.delete_volume(src_volume)
        self._validate_replic_vol_deletion(src_volume, True)
        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(src_volume, True)

    @ddt.data(({'replication_enabled': '<is> True',
                'replication_type': '<in> global'},
               {'replication_enabled': '<is> True',
                'replication_type': '<in> metro'}),
              ({'replication_enabled': '<is> True',
                'replication_type': '<in> metro'},
               {'replication_enabled': '<is> True',
                'replication_type': '<in> global'}))
    @ddt.unpack
    def test_storwize_retype_invalid_replication(self, old_opts, new_opts):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}
        old_type = self._create_replica_volume_type(
            False, opts=old_opts, vol_type_name='test_old_type')

        volume, model_update = self._create_test_volume(old_type)
        new_type = self._create_replica_volume_type(
            False, opts=new_opts, vol_type_name='test_new_type')
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, new_type['id'], old_type['id'])
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, self.ctxt,
                          volume, new_type, diff, host)

    @ddt.data(({'replication_enabled': '<is> True',
                'replication_type': '<in> metro'},
               {'mirror_pool': 'openstack1'}),
              ({'mirror_pool': 'openstack1'},
               {'mirror_pool': 'openstack1',
                'replication_enabled': '<is> True',
                'replication_type': '<in> metro'}),
              ({'replication_enabled': '<is> False'},
               {'mirror_pool': 'openstack1',
                'replication_enabled': '<is> True',
                'replication_type': '<in> metro'}))
    @ddt.unpack
    def test_storwize_retype_valid_replication(self, old_opts, new_opts):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}
        old_type = self._create_replica_volume_type(
            False, opts=old_opts, vol_type_name='test_old_type')

        volume, model_update = self._create_test_volume(old_type)
        new_type = self._create_replica_volume_type(
            False, opts=new_opts, vol_type_name='test_new_type')
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, new_type['id'], old_type['id'])
        self.driver.retype(self.ctxt, volume,
                           new_type, diff, host)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_volume_io_group')
    def test_storwize_svc_retype_global_mirror_volume_to_thin(self,
                                                              get_vol_io_grp):
        self.driver.do_setup(self.ctxt)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#openstack',
                'capabilities': cap}
        ctxt = context.get_admin_context()

        get_vol_io_grp.return_value = 0

        type_name = 'rep_global_none'
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> global',
                'drivers:rsize': '-1',
                'compression': 'False'}
        type_ref = volume_types.create(self.ctxt, type_name, spec)
        vol_type1 = objects.VolumeType.get_by_id(self.ctxt, type_ref['id'])

        type_name = 'rep_global_thin'
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> global',
                'drivers:rsize': '2',
                'compression': 'False'}
        type_ref = volume_types.create(self.ctxt, type_name, spec)
        vol_type2 = objects.VolumeType.get_by_id(self.ctxt,
                                                 type_ref['id'])

        diff, _equal = volume_types.volume_types_diff(ctxt, vol_type1.id,
                                                      vol_type2.id)

        # Create test volume with volume type with rsize as -1
        vol1, model_update = self._create_test_volume(vol_type1)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        vol1['status'] = 'available'

        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(1, len(copies))
        self.driver.retype(self.ctxt, vol1, vol_type2, diff, host)
        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(2, len(copies))
        get_vol_io_grp.assert_called_once_with(vol1.name)
        self.driver.delete_volume(vol1)

    def test_storwize_svc_retype_global_mirror_volume_to_none(self):
        self.driver.do_setup(self.ctxt)
        loc = ('StorwizeSVCDriver:' + self.driver._state['system_id'] +
               ':openstack')
        cap = {'location_info': loc, 'extent_size': '128'}
        self.driver._stats = {'location_info': loc}
        host = {'host': 'openstack@svc#openstack',
                'capabilities': cap}
        ctxt = context.get_admin_context()

        type_name = 'rep_global_thin'
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> global',
                'drivers:rsize': '2',
                'compression': 'False'}
        type_ref = volume_types.create(self.ctxt, type_name, spec)
        vol_type1 = objects.VolumeType.get_by_id(self.ctxt, type_ref['id'])

        type_name = 'rep_global_none'
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> global',
                'drivers:rsize': '-1',
                'compression': 'False'}
        type_ref = volume_types.create(self.ctxt, type_name, spec)
        vol_type2 = objects.VolumeType.get_by_id(self.ctxt,
                                                 type_ref['id'])

        diff, _equal = volume_types.volume_types_diff(ctxt, vol_type1.id,
                                                      vol_type2.id)

        # Create test volume with volume type with rsize as 2
        vol1, model_update = self._create_test_volume(vol_type1)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        vol1['status'] = 'available'

        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(1, len(copies))
        self.driver.retype(self.ctxt, vol1, vol_type2, diff, host)
        copies = self.driver._helpers.lsvdiskcopy(vol1.name)
        self.assertEqual(2, len(copies))
        self.driver.delete_volume(vol1)

    def test_storwize_retype_from_mirror_to_none_replication(self):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        host = {'host': 'openstack@svc#openstack'}

        volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.mm_type['id'], self.gm_type['id'])
        # Change the mirror type from mm to gm
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, self.ctxt,
                          volume, self.gm_type, diff, host)

        # Retype from mm to gmcv
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.mm_type['id'], self.gmcv_with_cps600_type['id'])
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, self.ctxt,
                          volume, self.gmcv_with_cps600_type, diff, host)

        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.non_replica_type['id'], self.mm_type['id'])
        # Retype from mm to non-replica
        retyped, model_update = self.driver.retype(
            self.ctxt, volume, self.non_replica_type, diff, host)
        self.assertEqual(fields.ReplicationStatus.DISABLED,
                         model_update['replication_status'])
        self._assert_vol_exists(
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], False)

        self.driver.delete_volume(volume)
        self._assert_vol_exists(volume['name'], False)
        rel_info = self.driver._helpers.get_relationship_info(volume['name'])
        self.assertIsNone(rel_info)

        # Create gmcv volume
        volume, model_update = self._create_test_volume(
            self.gmcv_with_cps900_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        # Retype from gmcv to gm
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.gmcv_with_cps900_type['id'], self.gm_type['id'])
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.retype, self.ctxt,
                          volume, self.gm_type, diff, host)
        # Retype from gmcv to non-replica
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.gmcv_with_cps900_type['id'],
            self.non_replica_type['id'])
        retyped, model_update = self.driver.retype(
            self.ctxt, volume, self.non_replica_type, diff, host)
        self.assertEqual(fields.ReplicationStatus.DISABLED,
                         model_update['replication_status'])
        # All change volumes should be deleted
        self._assert_vol_exists(
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], False)
        self._assert_vol_exists(
            storwize_const.REPLICA_CHG_VOL_PREFIX + volume['name'], False)
        self._assert_vol_exists(
            storwize_const.REPLICA_CHG_VOL_PREFIX +
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'], False)

        self.driver.delete_volume(volume)
        self._assert_vol_exists(volume['name'], False)
        rel_info = self.driver._helpers.get_relationship_info(volume['name'])
        self.assertIsNone(rel_info)

    def test_storwize_retype_from_none_to_mirror_replication(self):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        volume, model_update = self._create_test_volume(self.non_replica_type)
        self.assertEqual(fields.ReplicationStatus.NOT_CAPABLE,
                         model_update['replication_status'])

        # Retype to mm replica
        host = {'host': 'openstack@svc#openstack'}
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.non_replica_type['id'], self.mm_type['id'])
        retyped, model_update = self.driver.retype(
            self.ctxt, volume, self.mm_type, diff, host)
        volume['volume_type_id'] = self.mm_type['id']
        volume['volume_type'] = self.mm_type
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume)

        self.driver.delete_volume(volume)

        # Create non-replica volume
        volume, model_update = self._create_test_volume(self.non_replica_type)
        self.assertEqual(fields.ReplicationStatus.NOT_CAPABLE,
                         model_update['replication_status'])
        # Retype to gmcv replica
        host = {'host': 'openstack@svc#openstack'}
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.non_replica_type['id'],
            self.gmcv_with_cps900_type['id'])
        retyped, model_update = self.driver.retype(
            self.ctxt, volume, self.gmcv_with_cps900_type, diff, host)
        volume['volume_type_id'] = self.gmcv_with_cps900_type['id']
        volume['volume_type'] = self.gmcv_with_cps900_type
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)

        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume, True)

    @ddt.data((None, None),
              (None, SVC_TARGET_CHILD_POOL), (SVC_SOURCE_CHILD_POOL, None),
              (SVC_SOURCE_CHILD_POOL, SVC_TARGET_CHILD_POOL))
    @ddt.unpack
    def test_storwize_retype_from_none_to_gmcv_with_childpool(
            self, svc_src_childpool, svc_tgt_childpool):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create non-replica volume
        volume, model_update = self._create_test_volume(self.non_replica_type)
        self.assertEqual(fields.ReplicationStatus.NOT_CAPABLE,
                         model_update['replication_status'])

        # Retype to gmcv with childpool
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> gmcv',
                'drivers:storwize_svc_src_child_pool': svc_src_childpool,
                'drivers:storwize_svc_target_child_pool': svc_tgt_childpool}
        gmcv_childpool_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='test_gmcv_childpool_type')
        host = {'host': 'openstack@svc#openstack'}
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.non_replica_type['id'],
            gmcv_childpool_type['id'])
        retyped, model_update = self.driver.retype(
            self.ctxt, volume, gmcv_childpool_type, diff, host)
        volume['volume_type_id'] = gmcv_childpool_type['id']
        volume['volume_type'] = gmcv_childpool_type

        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)

        src_chg_vol_storage_pool = (
            svc_src_childpool if svc_src_childpool else _get_test_pool())
        tgt_chg_vol_storage_pool = (
            svc_tgt_childpool if svc_tgt_childpool else _get_test_pool())
        src_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               volume.name)
        src_childpool_vols = self._get_pool_volumes(src_chg_vol_storage_pool)
        self.assertIn(src_change_vol_name, src_childpool_vols)

        tgt_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               storwize_const.REPLICA_AUX_VOL_PREFIX +
                               volume.name)
        tgt_childpool_vols = self._get_pool_volumes(tgt_chg_vol_storage_pool)
        self.assertIn(tgt_change_vol_name, tgt_childpool_vols)

        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume, True)

    def test_storwize_retype_from_gmcv_to_gmcv_replication(self):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create gmcv default volume
        volume, model_update = self._create_test_volume(self.gmcv_default_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume, True)

        # Retype to gmcv with cycle_period_seconds 600 replica
        host = {'host': 'openstack@svc#openstack'}
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.gmcv_default_type['id'],
            self.gmcv_with_cps600_type['id'])
        self.driver.retype(self.ctxt, volume,
                           self.gmcv_with_cps600_type, diff, host)
        volume['volume_type_id'] = self.gmcv_with_cps600_type['id']
        volume['volume_type'] = self.gmcv_with_cps600_type
        self._validate_gmcv_vol_retype(volume)

        # Retype to gmcv with cycle_period_seconds 900 replica
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.gmcv_with_cps600_type['id'],
            self.gmcv_with_cps900_type['id'])
        self.driver.retype(self.ctxt, volume,
                           self.gmcv_with_cps900_type, diff, host)
        volume['volume_type_id'] = self.gmcv_with_cps900_type['id']
        volume['volume_type'] = self.gmcv_with_cps900_type
        self._validate_gmcv_vol_retype(volume)

        # Retype to gmcv with invalid cycle_period_seconds
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt, self.gmcv_with_cps600_type['id'],
            self.gmcv_with_cps86401_type['id'])
        self.assertRaises(exception.InvalidInput, self.driver.retype,
                          self.ctxt, volume, self.gmcv_with_cps86401_type,
                          diff, host)

        # Retype to gmcv default volume
        diff, _equal = volume_types.volume_types_diff(
            self.ctxt,
            self.gmcv_with_cps900_type['id'],
            self.gmcv_default_type['id'])
        self.driver.retype(self.ctxt, volume,
                           self.gmcv_default_type, diff, host)
        volume['volume_type_id'] = self.gmcv_default_type['id']
        volume['volume_type'] = self.gmcv_default_type
        self._validate_gmcv_vol_retype(volume)

        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume, True)

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'mkfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_prepare_fc_map')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'startfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'stop_relationship')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_relationship')
    def test_revert_to_snapshot_mirror_vol(self, start_relationship,
                                           stop_relationship, startfcmap,
                                           prepare_fc_map, mkfcmap):
        mkfcmap.side_effect = ['1']
        vol1 = self._generate_vol_info(self.gm_type,
                                       replication_status='enabled')
        snap1 = self._generate_snap_info(vol1.id)
        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_get_volume_replicated_type') as vol_rep_type:
            vol_rep_type.side_effect = [True, False]
            self.driver.revert_to_snapshot(self.ctxt, vol1, snap1)
            mkfcmap.assert_called_once_with(
                snap1.name, vol1.name, True,
                self.driver.configuration.storwize_svc_flashcopy_rate,
                self.driver.configuration.storwize_svc_clean_rate)
            prepare_fc_map.assert_called_once_with(
                '1', self.driver.configuration.storwize_svc_flashcopy_timeout,
                True)
            startfcmap.assert_called_once_with('1', True)
            self.assertEqual(fields.ReplicationStatus.ENABLED,
                             vol1.replication_status)
            stop_relationship.assert_called_once_with("volume-" + vol1.id,
                                                      access=False)
            start_relationship.assert_called_once_with("volume-" + vol1.id,
                                                       primary=None)

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'mkfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       '_prepare_fc_map')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'startfcmap')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_rccg_name_by_volume_name')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'stop_rccg')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_rccg')
    def test_revert_to_snapshot_mirror_vol_in_group(
            self, start_rccg, stop_rccg, get_rccg_name_by_volume_name,
            startfcmap, prepare_fc_map, mkfcmap):
        mkfcmap.side_effect = ['1']
        vol1 = self._generate_vol_info(self.gm_type,
                                       replication_status='enabled')
        snap1 = self._generate_snap_info(vol1.id)
        fake_rccg_name = "fake_rccg-1"
        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_get_volume_replicated_type') as vol_rep_type:
            vol_rep_type.side_effect = [True]
            get_rccg_name_by_volume_name.side_effect = [fake_rccg_name]
            self.driver.revert_to_snapshot(self.ctxt, vol1, snap1)
            mkfcmap.assert_called_once_with(
                snap1.name, vol1.name, True,
                self.driver.configuration.storwize_svc_flashcopy_rate,
                self.driver.configuration.storwize_svc_clean_rate)
            prepare_fc_map.assert_called_once_with(
                '1', self.driver.configuration.storwize_svc_flashcopy_timeout,
                True)
            startfcmap.assert_called_once_with('1', True)
            self.assertEqual(fields.ReplicationStatus.ENABLED,
                             vol1.replication_status)
            stop_rccg.assert_called_once_with(fake_rccg_name,
                                              access=False)
            start_rccg.assert_called_once_with(fake_rccg_name,
                                               primary=None)

    def test_storwize_extend_volume_replication(self):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create metro mirror replication volume.
        volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        self.driver.extend_volume(volume, '13')
        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 13)

        attrs = self.driver._aux_backend_helpers.get_vdisk_attributes(
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 13)

        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume)

        # Create gmcv replication volume.
        volume, model_update = self._create_test_volume(
            self.gmcv_with_cps900_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        self.driver.extend_volume(volume, 15)
        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 15)

        attrs = self.driver._aux_backend_helpers.get_vdisk_attributes(
            storwize_const.REPLICA_AUX_VOL_PREFIX + volume['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 15)

        attrs = self.driver._aux_backend_helpers.get_vdisk_attributes(
            storwize_const.REPLICA_CHG_VOL_PREFIX +
            storwize_const.REPLICA_AUX_VOL_PREFIX +
            volume['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 15)

        attrs = self.driver._helpers.get_vdisk_attributes(
            storwize_const.REPLICA_CHG_VOL_PREFIX + volume['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 15)

        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume)

    def test_storwize_extend_gmcv_volume_invalid(self):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Extend gmcv volume with thick_provisioning_support.
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> gmcv',
                'drivers:rsize': -1}

        gmcv_thick_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='test_gmcv_thik_type')
        gmcv_volume, model_update = self._create_test_volume(gmcv_thick_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        if self.USESIM:
            # tell expandvdisksize to fail while called extend_volume
            # because volume is fast formatting
            self.sim.error_injection('expandvdisksize', 'fast_formatting')
            self.assertRaises(exception.VolumeDriverException,
                              self.driver.extend_volume, gmcv_volume, 15)
            attrs = (
                self.driver._helpers.get_vdisk_attributes(gmcv_volume['name']))
            vol_size = int(attrs['capacity']) / units.Gi
            self.assertAlmostEqual(vol_size, 1)

        self.driver.delete_volume(gmcv_volume)
        self._validate_replic_vol_deletion(gmcv_volume)

    def test_storwize_extend_gmcv_volume_part_of_group(self):
        """Extend gmcv volume that added to group with replication."""
        # Create group with replication.
        group = self._create_test_rccg(self.rccg_type,
                                       [self.gmcv_default_type.id])
        rccg_name = self.driver._get_rccg_name(group)
        # Create gmcv volume
        volume, model_update = self._create_test_volume(
            self.gmcv_default_type)
        self._validate_replic_vol_creation(volume, True)
        rcrel = self.driver._helpers.get_relationship_info(volume.name)
        self.sim._rc_state_transition('wait', rcrel)
        # Add gmcv volume to group.
        add_vols = [volume]
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(
                self.ctxt, group, add_vols, [])
        self.assertEqual(
            rccg_name,
            self.driver._helpers.get_rccg_name_by_volume_name(volume.name))
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        self.assertEqual([{'id': volume.id, 'group_id': group.id}],
                         add_volumes_update)
        self.assertEqual([], remove_volumes_update)
        # Extend gmcv volume which is a part of group
        self.driver.extend_volume(volume, 2)
        attrs = self.driver._helpers.get_vdisk_attributes(volume['name'])
        vol_size = int(attrs['capacity']) / units.Gi
        self.assertAlmostEqual(vol_size, 2)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'extend_vdisk') as extend_vdisk:
            self.driver.extend_volume(volume, 3)
            extend_vdisk.assert_called_with(volume.name, 2)

        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume)

    def test_convert_global_mirror_volume_to_gmcv(self):
        """Test volume conversion from global to gmcv."""
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        # Create global mirror replication.
        gm_vol, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(gm_vol)
        rcrel = self.driver._helpers.get_relationship_info(gm_vol.name)
        self.assertEqual('none', rcrel['cycling_mode'])
        self.assertEqual('', rcrel['master_change_vdisk_name'])
        self.assertEqual('', rcrel['aux_change_vdisk_name'])

        # Validating volume conversion from global to gmcv by checking a few
        # property values of RC relationship
        target_vol = storwize_const.REPLICA_AUX_VOL_PREFIX + gm_vol.name
        master_change_vol_name = (
            storwize_const.REPLICA_CHG_VOL_PREFIX + gm_vol.name)
        aux_change_vol_name = (
            storwize_const.REPLICA_CHG_VOL_PREFIX + target_vol)
        size = 1
        self.driver._convert_global_mirror_volume_to_gmcv(gm_vol, target_vol,
                                                          size, rcrel)
        rcrel = self.driver._helpers.get_relationship_info(gm_vol.name)
        self.assertEqual('multi', rcrel['cycling_mode'])
        self.assertEqual(master_change_vol_name,
                         rcrel['master_change_vdisk_name'])
        self.assertEqual(aux_change_vol_name,
                         rcrel['aux_change_vdisk_name'])
        self.driver.delete_volume(gm_vol)
        self._validate_replic_vol_deletion(gm_vol)

        gm_vol, model_update = self._create_test_volume(self.gm_type)
        self._validate_replic_vol_creation(gm_vol)
        rcrel = self.driver._helpers.get_relationship_info(gm_vol.name)
        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'create_vdisk')) as create_vdisk:
            with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
                  'change_relationship_cyclingmode')
                  ) as ch_relationship_cyclingmode:
                target_vol = (
                    storwize_const.REPLICA_AUX_VOL_PREFIX + gm_vol.name)
                size = 1
                self.driver._convert_global_mirror_volume_to_gmcv(
                    gm_vol, target_vol, size, rcrel)
                create_vdisk.assert_called()
                self.assertEqual(2, create_vdisk.call_count)
                ch_relationship_cyclingmode.assert_called()
        self.driver.delete_volume(gm_vol)
        self._validate_replic_vol_deletion(gm_vol)

    def test_convert_global_mirror_volume_to_gmcv_part_of_group(self):
        """Test volume conversion from global to gmcv part of group."""
        group = self._create_test_rccg(self.rccg_type,
                                       [self.gm_type.id])
        rccg_name = self.driver._get_rccg_name(group)
        gm_vol, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(gm_vol)
        add_vols = [gm_vol]
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(
                self.ctxt, group, add_vols, [])
        self.assertEqual(
            rccg_name,
            self.driver._helpers.get_rccg_name_by_volume_name(gm_vol.name))
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'])
        self.assertEqual([{'id': gm_vol.id, 'group_id': group.id}],
                         add_volumes_update)
        self.assertEqual([], remove_volumes_update)
        rccg_info = self.driver._helpers.get_rccg_info(gm_vol.name)
        self.assertEqual('none', rccg_info['cycling_mode'])
        rcrel = self.driver._helpers.get_relationship_info(gm_vol.name)
        self.assertEqual('', rcrel['master_change_vdisk_name'])
        self.assertEqual('', rcrel['aux_change_vdisk_name'])
        # Validating volume conversion from global to gmcv by checking a few
        # property values of rccg and RC relationship
        target_vol = storwize_const.REPLICA_AUX_VOL_PREFIX + gm_vol.name
        master_change_vol_name = (
            storwize_const.REPLICA_CHG_VOL_PREFIX + gm_vol.name)
        aux_change_vol_name = (
            storwize_const.REPLICA_CHG_VOL_PREFIX + target_vol)
        size = 1
        self.driver._convert_global_mirror_volume_to_gmcv(
            gm_vol, target_vol, size, rcrel,
            rccg_name=rccg_name)
        rccg_info = self.driver._helpers.get_rccg_info(gm_vol.name)
        self.assertEqual('multi', rccg_info['cycling_mode'])
        rcrel = self.driver._helpers.get_relationship_info(gm_vol.name)
        self.assertEqual(master_change_vol_name,
                         rcrel['master_change_vdisk_name'])
        self.assertEqual(aux_change_vol_name,
                         rcrel['aux_change_vdisk_name'])
        self.driver.delete_volume(gm_vol)
        self._validate_replic_vol_deletion(gm_vol)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_rccg')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'change_consistgrp_cyclingmode')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'stop_rccg')
    def test_calls_in_convert_global_mirror_volume_to_gmcv_part_of_group(
            self, start_rccg, change_consistgrp_cyclingmode, stop_rccg):
        # Create global mirror replication.
        gm_vol, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(gm_vol)
        rcrel = self.driver._helpers.get_relationship_info(gm_vol.name)
        rccg_name = "fake_rccg_1"
        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'create_vdisk')) as create_vdisk:
            target_vol = (
                storwize_const.REPLICA_AUX_VOL_PREFIX + gm_vol.name)
            size = 1
            self.driver._convert_global_mirror_volume_to_gmcv(
                gm_vol, target_vol, size, rcrel,
                rccg_name=rccg_name)
            create_vdisk.assert_called()
            self.assertEqual(2, create_vdisk.call_count)
            stop_rccg.assert_called_once_with(rccg_name)
            change_consistgrp_cyclingmode.assert_called_once_with(rccg_name,
                                                                  'multi')
            start_rccg.assert_called_once_with(rccg_name)
        self.driver.delete_volume(gm_vol)
        self._validate_replic_vol_deletion(gm_vol)

    def test_storwize_manage_existing_mismatch_with_volume_replication(self):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create mm replication volume.
        rep_volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create non-replication volume.
        non_rep_volume, model_update = self._create_test_volume(
            self.non_replica_type)

        new_volume = self._generate_vol_info()

        ref = {'source-name': rep_volume['name']}
        new_volume['volume_type_id'] = self.non_replica_type['id']
        new_volume['volume_type'] = self.non_replica_type
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        ref = {'source-name': non_rep_volume['name']}
        new_volume['volume_type_id'] = self.mm_type['id']
        new_volume['volume_type'] = self.mm_type
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        ref = {'source-name': rep_volume['name']}
        new_volume['volume_type_id'] = self.gm_type['id']
        new_volume['volume_type'] = self.gm_type
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        ref = {'source-name': rep_volume['name']}
        new_volume['volume_type_id'] = self.gmcv_with_cps900_type['id']
        new_volume['volume_type'] = self.gmcv_with_cps900_type
        self.assertRaises(exception.ManageExistingVolumeTypeMismatch,
                          self.driver.manage_existing, new_volume, ref)

        self.driver.delete_volume(rep_volume)
        self.driver.delete_volume(new_volume)

        # Create gmcv default replication volume
        rep_volume, model_update = self._create_test_volume(
            self.gmcv_default_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        new_volume = self._generate_vol_info()
        ref = {'source-name': rep_volume['name']}
        new_volume['volume_type_id'] = self.gmcv_with_cps900_type['id']
        new_volume['volume_type'] = self.gmcv_with_cps900_type
        # manage existing gmcv volume with different cycle period seconds
        self.assertRaises(
            exception.ManageExistingVolumeTypeMismatch,
            self.driver.manage_existing,
            new_volume,
            ref)
        self.driver.delete_volume(rep_volume)
        self.driver.delete_volume(new_volume)

    def test_storwize_manage_existing_with_volume_replication(self):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create mm replication volume.
        rep_volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        uid_of_master = self._get_vdisk_uid(rep_volume['name'])
        uid_of_aux = self._get_vdisk_uid(
            storwize_const.REPLICA_AUX_VOL_PREFIX + rep_volume['name'])

        new_volume = self._generate_vol_info()
        ref = {'source-name': rep_volume['name']}
        new_volume['volume_type_id'] = self.mm_type['id']
        new_volume['volume_type'] = self.mm_type
        self.driver.manage_existing(new_volume, ref)

        # Check the uid of the volume which has been renamed.
        uid_of_master_volume = self._get_vdisk_uid(new_volume['name'])
        uid_of_aux_volume = self._get_vdisk_uid(
            storwize_const.REPLICA_AUX_VOL_PREFIX + new_volume['name'])
        self.assertEqual(uid_of_master, uid_of_master_volume)
        self.assertEqual(uid_of_aux, uid_of_aux_volume)

        self.driver.delete_volume(rep_volume)
        # Create gmcv replication volume.
        rep_volume, model_update = self._create_test_volume(
            self.gmcv_with_cps900_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        uid_of_master = self._get_vdisk_uid(rep_volume['name'])
        uid_of_master_change = self._get_vdisk_uid(
            storwize_const.REPLICA_CHG_VOL_PREFIX +
            rep_volume['name'])
        uid_of_aux = self._get_vdisk_uid(
            storwize_const.REPLICA_AUX_VOL_PREFIX +
            rep_volume['name'])
        uid_of_aux_change = self._get_vdisk_uid(
            storwize_const.REPLICA_CHG_VOL_PREFIX +
            storwize_const.REPLICA_AUX_VOL_PREFIX +
            rep_volume['name'])

        new_volume = self._generate_vol_info()
        ref = {'source-name': rep_volume['name']}
        new_volume['volume_type_id'] = self.gmcv_with_cps900_type['id']
        new_volume['volume_type'] = self.gmcv_with_cps900_type
        self.driver.manage_existing(new_volume, ref)

        # Check the uid of the volume which has been renamed.
        uid_of_new_master = self._get_vdisk_uid(new_volume['name'])
        uid_of_new_master_change = self._get_vdisk_uid(
            storwize_const.REPLICA_CHG_VOL_PREFIX +
            new_volume['name'])
        uid_of_new_aux = self._get_vdisk_uid(
            storwize_const.REPLICA_AUX_VOL_PREFIX +
            new_volume['name'])
        uid_of_new_aux_change = self._get_vdisk_uid(
            storwize_const.REPLICA_CHG_VOL_PREFIX +
            storwize_const.REPLICA_AUX_VOL_PREFIX +
            new_volume['name'])

        self.assertEqual(uid_of_master, uid_of_new_master)
        self.assertEqual(uid_of_aux, uid_of_new_aux)
        self.assertEqual(uid_of_master_change, uid_of_new_master_change)
        self.assertEqual(uid_of_aux_change, uid_of_new_aux_change)

        self.driver.delete_volume(rep_volume)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers, 'rename_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_relationship_info')
    def test_storwize_update_migrated_replication_volume(
            self, get_rp_info, rename_vdisk):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        # Create replication volume.
        backend_volume, model_update = self._create_test_volume(self.mm_type)
        volume, model_update = self._create_test_volume(self.mm_type)
        get_rp_info.side_effect = [{'aux_vdisk_name': 'aux_test'}]
        model_update = self.driver.update_migrated_volume(self.ctxt, volume,
                                                          backend_volume,
                                                          'available')
        aux_vol = (storwize_const.REPLICA_AUX_VOL_PREFIX + volume.name)
        rename_vdisk.assert_has_calls([mock.call(
            backend_volume.name, volume.name), mock.call('aux_test', aux_vol)])
        self.assertEqual({'_name_id': None}, model_update)

        rename_vdisk.reset_mock()
        rename_vdisk.side_effect = exception.VolumeBackendAPIException(
            data='foo')
        model_update = self.driver.update_migrated_volume(self.ctxt, volume,
                                                          backend_volume,
                                                          'available')
        self.assertEqual({'_name_id': backend_volume.id}, model_update)

    def test_storwize_delete_volume_with_mirror_replication(self):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create metro mirror replication.
        volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume)

        # Delete volume in non-failover state
        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume)

        # Create gmcv replication.
        gmcv_volume, model_update = self._create_test_volume(
            self.gmcv_with_cps600_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(gmcv_volume, True)

        # Delete gmcv volume in non-failover state
        self.driver.delete_volume(gmcv_volume)
        self._validate_replic_vol_deletion(gmcv_volume, True)

        volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(volume)

        non_replica_vol, model_update = self._create_test_volume(
            self.non_replica_type)
        self.assertEqual(fields.ReplicationStatus.NOT_CAPABLE,
                         model_update['replication_status'])

        gmcv_volume, model_update = self._create_test_volume(
            self.gmcv_with_cps600_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(gmcv_volume, True)

        volumes = [volume, non_replica_vol, gmcv_volume]
        # Delete volume in failover state
        self.driver.failover_host(
            self.ctxt, volumes, self.rep_target['backend_id'], [])
        # Delete non-replicate volume in a failover state
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.delete_volume,
                          non_replica_vol)

        # Delete replicate volume in failover state
        self.driver.delete_volume(volume)
        self._validate_replic_vol_deletion(volume)

        self.driver.delete_volume(gmcv_volume)
        self._validate_replic_vol_deletion(gmcv_volume, True)

        self.driver.failover_host(
            self.ctxt, volumes, 'default', [])
        self.driver.delete_volume(non_replica_vol)
        self._assert_vol_exists(non_replica_vol['name'], False)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_relationship')
    def test_delete_source_volume(self,
                                  delete_relationship,
                                  delete_vdisk):
        # Set replication target.

        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        fake_name = 'volume-%s' % fake.VOLUME_ID
        self.driver._helpers.delete_rc_volume(fake_name)
        self.assertFalse(delete_relationship.called)
        master_change_fake_name = (
            storwize_const.REPLICA_CHG_VOL_PREFIX + fake_name)
        calls = [mock.call(master_change_fake_name, force_delete=False,
                           force_unmap=True),
                 mock.call(fake_name, force_delete=False, force_unmap=True)]
        delete_vdisk.assert_has_calls(calls, any_order=True)
        self.assertEqual(2, delete_vdisk.call_count)

        rel_info = {'name': 'fake_rcrel',
                    'aux_vdisk_name': fake_name,
                    'master_vdisk_name': 'volume-%s' % fake.VOLUME_ID}
        self.driver._helpers.delete_rc_volume(fake_name, rel_info)
        delete_relationship.assert_called_once_with(fake_name,
                                                    rcrel=rel_info['name'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_relationship')
    def test_delete_target_volume(self,
                                  delete_relationship,
                                  delete_vdisk):
        # Set replication target.

        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        fake_name = 'aux_volume-%s' % fake.VOLUME_ID
        rel_info = {'name': 'fake_rcrel',
                    'aux_vdisk_name': fake_name,
                    'master_vdisk_name': 'volume-%s' % fake.VOLUME_ID}
        self.driver._aux_backend_helpers.delete_rc_volume(fake_name,
                                                          rel_info,
                                                          target_vol=True)
        delete_relationship.assert_called_with(fake_name,
                                               rcrel=rel_info['name'])
        target_change_fake_name = (
            storwize_const.REPLICA_CHG_VOL_PREFIX + fake_name)
        calls = [mock.call(target_change_fake_name, force_delete=False,
                           force_unmap=True),
                 mock.call(fake_name, force_delete=False, force_unmap=True)]
        delete_vdisk.assert_has_calls(calls, any_order=True)
        self.assertEqual(2, delete_vdisk.call_count)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_relationship')
    def test_delete_target_volume_no_relationship(self,
                                                  delete_relationship,
                                                  delete_vdisk):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        fake_name = 'aux_volume-%s' % fake.VOLUME_ID
        self.driver._aux_backend_helpers.delete_rc_volume(
            fake_name, target_vol=True, rel_info=None)
        self.assertFalse(delete_relationship.called)
        self.assertTrue(delete_vdisk.called)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_relationship')
    def test_delete_target_volume_fail(self,
                                       delete_relationship,
                                       delete_vdisk):
        fake_id = fake.VOLUME_ID
        fake_name = 'aux_volume-%s' % fake_id
        rel_info = {'name': 'fake_rcrel',
                    'aux_vdisk_name': fake_name,
                    'master_vdisk_name': 'volume-%s' % fake_id}
        delete_vdisk.side_effect = Exception
        self.assertRaises(exception.VolumeDriverException,
                          self.driver._aux_backend_helpers.delete_rc_volume,
                          fake_name, rel_info, target_vol=True)
        delete_relationship.assert_called_once_with(fake_name,
                                                    rcrel=rel_info['name'])

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_relationship_info')
    def test_get_target_volume_information(self, get_relationship_info):
        volume, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        fake_aux_volume_name = storwize_const.REPLICA_AUX_VOL_PREFIX + \
            volume.name
        rel_info = {'aux_vdisk_name': fake_aux_volume_name,
                    'master_vdisk_name': volume.name}
        get_relationship_info.return_value = rel_info
        target_volume, rel_info = (
            self.driver._helpers.get_target_volume_information(volume))
        self.assertEqual(target_volume, fake_aux_volume_name)
        self.assertIsNotNone(rel_info)
        get_relationship_info.assert_called_with(volume.name)

        get_relationship_info.return_value = None
        target_volume, rel_info = (
            self.driver._helpers.get_target_volume_information(volume))
        self.assertEqual(target_volume, fake_aux_volume_name)
        self.assertIsNone(rel_info)
        get_relationship_info.assert_called_with(volume.name)

    def test_is_replicated_volume_primary(self):
        volume, model_update = self._create_test_volume(self.gm_type)
        rel_info = self.driver._helpers.get_relationship_info(volume.name)
        flag = self.driver._helpers.is_replicated_volume_primary(volume,
                                                                 rel_info)
        self.assertEqual(flag, True)

        # GM volume with auxillary as Primary
        rel_info["primary"] = "aux"
        flag = self.driver._helpers.is_replicated_volume_primary(volume,
                                                                 rel_info)
        self.assertEqual(flag, False)

    @ddt.data((True, True, 1), (False, True, 2),
              (True, False, 2), (False, False, 2))
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_vdisk')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'delete_relationship')
    @ddt.unpack
    def test_retain_target_volume(self, target_vol, retain_aux_vol,
                                  call_count, delete_relationship,
                                  delete_vdisk):
        # Set replication target.
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        fake_name = 'aux_volume-%s' % fake.VOLUME_ID
        rel_info = {'name': 'fakercrel',
                    'aux_vdisk_name': fake_name,
                    'master_vdisk_name': 'volume-%s' % fake.VOLUME_ID}
        self.driver._aux_backend_helpers.delete_rc_volume(
            fake_name, rel_info, target_vol=target_vol,
            retain_aux_volume=retain_aux_vol)

        vol_name = fake_name
        change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX + vol_name)

        if rel_info:
            delete_relationship.assert_called_once_with(vol_name,
                                                        rcrel=rel_info['name'])

        calls = [mock.call(change_vol_name, force_delete=False,
                           force_unmap=True)]
        if (target_vol and not retain_aux_vol) or not target_vol:
            calls.extend([mock.call(vol_name, force_delete=False,
                                    force_unmap=True)])
        delete_vdisk.assert_has_calls(calls, any_order=True)
        self.assertEqual(call_count, delete_vdisk.call_count)

    def test_storwize_failover_host_backend_error(self):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create metro mirror replication.
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create gmcv replication.
        gmcv_vol, model_update = self._create_test_volume(
            self.gmcv_with_cps900_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        volumes = [mm_vol, gmcv_vol]

        self.driver._replica_enabled = False
        self.assertRaises(exception.UnableToFailOver,
                          self.driver.failover_host,
                          self.ctxt, volumes, self.rep_target['backend_id'],
                          [])
        self.driver._replica_enabled = True
        self.assertRaises(exception.InvalidReplicationTarget,
                          self.driver.failover_host,
                          self.ctxt, volumes, self.fake_target['backend_id'],
                          [])

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_system_info') as get_sys_info:
            get_sys_info.side_effect = [
                exception.VolumeBackendAPIException(data='CMMVC6071E'),
                exception.VolumeBackendAPIException(data='CMMVC6071E')]
            self.assertRaises(exception.UnableToFailOver,
                              self.driver.failover_host,
                              self.ctxt, volumes,
                              self.rep_target['backend_id'], [])

            self.driver._active_backend_id = self.rep_target['backend_id']
            self.assertRaises(exception.UnableToFailOver,
                              self.driver.failover_host,
                              self.ctxt, volumes, 'default', [])
        self.driver.delete_volume(mm_vol)
        self.driver.delete_volume(gmcv_vol)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_relationship_info')
    def test_failover_volume_relationship_error(self, get_relationship_info):
        # Create global mirror replication.
        gm_vol, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create gmcv replication.
        gmcv_vol, model_update = self._create_test_volume(
            self.gmcv_default_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        get_relationship_info.side_effect = [None,
                                             exception.VolumeDriverException,
                                             None,
                                             exception.VolumeDriverException]
        expected_list = [{'updates': {'replication_status':
                                      fields.ReplicationStatus.FAILOVER_ERROR,
                                      'status': 'error'},
                          'volume_id': gm_vol['id']},
                         {'updates': {'replication_status':
                                      fields.ReplicationStatus.FAILOVER_ERROR,
                                      'status': 'error'},
                          'volume_id': gmcv_vol['id']}
                         ]
        volumes_update = self.driver._failover_replica_volumes(
            self.ctxt, [gm_vol, gmcv_vol])
        self.assertEqual(expected_list, volumes_update)

        volumes_update = self.driver._failover_replica_volumes(
            self.ctxt, [gm_vol, gmcv_vol])
        self.assertEqual(expected_list, volumes_update)

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_update_volume_stats')
    def test_storwize_failover_host_replica_volumes(self,
                                                    update_volume_stats):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create metro mirror replication.
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create global replication volume.
        gm_vol, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        gm_vol['status'] = 'in-use'

        # Create global replication volume.
        gm_vol1, model_update = self._create_test_volume(self.gm_type)
        gm_vol1['status'] = 'in-use'
        gm_vol1['previous_status'] = 'in-use'

        gm_vol2, model_update = self._create_test_volume(self.gm_type)
        gm_vol2['status'] = 'in-use'
        gm_vol2['previous_status'] = 'available'

        # Create gmcv volume.
        gmcv_vol, model_update = self._create_test_volume(
            self.gmcv_with_cps600_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        gmcv_vol['status'] = 'available'
        gmcv_vol['previous_status'] = 'in-use'

        volumes = [mm_vol, gm_vol, gm_vol1, gm_vol2, gmcv_vol]
        expected_list = [{'updates': {'replication_status':
                                      fields.ReplicationStatus.FAILED_OVER},
                          'volume_id': mm_vol['id']},
                         {'updates': {'replication_status':
                                      fields.ReplicationStatus.FAILED_OVER},
                          'volume_id': gm_vol['id']},
                         {'updates': {'replication_status':
                                      fields.ReplicationStatus.FAILED_OVER},
                          'volume_id': gm_vol1['id']},
                         {'updates': {'replication_status':
                                      fields.ReplicationStatus.FAILED_OVER},
                          'volume_id': gm_vol2['id']},
                         {'updates': {'replication_status':
                                      fields.ReplicationStatus.FAILED_OVER},
                          'volume_id': gmcv_vol['id']}
                         ]

        group1 = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        group2 = self._create_test_rccg(self.rccg_type, [self.gm_type.id])
        mm_vol1, model_update = self._create_test_volume(
            self.mm_type, status='available')
        mm_vol2, model_update = self._create_test_volume(
            self.mm_type, status='in-use')
        gm_vol3, model_update = self._create_test_volume(
            self.gm_type, status='available', previous_status='in-use')
        ctxt = context.get_admin_context()
        self.db.volume_update(ctxt, mm_vol1['id'], {'group_id': group1.id})
        self.db.volume_update(ctxt, mm_vol2['id'], {'group_id': group1.id})
        self.db.volume_update(ctxt, gm_vol3['id'], {'group_id': group2.id})
        vols1 = [mm_vol1, mm_vol2]
        self.driver.update_group(self.ctxt, group1, vols1, [])
        mm_vol1.group = group1
        mm_vol2.group = group1
        group1.volumes = objects.VolumeList.get_all_by_generic_group(self.ctxt,
                                                                     group1.id)
        vols2 = [gm_vol3]
        self.driver.update_group(self.ctxt, group2, vols2, [])
        gm_vol3.group = group2
        group2.volumes = objects.VolumeList.get_all_by_generic_group(self.ctxt,
                                                                     group2.id)
        rccg_name = self.driver._get_rccg_name(group1)
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])
        rccg_name = self.driver._get_rccg_name(group2)
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])
        volumes.extend(vols1)
        volumes.extend(vols2)
        expected_list1 = [{'updates': {'replication_status':
                                       fields.ReplicationStatus.FAILED_OVER,
                                       'status': 'available'},
                           'volume_id': mm_vol1['id']},
                          {'updates': {'replication_status':
                                       fields.ReplicationStatus.FAILED_OVER,
                                       'status': 'in-use'},
                           'volume_id': mm_vol2['id']},
                          {'updates': {'replication_status':
                                       fields.ReplicationStatus.FAILED_OVER,
                                       'status': 'available'},
                           'volume_id': gm_vol3['id']}]
        expected_list.extend(expected_list1)
        grp_expected = [{'group_id': group1.id,
                         'updates':
                             {'replication_status':
                              fields.ReplicationStatus.FAILED_OVER,
                              'status': fields.GroupStatus.AVAILABLE}},
                        {'group_id': group2.id,
                         'updates':
                             {'replication_status':
                              fields.ReplicationStatus.FAILED_OVER,
                              'status': fields.GroupStatus.AVAILABLE}}
                        ]

        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, self.rep_target['backend_id'],
            [group1, group2])
        self.assertEqual(self.rep_target['backend_id'], target_id)
        self.assertEqual(expected_list, volume_list)
        self.assertEqual(grp_expected, groups_update)

        self.assertEqual(self.driver._active_backend_id, target_id)
        self.assertEqual(self.driver._aux_backend_helpers,
                         self.driver._helpers)
        self.assertEqual([self.driver._replica_target['pool_name']],
                         self.driver._get_backend_pools())
        self.assertEqual(self.driver._state, self.driver._aux_state)
        self.assertTrue(update_volume_stats.called)

        self.driver.delete_volume(gmcv_vol)

        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, None, [])
        self.assertEqual(self.rep_target['backend_id'], target_id)
        self.assertEqual([], volume_list)
        self.assertEqual([], groups_update)

        self.driver.delete_volume(mm_vol)
        self.driver.delete_volume(gm_vol)
        self.driver.delete_volume(gm_vol1)
        self.driver.delete_volume(gm_vol2)
        self.driver.delete_volume(gmcv_vol)
        self.driver.delete_group(self.ctxt, group1, vols1)
        self.driver.delete_group(self.ctxt, group2, vols2)

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_update_volume_stats')
    def test_storwize_failover_host_normal_volumes(self,
                                                   update_volume_stats):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create metro mirror replication.
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        mm_vol['status'] = 'error'

        # Create gmcv replication.
        gmcv_vol, model_update = self._create_test_volume(
            self.gmcv_with_cps600_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        gmcv_vol['status'] = 'error'

        # Create non-replication volume.
        non_replica_vol, model_update = self._create_test_volume(
            self.non_replica_type)
        self.assertEqual(fields.ReplicationStatus.NOT_CAPABLE,
                         model_update['replication_status'])
        non_replica_vol['status'] = 'error'

        volumes = [mm_vol, gmcv_vol, non_replica_vol]

        rep_data1 = json.dumps({'previous_status': mm_vol['status']})
        rep_data2 = json.dumps({'previous_status': gmcv_vol['status']})
        rep_data3 = json.dumps({'previous_status': non_replica_vol['status']})

        expected_list = [{'updates': {'status': 'error',
                                      'replication_driver_data': rep_data1},
                          'volume_id': mm_vol['id']},
                         {'updates': {'status': 'error',
                                      'replication_driver_data': rep_data2},
                          'volume_id': gmcv_vol['id']},
                         {'updates': {'status': 'error',
                                      'replication_driver_data': rep_data3},
                          'volume_id': non_replica_vol['id']},
                         ]

        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, self.rep_target['backend_id'], [])
        self.assertEqual(self.rep_target['backend_id'], target_id)
        self.assertEqual(expected_list, volume_list)
        self.assertEqual([], groups_update)

        self.assertEqual(self.driver._active_backend_id, target_id)
        self.assertEqual(self.driver._aux_backend_helpers,
                         self.driver._helpers)
        self.assertEqual([self.driver._replica_target['pool_name']],
                         self.driver._get_backend_pools())
        self.assertEqual(self.driver._state, self.driver._aux_state)
        self.assertTrue(update_volume_stats.called)

        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, None, [])
        self.assertEqual(self.rep_target['backend_id'], target_id)
        self.assertEqual([], volume_list)
        self.assertEqual([], groups_update)
        # Delete non-replicate volume in a failover state
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.delete_volume,
                          non_replica_vol)
        self.driver.failover_host(self.ctxt, volumes, 'default', [])
        self.driver.delete_volume(mm_vol)
        self.driver.delete_volume(gmcv_vol)
        self.driver.delete_volume(non_replica_vol)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_relationship')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'stop_relationship')
    def test_failover_host_by_force_access(self, stop_relationship,
                                           start_relationship):
        replica_obj = self.driver._get_replica_obj(storwize_const.METRO)
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        mm_vol_attrs = self.driver._helpers.get_vdisk_attributes(mm_vol.name)
        target_vol = storwize_const.REPLICA_AUX_VOL_PREFIX + mm_vol.name
        context = mock.Mock
        replica_obj.failover_volume_host(context, mm_vol)
        stop_relationship.assert_called_once_with(target_vol, access=True)
        calls = [mock.call(mm_vol.name, rcrel=mm_vol_attrs['RC_name']),
                 mock.call(target_vol, 'aux')]
        start_relationship.assert_has_calls(calls, any_order=True)
        self.assertEqual(2, start_relationship.call_count)

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_update_volume_stats')
    def test_storwize_failback_replica_volumes(self,
                                               update_volume_stats):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create metro mirror replication.
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create global mirror replication.
        gm_vol, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        gm_vol['status'] = 'in-use'
        gm_vol['previous_status'] = ''

        gm_vol1, model_update = self._create_test_volume(self.gm_type)
        gm_vol1['status'] = 'in-use'
        gm_vol1['previous_status'] = 'in-use'

        gm_vol2, model_update = self._create_test_volume(self.gm_type)
        gm_vol2['status'] = 'in-use'
        gm_vol2['previous_status'] = 'available'

        # Create gmcv replication.
        gmcv_vol, model_update = self._create_test_volume(
            self.gmcv_with_cps900_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        volumes = [mm_vol, gm_vol, gm_vol1, gm_vol2, gmcv_vol]
        failover_expect = [{'updates': {'replication_status':
                                        fields.ReplicationStatus.FAILED_OVER},
                            'volume_id': mm_vol['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.FAILED_OVER},
                            'volume_id': gm_vol['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.FAILED_OVER},
                            'volume_id': gm_vol1['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.FAILED_OVER},
                            'volume_id': gm_vol2['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.FAILED_OVER},
                            'volume_id': gmcv_vol['id']}
                           ]
        group1 = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        group2 = self._create_test_rccg(self.rccg_type, [self.gm_type.id])
        mm_vol1, model_update = self._create_test_volume(
            self.mm_type, status='available')
        mm_vol2, model_update = self._create_test_volume(
            self.mm_type, status='in-use')
        gm_vol3, model_update = self._create_test_volume(
            self.gm_type,
            status='available', previous_status='in-use')
        ctxt = context.get_admin_context()
        self.db.volume_update(ctxt, mm_vol1['id'], {'group_id': group1.id})
        self.db.volume_update(ctxt, mm_vol2['id'], {'group_id': group1.id})
        self.db.volume_update(ctxt, gm_vol3['id'], {'group_id': group2.id})
        vols1 = [mm_vol1, mm_vol2]
        self.driver.update_group(self.ctxt, group1, vols1, [])
        mm_vol1.group = group1
        mm_vol2.group = group1
        group1.volumes = objects.VolumeList.get_all_by_generic_group(self.ctxt,
                                                                     group1.id)
        vols2 = [gm_vol3]
        self.driver.update_group(self.ctxt, group2, vols2, [])
        gm_vol3.group = group2
        group2.volumes = objects.VolumeList.get_all_by_generic_group(self.ctxt,
                                                                     group2.id)
        rccg_name = self.driver._get_rccg_name(group1)
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])
        rccg_name = self.driver._get_rccg_name(group2)
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])
        volumes.extend(vols1)
        volumes.extend(vols2)
        expected_list1 = [{'updates': {'replication_status':
                                       fields.ReplicationStatus.FAILED_OVER,
                                       'status': 'available'},
                           'volume_id': mm_vol1['id']},
                          {'updates': {'replication_status':
                                       fields.ReplicationStatus.FAILED_OVER,
                                       'status': 'in-use'},
                           'volume_id': mm_vol2['id']},
                          {'updates': {'replication_status':
                                       fields.ReplicationStatus.FAILED_OVER,
                                       'status': 'available'},
                           'volume_id': gm_vol3['id']}]
        failover_expect.extend(expected_list1)
        grp_expected = [{'group_id': group1.id,
                         'updates':
                             {'replication_status':
                              fields.ReplicationStatus.FAILED_OVER,
                              'status': fields.GroupStatus.AVAILABLE}},
                        {'group_id': group2.id,
                         'updates':
                             {'replication_status':
                              fields.ReplicationStatus.FAILED_OVER,
                              'status': fields.GroupStatus.AVAILABLE}}
                        ]

        # Already failback
        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, 'default', [group1, group2])
        self.assertIsNone(target_id)
        self.assertEqual([], volume_list)
        self.assertEqual([], groups_update)

        # fail over operation
        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, self.rep_target['backend_id'],
            [group1, group2])
        self.assertEqual(self.rep_target['backend_id'], target_id)
        self.assertEqual(failover_expect, volume_list)
        self.assertEqual(grp_expected, groups_update)
        self.assertEqual(self.driver._state, self.driver._aux_state)
        self.assertTrue(update_volume_stats.called)

        mm_vol['status'] = 'available'
        mm_vol['previous_status'] = 'available'
        gm_vol['status'] = 'available'
        gm_vol['previous_status'] = 'in-use'
        gm_vol1['status'] = 'in-use'
        gm_vol1['previous_status'] = 'in-use'
        gm_vol2['status'] = 'available'
        gm_vol2['previous_status'] = 'in-use'
        gmcv_vol['status'] = 'available'
        gmcv_vol['previous_status'] = ''
        failback_expect = [{'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED},
                            'volume_id': mm_vol['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED},
                            'volume_id': gm_vol['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED},
                            'volume_id': gm_vol1['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED},
                            'volume_id': gm_vol2['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED},
                            'volume_id': gmcv_vol['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED,
                                        'status': 'available'},
                            'volume_id': mm_vol1['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED,
                                        'status': 'in-use'},
                            'volume_id': mm_vol2['id']},
                           {'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED,
                                        'status': 'available'},
                            'volume_id': gm_vol3['id']}]
        grp_expected = [{'group_id': group1.id,
                         'updates':
                             {'replication_status':
                              fields.ReplicationStatus.ENABLED,
                              'status': fields.GroupStatus.AVAILABLE}},
                        {'group_id': group2.id,
                         'updates':
                             {'replication_status':
                              fields.ReplicationStatus.ENABLED,
                              'status': fields.GroupStatus.AVAILABLE}}
                        ]
        # fail back operation
        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, 'default', [group1, group2])
        self.assertEqual('default', target_id)
        self.assertEqual(failback_expect, volume_list)
        self.assertEqual(grp_expected, groups_update)

        self.assertIsNone(self.driver._active_backend_id)
        self.assertEqual(SVC_POOLS, self.driver._get_backend_pools())
        self.assertEqual(self.driver._state, self.driver._master_state)
        self.assertTrue(update_volume_stats.called)
        self.driver.delete_volume(mm_vol)
        self.driver.delete_volume(gm_vol)
        self.driver.delete_volume(gm_vol1)
        self.driver.delete_volume(gm_vol2)
        self.driver.delete_volume(gmcv_vol)
        self.driver.delete_group(self.ctxt, group1, vols1)
        self.driver.delete_group(self.ctxt, group2, vols2)

    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_update_volume_stats')
    def test_storwize_failback_normal_volumes(self,
                                              update_volume_stats):

        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create replication volume.
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self.assertEqual('enabled', model_update['replication_status'])
        mm_vol['status'] = 'error'

        gm_vol, model_update = self._create_test_volume(self.gm_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        gm_vol['status'] = 'available'

        # Create gmcv replication.
        gmcv_vol, model_update = self._create_test_volume(
            self.gmcv_default_type)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        gmcv_vol['status'] = 'error'
        volumes = [mm_vol, gmcv_vol, gm_vol]

        rep_data0 = json.dumps({'previous_status': mm_vol['status']})
        rep_data1 = json.dumps({'previous_status': gmcv_vol['status']})

        failover_expect = [{'updates': {'replication_status':
                                        fields.ReplicationStatus.FAILED_OVER},
                            'volume_id': gm_vol['id']},
                           {'updates': {'status': 'error',
                                        'replication_driver_data': rep_data0},
                            'volume_id': mm_vol['id']},
                           {'updates': {'status': 'error',
                                        'replication_driver_data': rep_data1},
                            'volume_id': gmcv_vol['id']}]

        # Already failback
        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, 'default', [])
        self.assertIsNone(target_id)
        self.assertEqual([], volume_list)
        self.assertEqual([], groups_update)

        # fail over operation
        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, self.rep_target['backend_id'], [])
        self.assertEqual(self.rep_target['backend_id'], target_id)
        self.assertEqual(failover_expect, volume_list)
        self.assertEqual([], groups_update)
        self.assertEqual(self.driver._state, self.driver._aux_state)
        self.assertTrue(update_volume_stats.called)

        # fail back operation
        mm_vol['replication_driver_data'] = json.dumps(
            {'previous_status': 'error'})
        gmcv_vol['replication_driver_data'] = json.dumps(
            {'previous_status': 'error'})
        gm_vol['status'] = 'in-use'
        gm_vol['previous_status'] = 'in-use'
        failback_expect = [{'updates': {'replication_status':
                                        fields.ReplicationStatus.ENABLED},
                            'volume_id': gm_vol['id']},
                           {'updates': {'status': 'error',
                                        'replication_driver_data': ''},
                            'volume_id': mm_vol['id']},
                           {'updates': {'status': 'error',
                                        'replication_driver_data': ''},
                            'volume_id': gmcv_vol['id']}]
        target_id, volume_list, groups_update = self.driver.failover_host(
            self.ctxt, volumes, 'default', [])
        self.assertEqual('default', target_id)
        self.assertEqual(failback_expect, volume_list)
        self.assertEqual([], groups_update)
        self.assertIsNone(self.driver._active_backend_id)
        self.assertEqual(SVC_POOLS, self.driver._get_backend_pools())
        self.assertEqual(self.driver._state, self.driver._master_state)
        self.assertTrue(update_volume_stats.called)
        self.driver.delete_volume(mm_vol)
        self.driver.delete_volume(gmcv_vol)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_system_info')
    @mock.patch.object(storwize_rep.StorwizeSVCReplicationManager,
                       '_partnership_validate_create')
    def test_establish_partnership_with_local_sys(self, partnership_create,
                                                  get_system_info):
        get_system_info.side_effect = [{'system_name': 'storwize-svc-sim'},
                                       {'system_name': 'storwize-svc-sim'}]

        rep_mgr = self.driver._get_replica_mgr()
        rep_mgr.establish_target_partnership()
        self.assertFalse(partnership_create.called)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_system_info')
    def test_establish_target_partnership(self, get_system_info):
        source_system_name = 'storwize-svc-sim'
        target_system_name = 'aux-svc-sim'

        get_system_info.side_effect = [{'system_name': source_system_name},
                                       {'system_name': target_system_name}]

        rep_mgr = self.driver._get_replica_mgr()
        rep_mgr.establish_target_partnership()
        partner_info = self.driver._helpers.get_partnership_info(
            source_system_name)
        self.assertIsNotNone(partner_info)
        self.assertEqual(partner_info['name'], source_system_name)

        partner_info = self.driver._helpers.get_partnership_info(
            source_system_name)
        self.assertIsNotNone(partner_info)
        self.assertEqual(partner_info['name'], source_system_name)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_partnership_info')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'chpartnership')
    def test_start_partnership(self, chpartnership, get_partnership_info):
        get_partnership_info.side_effect = [
            None,
            {'partnership': 'fully_configured',
             'id': '0'},
            {'partnership': 'fully_configured_stopped',
             'id': '0'}]

        rep_mgr = self.driver._get_replica_mgr()
        rep_mgr._partnership_start(rep_mgr._master_helpers,
                                   'storwize-svc-sim')
        self.assertFalse(chpartnership.called)
        rep_mgr._partnership_start(rep_mgr._master_helpers,
                                   'storwize-svc-sim')
        self.assertFalse(chpartnership.called)

        rep_mgr._partnership_start(rep_mgr._master_helpers,
                                   'storwize-svc-sim')
        chpartnership.assert_called_once_with('0')

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'start_relationship')
    def test_sync_replica_volumes_with_aux(self, start_relationship):
        # Create metro mirror replication.
        mm_vol = self._generate_vol_info(self.mm_type)
        tgt_volume = storwize_const.REPLICA_AUX_VOL_PREFIX + mm_vol['name']

        # Create gmcv replication.
        gmcv_vol = self._generate_vol_info(self.gmcv_with_cps600_type)
        tgt_gmcv_volume = (storwize_const.REPLICA_AUX_VOL_PREFIX +
                           gmcv_vol['name'])
        volumes = [mm_vol, gmcv_vol]

        fake_info = {'volume': 'fake',
                     'master_vdisk_name': 'fake',
                     'aux_vdisk_name': 'fake',
                     'name': 'fake_rcrel'}
        sync_state = {'state': storwize_const.REP_CONSIS_SYNC,
                      'primary': 'fake'}
        sync_state.update(fake_info)

        sync_copying_state = {'state': storwize_const.REP_CONSIS_COPYING,
                              'primary': 'fake'}
        sync_copying_state.update(fake_info)

        disconn_state = {'state': storwize_const.REP_IDL_DISC,
                         'primary': 'master'}
        disconn_state.update(fake_info)
        stop_state = {'state': storwize_const.REP_CONSIS_STOP,
                      'primary': 'aux'}
        stop_state.update(fake_info)
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=None)):
            self.driver._sync_with_aux(self.ctxt, volumes)
            self.assertFalse(start_relationship.called)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=sync_state)):
            self.driver._sync_with_aux(self.ctxt, volumes)
            self.assertFalse(start_relationship.called)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=sync_copying_state)):
            self.driver._sync_with_aux(self.ctxt, volumes)
            self.assertFalse(start_relationship.called)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=disconn_state)):
            self.driver._sync_with_aux(self.ctxt, volumes)
            calls = [mock.call(tgt_volume, rcrel=fake_info['name']),
                     mock.call(tgt_gmcv_volume, rcrel=fake_info['name'])]
            start_relationship.assert_has_calls(calls, any_order=True)
            self.assertEqual(2, start_relationship.call_count)

        start_relationship.reset_mock()
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=stop_state)):
            self.driver._sync_with_aux(self.ctxt, volumes)
            calls = [mock.call(tgt_volume, primary='aux',
                               rcrel=fake_info['name']),
                     mock.call(tgt_gmcv_volume, primary='aux',
                               rcrel=fake_info['name'])]
            start_relationship.assert_has_calls(calls, any_order=True)
            self.assertEqual(2, start_relationship.call_count)
        self.driver.delete_volume(mm_vol)
        self.driver.delete_volume(gmcv_vol)

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_relationship_info')
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    def test_wait_replica_vol_ready(self, get_relationship_info):
        # Create metro mirror replication.
        mm_vol = self._generate_vol_info(self.mm_type)

        # Create gmcv replication.
        gmcv_vol = self._generate_vol_info(self.gmcv_with_cps900_type)

        fake_info = {'volume': 'fake',
                     'master_vdisk_name': 'fake',
                     'aux_vdisk_name': 'fake',
                     'primary': 'fake'}
        sync_state = {'state': storwize_const.REP_CONSIS_SYNC}
        sync_state.update(fake_info)
        sync_copying_state = {'state': storwize_const.REP_CONSIS_COPYING}
        sync_copying_state.update(fake_info)
        disconn_state = {'state': storwize_const.REP_IDL_DISC}
        disconn_state.update(fake_info)
        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=None)):
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver._wait_replica_vol_ready,
                              self.ctxt, mm_vol)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=None)):
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver._wait_replica_vol_ready,
                              self.ctxt, gmcv_vol)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=sync_state)):
            self.driver._wait_replica_vol_ready(self.ctxt, mm_vol)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=sync_copying_state)):
            self.driver._wait_replica_vol_ready(self.ctxt, gmcv_vol)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=disconn_state)):
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver._wait_replica_vol_ready,
                              self.ctxt, mm_vol)

        with mock.patch.object(storwize_svc_common.StorwizeHelpers,
                               'get_relationship_info',
                               mock.Mock(return_value=disconn_state)):
            self.assertRaises(exception.VolumeBackendAPIException,
                              self.driver._wait_replica_vol_ready,
                              self.ctxt, gmcv_vol)

    # Replication groups operation
    def test_storwize_rep_group_create(self):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        # create group in db
        group = testutils.create_group(self.ctxt,
                                       volume_type_ids=[self.mm_type.id],
                                       group_type_id=self.rccg_type.id)

        model_update = self.driver.create_group(self.ctxt, group)
        self.assertEqual(fields.GroupStatus.AVAILABLE, model_update['status'])
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        rccg_name = self.driver._get_rccg_name(group)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertEqual(rccg['name'], rccg_name)
        self.driver.delete_group(self.ctxt, group, [])

    def test_storwize_rep_group_delete(self):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        mm_vol2, model_update = self._create_test_volume(self.mm_type)
        vols = [mm_vol1, mm_vol2]
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        self.driver.update_group(self.ctxt, group, vols, [])
        (model_update, volumes_model_update) = self.driver.delete_group(
            self.ctxt, group, vols)
        for vol in vols:
            self.assertFalse(self.driver._helpers.is_vdisk_defined(vol.name))
        self.assertIsNone(self.driver._helpers.get_rccg(
            self.driver._get_rccg_name(group)))
        for vol_update in volumes_model_update:
            self.assertEqual(fields.GroupStatus.DELETED, vol_update['status'])
        self.assertEqual(fields.GroupStatus.DELETED, model_update['status'])

    @ddt.data(('state', 'inconsistent_stopped'), ('primary', 'aux'),
              ('cycling_mode', 'multi'), ('cycle_period_seconds', '500'))
    @ddt.unpack
    def test_storwize_rep_group_update_error(self, state, value):
        """Test group update error."""
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        mm_vol2, model_update = self._create_test_volume(self.mm_type)
        self.driver.update_group(self.ctxt, group, [mm_vol1], [])

        rccg_name = self.driver._get_rccg_name(group)
        temp_state = self.sim._rcconsistgrp_list[rccg_name][state]
        self.sim._rcconsistgrp_list[rccg_name][state] = value
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(
            self.ctxt, group, [mm_vol2], [])
        self.assertEqual(fields.GroupStatus.ERROR, model_update['status'])
        self.assertEqual([], add_volumes_update)
        self.assertEqual([], remove_volumes_update)
        self.sim._rcconsistgrp_list[rccg_name][state] = temp_state

        self.driver.delete_group(self.ctxt, group, [mm_vol1])

    def test_storwize_rep_group_update(self):
        """Test group update."""
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        gm_vol, model_update = self._create_test_volume(self.gm_type)
        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_update_rccg_properties') as update_rccg_prop:
            add_vols = [mm_vol, gm_vol]
            (model_update, add_volumes_update,
             remove_volumes_update) = self.driver.update_group(
                self.ctxt, group, add_vols, [])
            update_rccg_prop.assert_called()
            self.assertEqual(1, update_rccg_prop.call_count)
            self.assertEqual(fields.GroupStatus.ERROR, model_update['status'])
            self.assertEqual([{'id': mm_vol.id, 'group_id': group.id}],
                             add_volumes_update)
            self.assertEqual([], remove_volumes_update)
            self.driver.delete_group(self.ctxt, group, add_vols)

        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        rccg_name = self.driver._get_rccg_name(group)
        # Create metro mirror replication.
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        rcrel = self.driver._helpers.get_relationship_info(mm_vol1.name)
        self.sim._rc_state_transition('wait', rcrel)
        mm_vol2, model_update = self._create_test_volume(self.mm_type)
        rcrel = self.driver._helpers.get_relationship_info(mm_vol2.name)
        self.sim._rc_state_transition('wait', rcrel)
        mm_vol3, model_update = self._create_test_volume(self.mm_type)
        rcrel = self.driver._helpers.get_relationship_info(mm_vol3.name)
        self.sim._rc_state_transition('wait', rcrel)
        mm_vol4, model_update = self._create_test_volume(self.mm_type)
        rcrel = self.driver._helpers.get_relationship_info(mm_vol4.name)
        self.sim._rc_state_transition('wait', rcrel)

        with mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                               '_update_rccg_properties') as update_rccg_prop:
            add_vols = [mm_vol1, mm_vol2]
            (model_update, add_volumes_update,
             remove_volumes_update) = self.driver.update_group(
                self.ctxt, group, add_vols, [])
            update_rccg_prop.assert_called()
            self.assertEqual(2, update_rccg_prop.call_count)
            self.assertEqual(
                rccg_name,
                self.driver._helpers.get_rccg_info(mm_vol1.name)['name'])
            self.assertEqual(
                rccg_name,
                self.driver._helpers.get_rccg_info(mm_vol2.name)['name'])
            self.assertEqual(fields.GroupStatus.AVAILABLE,
                             model_update['status'])
            self.assertEqual([{'id': mm_vol1.id, 'group_id': group.id},
                              {'id': mm_vol2.id, 'group_id': group.id}],
                             add_volumes_update)
            self.assertEqual([], remove_volumes_update)

        add_vols = [mm_vol3, mm_vol4]
        rmv_vols = [mm_vol1, mm_vol2]
        (model_update, add_volumes_update,
         remove_volumes_update) = self.driver.update_group(
            self.ctxt, group, add_volumes=add_vols, remove_volumes=rmv_vols)
        # Validating the rccg property value from metadata of the volume
        # mm_vol3 that is added to a group
        self.assertEqual(rccg_name, mm_vol3.metadata['Consistency Group Name'])
        # Validating the rccg property value from metadata of the volume
        # mm_vol1 that is removed from a group
        exp_rccg_name = ''
        self.assertEqual(exp_rccg_name,
                         mm_vol1.metadata['Consistency Group Name'])

        self.assertIsNone(self.driver._helpers.get_rccg_info(mm_vol1.name))
        self.assertIsNone(self.driver._helpers.get_rccg_info(mm_vol2.name))
        self.assertEqual(
            rccg_name,
            self.driver._helpers.get_rccg_info(mm_vol3.name)['name'])
        self.assertEqual(
            rccg_name,
            self.driver._helpers.get_rccg_info(mm_vol4.name)['name'])
        self.assertEqual(fields.GroupStatus.AVAILABLE, model_update['status'])
        self.assertEqual([{'id': mm_vol3.id, 'group_id': group.id},
                          {'id': mm_vol4.id, 'group_id': group.id}],
                         add_volumes_update)
        self.assertEqual([{'id': mm_vol1.id, 'group_id': None},
                          {'id': mm_vol2.id, 'group_id': None}],
                         remove_volumes_update)
        self.driver.delete_group(self.ctxt, group, [mm_vol1, mm_vol2,
                                                    mm_vol3, mm_vol4])

    def test_storwize_rep_volume_rccg_properties_update(self):
        """Test rep volume rccg_properties update."""
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        # Create metro mirror replication.
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        # Validating the rccg property value of volume-metadata that updated
        # during the call to the function _update_rccg_properties by passing
        # the parameter 'group'
        self.driver._update_rccg_properties(self.ctxt, mm_vol, group)
        rccg_name = self.driver._get_rccg_name(group)
        self.assertEqual(rccg_name, mm_vol.metadata['Consistency Group Name'])
        # Validating the rccg property value of volume-metadata that updated
        # during the call to the function _update_rccg_properties by not
        # passing the parameter 'group'
        self.driver._update_rccg_properties(self.ctxt, mm_vol)
        exp_rccg_name = ""
        self.assertEqual(exp_rccg_name,
                         mm_vol.metadata['Consistency Group Name'])

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'startrcconsistgrp')
    def test_storwize_enable_replication_error(self, startrccg):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        rccg_name = self.driver._get_rccg_name(group)
        exp_mod_upd = {'replication_status': fields.ReplicationStatus.ENABLED}
        exp_mod_upd_err = {'replication_status':
                           fields.ReplicationStatus.ERROR}
        # enable replicaion on empty group
        model_update, volumes_update = self.driver.enable_replication(
            self.ctxt, group, [])
        self.assertEqual(exp_mod_upd_err, model_update)
        self.assertEqual([], volumes_update)
        self.assertFalse(startrccg.called)
        # Create metro mirror replication.
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        vols = [mm_vol1]
        self.driver.update_group(self.ctxt, group, vols, [])
        exp_vols_upd = [
            {'id': mm_vol1['id'],
             'replication_status': exp_mod_upd['replication_status']}]
        exp_vols_upd_err = [
            {'id': mm_vol1['id'],
             'replication_status': exp_mod_upd_err['replication_status']}]

        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'lsrcconsistgrp',
                               side_effect=[None, {'primary': 'master',
                                                   'relationship_count': '1'},
                                            {'primary': 'aux',
                                             'relationship_count': '1'},
                                            {'primary': 'master',
                                             'relationship_count': '1'}]):
            startrccg.side_effect = [
                None, None,
                exception.VolumeBackendAPIException(data='CMMVC6372W')]

            model_update, volumes_update = self.driver.enable_replication(
                self.ctxt, group, vols)
            self.assertEqual(exp_mod_upd_err, model_update)
            self.assertEqual(exp_vols_upd_err, volumes_update)
            self.assertFalse(startrccg.called)

            model_update, volumes_update = self.driver.enable_replication(
                self.ctxt, group, vols)
            self.assertEqual(exp_mod_upd, model_update)
            self.assertEqual(exp_vols_upd, volumes_update)
            startrccg.assert_called_with(rccg_name, 'master')

            model_update, volumes_update = self.driver.enable_replication(
                self.ctxt, group, vols)
            self.assertEqual(exp_mod_upd, model_update)
            self.assertEqual(exp_vols_upd, volumes_update)
            startrccg.assert_called_with(rccg_name, 'aux')

            model_update, volumes_update = self.driver.enable_replication(
                self.ctxt, group, vols)
            self.assertEqual(exp_mod_upd_err, model_update)
            self.assertEqual(exp_vols_upd_err, volumes_update)

    def test_storwize_enable_replication(self):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        # Create metro mirror replication.
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        mm_vol2, model_update = self._create_test_volume(self.mm_type)
        vols = [mm_vol1, mm_vol2]
        expect_model_update = {'replication_status':
                               fields.ReplicationStatus.ENABLED}
        expect_vols_update = [
            {'id': mm_vol1['id'],
             'replication_status': expect_model_update['replication_status']},
            {'id': mm_vol2['id'],
             'replication_status': expect_model_update['replication_status']}
        ]
        self.driver.update_group(self.ctxt, group, vols, [])
        model_update, volumes_update = self.driver.enable_replication(
            self.ctxt, group, vols)
        self.assertEqual(expect_model_update, model_update)
        self.assertEqual(expect_vols_update, volumes_update)
        rccg_name = self.driver._get_rccg_name(group)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertEqual(rccg['primary'], 'master')
        self.assertIn(rccg['state'], ['inconsistent_copying',
                                      'consistent_synchronized'])
        self.driver.delete_group(self.ctxt, group, vols)

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'stoprcconsistgrp')
    def test_storwize_disable_replication_error(self, stoprccg):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        rccg_name = self.driver._get_rccg_name(group)
        exp_mod_upd = {'replication_status': fields.ReplicationStatus.DISABLED}
        exp_mod_upd_err = {'replication_status':
                           fields.ReplicationStatus.ERROR}
        # disable replicarion on empty group
        model_update, volumes_update = self.driver.disable_replication(
            self.ctxt, group, [])
        self.assertEqual(exp_mod_upd_err, model_update)
        self.assertEqual([], volumes_update)
        self.assertFalse(stoprccg.called)
        # Create metro mirror replication.
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        vols = [mm_vol1]
        self.driver.update_group(self.ctxt, group, vols, [])
        exp_vols_upd = [
            {'id': mm_vol1['id'],
             'replication_status': exp_mod_upd['replication_status']}]
        exp_vols_upd_err = [
            {'id': mm_vol1['id'],
             'replication_status': exp_mod_upd_err['replication_status']}]

        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'lsrcconsistgrp',
                               side_effect=[None, {'name': rccg_name,
                                                   'relationship_count': '1'},
                                            {'name': rccg_name,
                                             'relationship_count': '1'}]):
            stoprccg.side_effect = [
                None, exception.VolumeBackendAPIException(data='CMMVC6372W')]

            model_update, volumes_update = self.driver.disable_replication(
                self.ctxt, group, vols)
            self.assertEqual(exp_mod_upd_err, model_update)
            self.assertEqual(exp_vols_upd_err, volumes_update)
            self.assertFalse(stoprccg.called)

            model_update, volumes_update = self.driver.disable_replication(
                self.ctxt, group, vols)
            self.assertEqual(exp_mod_upd, model_update)
            self.assertEqual(exp_vols_upd, volumes_update)
            stoprccg.assert_called_with(rccg_name, False)

            model_update, volumes_update = self.driver.disable_replication(
                self.ctxt, group, vols)
            self.assertEqual(exp_mod_upd_err, model_update)
            self.assertEqual(exp_vols_upd_err, volumes_update)

    def test_storwize_disable_replication(self):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        # Create metro mirror replication.
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        mm_vol2, model_update = self._create_test_volume(self.mm_type)
        vols = [mm_vol1, mm_vol2]
        expect_model_update = {'replication_status':
                               fields.ReplicationStatus.DISABLED}
        expect_vols_update = [
            {'id': mm_vol1['id'],
             'replication_status': expect_model_update['replication_status']},
            {'id': mm_vol2['id'],
             'replication_status': expect_model_update['replication_status']}
        ]
        self.driver.update_group(self.ctxt, group, vols, [])
        model_update, volumes_update = self.driver.disable_replication(
            self.ctxt, group, vols)
        self.assertEqual(expect_model_update, model_update)
        self.assertEqual(expect_vols_update, volumes_update)
        rccg_name = self.driver._get_rccg_name(group)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertIn(rccg['state'], ['inconsistent_stopped',
                                      'consistent_stopped'])
        self.driver.delete_group(self.ctxt, group, vols)

    def test_storwize_failover_group_error(self):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        # Create metro mirror replication.
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        mm_vol2, model_update = self._create_test_volume(self.mm_type)
        vols = [mm_vol1, mm_vol2]

        self.driver._replica_enabled = False
        self.assertRaises(exception.UnableToFailOver,
                          self.driver.failover_replication, self.ctxt, group,
                          vols, self.rep_target['backend_id'])
        self.driver._replica_enabled = True
        self.assertRaises(exception.InvalidReplicationTarget,
                          self.driver.failover_replication, self.ctxt, group,
                          vols, self.fake_target['backend_id'])

        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'stoprcconsistgrp') as stoprccg:
            stoprccg.side_effect = exception.VolumeBackendAPIException(
                data='CMMVC6071E')
            self.assertRaises(exception.UnableToFailOver,
                              self.driver.failover_replication, self.ctxt,
                              group, vols, self.rep_target['backend_id'])

        self.driver.delete_group(self.ctxt, group, vols)

    def test_storwize_failover_group_without_action(self):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        self.driver.update_group(self.ctxt, group, [mm_vol1], [])
        rccg_name = self.driver._get_rccg_name(group)
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])

        self.sim._rcconsistgrp_list[rccg_name]['primary'] = 'aux'
        model_update = self.driver._rep_grp_failover(self.ctxt, group)
        self.assertEqual(
            {'replication_status': fields.ReplicationStatus.FAILED_OVER},
            model_update)

        self.sim._rcconsistgrp_list[rccg_name]['primary'] = 'master'
        model_update = self.driver._rep_grp_failback(self.ctxt, group)
        self.assertEqual(
            {'replication_status': fields.ReplicationStatus.ENABLED},
            model_update)

        self.driver.delete_group(self.ctxt, group, [])

    @ddt.data(({'replication_enabled': '<is> True',
                'replication_type': '<in> metro'}, 'test_rep_metro'),
              ({'replication_enabled': '<is> True',
                'replication_type': '<in> global'}, 'test_rep_gm'))
    @ddt.unpack
    def test_storwize_failover_replica_group(self, spec, type_name):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        type_ref = volume_types.create(self.ctxt, type_name, spec)
        rep_type = objects.VolumeType.get_by_id(self.ctxt, type_ref['id'])
        group = self._create_test_rccg(self.rccg_type, [rep_type.id])
        vol1, model_update = self._create_test_volume(rep_type)
        vol2, model_update = self._create_test_volume(rep_type)
        vol2['status'] = 'in-use'
        vol3, model_update = self._create_test_volume(rep_type)
        vol3['status'] = 'available'
        vol3['previous_status'] = 'in-use'
        vols = [vol1, vol2, vol3]
        self.driver.update_group(self.ctxt, group, vols, [])
        rccg_name = self.driver._get_rccg_name(group)
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])
        expected_list = [{'id': vol1['id'],
                          'replication_status':
                              fields.ReplicationStatus.FAILED_OVER},
                         {'id': vol2['id'],
                          'replication_status':
                              fields.ReplicationStatus.FAILED_OVER},
                         {'id': vol3['id'],
                          'replication_status':
                              fields.ReplicationStatus.FAILED_OVER}]

        model_update, volumes_model_update = self.driver.failover_replication(
            self.ctxt, group, vols, self.rep_target['backend_id'])
        self.assertEqual(
            {'replication_status': fields.ReplicationStatus.FAILED_OVER},
            model_update)
        self.assertEqual(expected_list, volumes_model_update)
        self.assertIsNone(self.driver._active_backend_id)
        self.assertEqual(self.driver._master_backend_helpers,
                         self.driver._helpers)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertEqual('aux', rccg['primary'])

        group.replication_status = fields.ReplicationStatus.FAILED_OVER
        model_update, volumes_model_update = self.driver.failover_replication(
            self.ctxt, group, vols, None)
        self.assertEqual(
            {'replication_status': fields.ReplicationStatus.FAILED_OVER},
            model_update)
        self.assertEqual(expected_list, volumes_model_update)
        self.assertIsNone(self.driver._active_backend_id)
        self.assertEqual(self.driver._master_backend_helpers,
                         self.driver._helpers)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertEqual('aux', rccg['primary'])

        self.driver.delete_group(self.ctxt, group, vols)

    def test_failover_replica_group_by_force_access(self):
        self.driver.do_setup(self.ctxt)
        group = self._create_test_rccg(self.rccg_type, [self.mm_type.id])
        mm_vol1, model_update = self._create_test_volume(self.mm_type)
        self.driver.update_group(self.ctxt, group, [mm_vol1], [])
        rccg_name = self.driver._get_rccg_name(group)
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])
        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'startrcconsistgrp') as startrcconsistgrp:
            self.driver.failover_replication(self.ctxt, group, [mm_vol1], None)
            startrcconsistgrp.assert_called_once_with(rccg_name, 'aux')

        with mock.patch.object(storwize_svc_common.StorwizeSSH,
                               'stoprcconsistgrp') as stoprccg:
            stoprccg.side_effect = exception.VolumeBackendAPIException(
                data='CMMVC6071E')
            self.assertRaises(exception.UnableToFailOver,
                              self.driver.failover_replication, self.ctxt,
                              group, [mm_vol1], self.rep_target['backend_id'])
        self.driver.delete_group(self.ctxt, group, [mm_vol1])

    @ddt.data(({'replication_enabled': '<is> True',
                'replication_type': '<in> metro'}, 'test_rep_metro'),
              ({'replication_enabled': '<is> True',
                'replication_type': '<in> global'}, 'test_rep_gm_default'))
    @ddt.unpack
    def test_storwize_failback_replica_group(self, spec, type_name):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        type_ref = volume_types.create(self.ctxt, type_name, spec)
        rep_type = objects.VolumeType.get_by_id(self.ctxt, type_ref['id'])
        group = self._create_test_rccg(self.rccg_type, [rep_type.id])
        vol1, model_update = self._create_test_volume(rep_type)
        vol2, model_update = self._create_test_volume(rep_type)
        vol2['status'] = 'in-use'
        vol3, model_update = self._create_test_volume(rep_type)
        vol3['status'] = 'available'
        vol3['previous_status'] = 'in-use'
        vols = [vol1, vol2, vol3]
        self.driver.update_group(self.ctxt, group, vols, [])
        rccg_name = self.driver._get_rccg_name(group)
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])
        failover_expect = [{'id': vol1['id'],
                            'replication_status':
                                fields.ReplicationStatus.FAILED_OVER},
                           {'id': vol2['id'],
                            'replication_status':
                                fields.ReplicationStatus.FAILED_OVER},
                           {'id': vol3['id'],
                            'replication_status':
                                fields.ReplicationStatus.FAILED_OVER}]

        model_update, volumes_model_update = self.driver.failover_replication(
            self.ctxt, group, vols, self.rep_target['backend_id'])
        self.assertEqual(
            {'replication_status': fields.ReplicationStatus.FAILED_OVER},
            model_update)
        self.assertEqual(failover_expect, volumes_model_update)
        self.assertIsNone(self.driver._active_backend_id)
        self.assertEqual(self.driver._master_backend_helpers,
                         self.driver._helpers)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertEqual('aux', rccg['primary'])

        group.replication_status = fields.ReplicationStatus.FAILED_OVER
        model_update, volumes_model_update = self.driver.failover_replication(
            self.ctxt, group, vols, None)
        self.assertEqual(
            {'replication_status': fields.ReplicationStatus.FAILED_OVER},
            model_update)
        self.assertEqual(failover_expect, volumes_model_update)
        self.assertIsNone(self.driver._active_backend_id)
        self.assertEqual(self.driver._master_backend_helpers,
                         self.driver._helpers)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertEqual('aux', rccg['primary'])
        self.sim._rccg_state_transition('wait',
                                        self.sim._rcconsistgrp_list[rccg_name])

        vol1['status'] = 'available'
        vol1['previous_status'] = 'available'
        vol2['status'] = 'available'
        vol2['previous_status'] = 'in-use'
        vol3['status'] = 'in-use'
        vol3['previous_status'] = 'in-use'
        failback_expect = [{'id': vol1['id'],
                            'replication_status':
                                fields.ReplicationStatus.ENABLED},
                           {'id': vol2['id'],
                            'replication_status':
                                fields.ReplicationStatus.ENABLED},
                           {'id': vol3['id'],
                            'replication_status':
                                fields.ReplicationStatus.ENABLED}]
        self.driver._active_backend_id = self.rep_target['backend_id']

        model_update, volumes_model_update = self.driver.failover_replication(
            self.ctxt, group, vols, 'default')
        self.assertEqual(
            {'replication_status': fields.ReplicationStatus.ENABLED},
            model_update)
        self.assertEqual(failback_expect, volumes_model_update)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertEqual('master', rccg['primary'])

        group.replication_status = fields.ReplicationStatus.ENABLED
        model_update, volumes_model_update = self.driver.failover_replication(
            self.ctxt, group, vols, 'default')
        self.assertEqual(
            {'replication_status': fields.ReplicationStatus.ENABLED},
            model_update)
        self.assertEqual(failback_expect, volumes_model_update)
        rccg = self.driver._helpers.get_rccg(rccg_name)
        self.assertEqual('master', rccg['primary'])

        self.driver.delete_group(self.ctxt, group, vols)

    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'lsrcconsistgrp')
    @mock.patch.object(storwize_svc_common.StorwizeSSH,
                       'startrcconsistgrp')
    def test_sync_replica_group_with_aux(self, startrccg, lsrccg):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        rccg_name = 'fakerccg'

        sync_state = {'state': storwize_const.REP_CONSIS_SYNC,
                      'primary': 'fake', 'relationship_count': '1'}

        sync_copying_state = {'state': storwize_const.REP_CONSIS_COPYING,
                              'primary': 'fake', 'relationship_count': '1'}

        disconn_state = {'state': storwize_const.REP_IDL_DISC,
                         'primary': 'master', 'relationship_count': '1'}

        stop_state = {'state': storwize_const.REP_CONSIS_STOP,
                      'primary': 'aux', 'relationship_count': '1'}
        lsrccg.side_effect = [None, sync_state, sync_copying_state,
                              disconn_state, stop_state]

        self.driver._sync_with_aux_grp(self.ctxt, rccg_name)
        self.assertFalse(startrccg.called)

        self.driver._sync_with_aux_grp(self.ctxt, rccg_name)
        self.assertFalse(startrccg.called)

        self.driver._sync_with_aux_grp(self.ctxt, rccg_name)
        self.assertFalse(startrccg.called)

        self.driver._sync_with_aux_grp(self.ctxt, rccg_name)
        startrccg.assert_called_once_with(rccg_name, 'master')

        self.driver._sync_with_aux_grp(self.ctxt, rccg_name)
        startrccg.assert_called_with(rccg_name, 'aux')

    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'initialize_host_info')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'get_host_from_connector')
    @mock.patch.object(storwize_svc_common.StorwizeHelpers,
                       'check_vol_mapped_to_host')
    def test_get_map_info_from_connector(self, is_mapped, get_host_from_conn,
                                         initialize_host_info):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        is_mapped.side_effect = [False, False, False, False, False]
        get_host_from_conn.side_effect = [None, 'fake-host',
                                          'master-host',
                                          exception.VolumeBackendAPIException,
                                          'master-host', None, None,
                                          'aux-host']
        non_rep_vol, model_update = self._create_test_volume(
            self.non_replica_type)
        non_rep_vol['status'] = 'in-use'
        mm_vol, model_update = self._create_test_volume(self.mm_type)
        mm_vol['status'] = 'in-use'
        connector = {}

        (info, host_name, vol_name, backend_helper,
         node_state) = self.driver._get_map_info_from_connector(
            mm_vol, connector, iscsi=False)
        self.assertEqual(info, {})
        self.assertIsNone(host_name)
        self.assertEqual(vol_name, mm_vol.name)
        self.assertEqual(self.driver._master_backend_helpers,
                         backend_helper)
        self.assertEqual(self.driver._master_state,
                         node_state)
        initialize_host_info.assert_called()
        self.assertEqual(1, initialize_host_info.call_count)

        connector = {'host': 'storwize-svc-host',
                     'wwnns': ['20000090fa17311e', '20000090fa17311f'],
                     'wwpns': ['ff00000000000000', 'ff00000000000001'],
                     'initiator': 'iqn.1993-08.org.debian:01:eac5ccc1aaa'}
        self.assertRaises(exception.VolumeDriverException,
                          self.driver._get_map_info_from_connector,
                          non_rep_vol, connector, False)

        (info, host_name, vol_name, backend_helper,
         node_state) = self.driver._get_map_info_from_connector(
            non_rep_vol, connector, iscsi=False)
        self.assertEqual(info['driver_volume_type'], 'fibre_channel')
        self.assertEqual(host_name, 'fake-host')
        self.assertEqual(vol_name, non_rep_vol.name)
        self.assertEqual(self.driver._master_backend_helpers,
                         backend_helper)
        self.assertEqual(self.driver._master_state,
                         node_state)

        (info, host_name, vol_name, backend_helper,
         node_state) = self.driver._get_map_info_from_connector(
            mm_vol, connector, iscsi=True)
        self.assertEqual(info['driver_volume_type'], 'iscsi')
        self.assertIsNone(host_name)
        self.assertIsNone(vol_name)
        self.assertIsNone(backend_helper)
        self.assertIsNone(node_state)

        self.assertRaises(exception.VolumeDriverException,
                          self.driver._get_map_info_from_connector,
                          mm_vol, connector, False)

        (info, host_name, vol_name, backend_helper,
         node_state) = self.driver._get_map_info_from_connector(
            mm_vol, connector, iscsi=False)
        self.assertEqual(info['driver_volume_type'], 'fibre_channel')
        self.assertEqual(host_name, 'aux-host')
        self.assertEqual(vol_name,
                         storwize_const.REPLICA_AUX_VOL_PREFIX + mm_vol.name)
        self.assertEqual(self.driver._aux_backend_helpers, backend_helper)
        self.assertEqual(self.driver._aux_state, node_state)

    @ddt.data(({'consistent_group_replication_enabled': '<is> True'},
               {'replication_enabled': '<is> False'}),
              ({'consistent_group_replication_enabled': '<is> True'},
               {'replication_enabled': '<is> True',
                'replication_type': '<in> gmcv'}))
    @ddt.unpack
    def test_storwize_group_create_with_replication(self, grp_spec, vol_spec):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        """Test group create."""
        gr_type_ref = group_types.create(self.ctxt, 'gr_type', grp_spec)
        gr_type = objects.GroupType.get_by_id(self.ctxt, gr_type_ref['id'])
        vol_type_ref = volume_types.create(self.ctxt, 'vol_type', vol_spec)
        group = testutils.create_group(self.ctxt,
                                       group_type_id=gr_type.id,
                                       volume_type_ids=[vol_type_ref['id']])
        model_update = self.driver.create_group(self.ctxt, group)
        if vol_spec['replication_enabled'] == '<is> True':
            self.assertEqual(fields.GroupStatus.AVAILABLE,
                             model_update['status'])
        else:
            self.assertEqual(fields.GroupStatus.ERROR, model_update['status'])

    @ddt.data((None, None),
              (None, SVC_TARGET_CHILD_POOL), (SVC_SOURCE_CHILD_POOL, None),
              (SVC_SOURCE_CHILD_POOL, SVC_TARGET_CHILD_POOL))
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @ddt.unpack
    def test_create_group_from_src_with_gmcv_volume_with_childpool(
            self, svc_src_childpool, svc_tgt_childpool):
        # Set replication target
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)

        # Create new volume_type template for gmcv
        pool = _get_test_pool()
        spec = {'replication_enabled': '<is> True',
                'replication_type': '<in> gmcv',
                'drivers:storwize_svc_src_child_pool': svc_src_childpool,
                'drivers:storwize_svc_target_child_pool': svc_tgt_childpool}
        gmcv_childpool_type = self._create_replica_volume_type(
            False, opts=spec, vol_type_name='test_gmcv_childpool_type')

        # Create source group
        src_group = testutils.create_group(
            self.ctxt, volume_type_ids=[gmcv_childpool_type.id],
            group_type_id=self.rccg_type.id)
        model_update = self.driver.create_group(self.ctxt, src_group)
        self.assertEqual(fields.GroupStatus.AVAILABLE, model_update['status'])
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create gmcv volume
        src_volume = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=gmcv_childpool_type.id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))
        model_update = self.driver.create_volume(src_volume)
        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])
        self._validate_replic_vol_creation(src_volume, True)

        # Check source gmcv change volumes are created on child storage pools
        src_chg_vol_storage_pool = (
            svc_src_childpool if svc_src_childpool else _get_test_pool())
        tgt_chg_vol_storage_pool = (
            svc_tgt_childpool if svc_tgt_childpool else _get_test_pool())
        src_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               src_volume.name)
        src_childpool_vols = self._get_pool_volumes(src_chg_vol_storage_pool)
        self.assertIn(src_change_vol_name, src_childpool_vols)

        tgt_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               storwize_const.REPLICA_AUX_VOL_PREFIX +
                               src_volume.name)
        tgt_childpool_vols = self._get_pool_volumes(tgt_chg_vol_storage_pool)
        self.assertIn(tgt_change_vol_name, tgt_childpool_vols)

        rcrel = self.driver._helpers.get_relationship_info(src_volume.name)
        self.sim._rc_state_transition('wait', rcrel)
        src_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), src_group.id)

        # Add volume to source group
        add_volumes = [src_volume]
        delete_volumes = []
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                              src_group,
                                                              add_volumes,
                                                              delete_volumes)

        self.assertEqual(fields.GroupStatus.AVAILABLE, model_update['status'])
        self.assertEqual([{'id': src_volume.id, 'group_id': src_group.id}],
                         add_volumes_update)
        self.assertEqual([], remove_volumes_update)

        # Create clone group from source group
        clone_group = (
            testutils.create_group(
                self.ctxt, volume_type_ids=[gmcv_childpool_type.id],
                group_type_id=self.rccg_type.id))
        clone_volume = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=gmcv_childpool_type.id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), clone_group.id)

        with mock.patch.object(
                storwize_svc_common.StorwizeSVCCommonDriver,
                '_update_rccg_properties'):
            model_update, volumes_model_update = (
                self.driver.create_group_from_src(self.ctxt, clone_group,
                                                  clone_volumes, None, None,
                                                  src_group,
                                                  src_volumes))

            self.assertEqual(fields.GroupStatus.AVAILABLE,
                             model_update['status'])
            for vol_model_update in volumes_model_update:
                self.assertEqual(fields.VolumeStatus.AVAILABLE,
                                 vol_model_update['status'])

        src_chg_vol_storage_pool = (
            svc_src_childpool if svc_src_childpool else _get_test_pool())
        tgt_chg_vol_storage_pool = (
            svc_tgt_childpool if svc_tgt_childpool else _get_test_pool())
        src_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               clone_volume.name)
        src_childpool_vols = self._get_pool_volumes(src_chg_vol_storage_pool)
        self.assertIn(src_change_vol_name, src_childpool_vols)

        tgt_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               storwize_const.REPLICA_AUX_VOL_PREFIX +
                               clone_volume.name)
        tgt_childpool_vols = self._get_pool_volumes(tgt_chg_vol_storage_pool)
        self.assertIn(tgt_change_vol_name, tgt_childpool_vols)

        # Create group from source_group_snapshot
        group_from_src_group_snapshot = (
            testutils.create_group(
                self.ctxt, volume_type_ids=[gmcv_childpool_type.id],
                group_type_id=self.rccg_type.id))
        vol_from_snapshot = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=gmcv_childpool_type.id,
                                    group_id=group_from_src_group_snapshot.id,
                                    host='openstack@svc#%s' % pool))
        group_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), group_from_src_group_snapshot.id)

        group_snapshot, snapshots = self._create_group_snapshot(
            src_group.id, group_type_id=self.rccg_type.id)
        with mock.patch.object(
                storwize_svc_common.StorwizeSVCCommonDriver,
                '_update_rccg_properties'):
            model_update, volumes_model_update = (
                self.driver.create_group_from_src(
                    self.ctxt, group_from_src_group_snapshot, group_volumes,
                    group_snapshot, snapshots, None, None))

            self.assertEqual(fields.GroupStatus.AVAILABLE,
                             model_update['status'])
            for vol_model_update in volumes_model_update:
                self.assertEqual(fields.VolumeStatus.AVAILABLE,
                                 vol_model_update['status'])

        src_chg_vol_storage_pool = (
            svc_src_childpool if svc_src_childpool else _get_test_pool())
        tgt_chg_vol_storage_pool = (
            svc_tgt_childpool if svc_tgt_childpool else _get_test_pool())
        src_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               vol_from_snapshot.name)
        src_childpool_vols = self._get_pool_volumes(src_chg_vol_storage_pool)
        self.assertIn(src_change_vol_name, src_childpool_vols)

        tgt_change_vol_name = (storwize_const.REPLICA_CHG_VOL_PREFIX +
                               storwize_const.REPLICA_AUX_VOL_PREFIX +
                               vol_from_snapshot.name)
        tgt_childpool_vols = self._get_pool_volumes(tgt_chg_vol_storage_pool)
        self.assertIn(tgt_change_vol_name, tgt_childpool_vols)

        # Delete groups
        model_update = self.driver.delete_group(self.ctxt,
                                                group_from_src_group_snapshot,
                                                group_volumes)
        self.assertEqual(fields.GroupStatus.DELETED, model_update[0]['status'])

        with mock.patch(
                'cinder.volume.volume_utils.'
                'is_group_a_cg_snapshot_type') as is_group_a_cg_snapshot_type:
            is_group_a_cg_snapshot_type.return_value = True
            model_update = self.driver.delete_group_snapshot(self.ctxt,
                                                             group_snapshot,
                                                             snapshots)
            self.assertEqual(fields.GroupSnapshotStatus.DELETED,
                             model_update[0]['status'])
            for snapshot in model_update[1]:
                self.assertEqual(fields.SnapshotStatus.DELETED,
                                 snapshot['status'])

        model_update = self.driver.delete_group(self.ctxt,
                                                clone_group, clone_volumes)
        self.assertEqual(fields.GroupStatus.DELETED, model_update[0]['status'])

        model_update = self.driver.delete_group(self.ctxt, src_group,
                                                src_volumes)
        self.assertEqual(fields.GroupStatus.DELETED, model_update[0]['status'])

    @ddt.data(({'volume_type': 'mm'}),
              ({'volume_type': 'gm'}),
              ({'volume_type': 'gmcv'})
              )
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_update_rccg_properties')
    def test_create_group_from_src_grp(self, vol_spec, update_rccg_properties):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])

        pool = _get_test_pool()

        if vol_spec['volume_type'] == 'mm':
            vol_type_id = self.mm_type.id
        if vol_spec['volume_type'] == 'gm':
            vol_type_id = self.gm_type.id
        if vol_spec['volume_type'] == 'gmcv':
            vol_type_id = self.gmcv_default_type.id

        # create group in db
        src_group = testutils.create_group(
            self.ctxt, volume_type_ids=[vol_type_id],
            group_type_id=self.rccg_type.id)

        model_update = self.driver.create_group(self.ctxt, src_group)

        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create volumes in db
        src_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))
        src_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))

        self.driver.create_volume(src_vol1)
        self.driver.create_volume(src_vol2)
        src_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), src_group.id)

        add_volumes = [src_vol1, src_vol2]
        del_volumes = []

        # add volumes to group
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                              src_group,
                                                              add_volumes,
                                                              del_volumes)

        # Clone group for source group
        clone_group = (
            testutils.create_group(
                self.ctxt, volume_type_ids=[vol_type_id],
                group_type_id=self.rccg_type.id))

        # Create volumes in db
        clone_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), clone_group.id)

        # Create group from source group
        model_update, volumes_model_update = (
            self.driver.create_group_from_src(self.ctxt, clone_group,
                                              clone_volumes, None, None,
                                              src_group,
                                              src_volumes))
        self.assertEqual('available', model_update['status'])
        for each_vol in volumes_model_update:
            self.assertEqual('available', each_vol['status'])
        model_update = self.driver.delete_group(self.ctxt, clone_group,
                                                [clone_vol1, clone_vol2])
        self.assertEqual(fields.GroupStatus.DELETED, model_update[0]['status'])

        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'create_rccg')) as create_rccg:
            with ((mock.patch.object(
                   storwize_svc_common.StorwizeSVCCommonDriver,
                   '_update_replication_grp'))) as update_rep_group:
                update_rep_group.return_value = (dict(), dict(), dict())
                # Create group from source group
                model_update, volumes_model_update = (
                    self.driver.create_group_from_src(self.ctxt,
                                                      clone_group,
                                                      clone_volumes, None,
                                                      None, src_group,
                                                      src_volumes))
                create_rccg.assert_called()
                self.assertEqual(1, create_rccg.call_count)
                update_rep_group.assert_called()
                self.assertEqual(1, update_rep_group.call_count)
                model_update = self.driver.delete_group(
                    self.ctxt, clone_group, [clone_vol1, clone_vol2])
                self.assertEqual(fields.GroupStatus.DELETED,
                                 model_update[0]['status'])
                model_update = self.driver.delete_group(self.ctxt, src_group,
                                                        [src_vol1, src_vol2])
                self.assertEqual(fields.GroupStatus.DELETED,
                                 model_update[0]['status'])

    @ddt.data(({'volume_type': 'mm'}),
              ({'volume_type': 'gm'}),
              ({'volume_type': 'gmcv'})
              )
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch.object(storwize_svc_common.StorwizeSVCCommonDriver,
                       '_update_rccg_properties')
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_create_group_from_grp_snapshot(self, vol_spec,
                                            is_group_a_cg_snap_type,
                                            update_rccg_properties):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        is_group_a_cg_snap_type.return_value = False

        pool = _get_test_pool()

        if vol_spec['volume_type'] == 'mm':
            vol_type_id = self.mm_type.id
        if vol_spec['volume_type'] == 'gm':
            vol_type_id = self.gm_type.id
        if vol_spec['volume_type'] == 'gmcv':
            vol_type_id = self.gmcv_default_type.id

        # create group in db
        src_group = testutils.create_group(
            self.ctxt, volume_type_ids=[vol_type_id],
            group_type_id=self.rccg_type.id)

        model_update = self.driver.create_group(self.ctxt, src_group)

        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create volumes in db
        src_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))
        src_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))

        self.driver.create_volume(src_vol1)
        self.driver.create_volume(src_vol2)

        add_volumes = [src_vol1, src_vol2]
        del_volumes = []

        # add volumes to group
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                              src_group,
                                                              add_volumes,
                                                              del_volumes)

        # Clone group for source group
        clone_group = (
            testutils.create_group(
                self.ctxt, volume_type_ids=[vol_type_id],
                group_type_id=self.rccg_type.id))

        # Create volumes in db
        clone_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), clone_group.id)

        # Create group snapshot
        group_snapshot, snapshots = self._create_group_snapshot(
            src_group.id, group_type_id=self.rccg_type.id)

        # Create group from source as group snapshot
        model_update, volumes_model_update = (
            self.driver.create_group_from_src(self.ctxt, clone_group,
                                              clone_volumes,
                                              group_snapshot,
                                              snapshots, None, None))
        self.assertEqual(fields.GroupStatus.AVAILABLE,
                         model_update['status'],
                         "CG create from src created passed")

        rccg_name = self.driver._get_rccg_name(clone_group)
        for each_vol in volumes_model_update:
            self.assertEqual('available', each_vol['status'])
            self.assertEqual(rccg_name,
                             each_vol['metadata']['Consistency Group Name'])

        model_update = self.driver.delete_group(self.ctxt, clone_group,
                                                [clone_vol1, clone_vol2])
        self.assertEqual(fields.GroupStatus.DELETED, model_update[0]['status'])

        with (mock.patch.object(storwize_svc_common.StorwizeHelpers,
              'create_rccg')) as create_rccg:
            with ((mock.patch.object(
                    storwize_svc_common.StorwizeSVCCommonDriver,
                    '_update_replication_grp'))) as update_rep_group:
                update_rep_group.return_value = (dict(), dict(), dict())
                # Create group from source as group snapshot
                model_update, volumes_model_update = (
                    self.driver.create_group_from_src(self.ctxt,
                                                      clone_group,
                                                      clone_volumes,
                                                      group_snapshot,
                                                      snapshots, None,
                                                      None))
                create_rccg.assert_called()
                self.assertEqual(1, create_rccg.call_count)
                update_rep_group.assert_called()
                self.assertEqual(1, update_rep_group.call_count)

                model_update = (
                    self.driver.delete_group(self.ctxt, clone_group,
                                             [clone_vol1, clone_vol2]))
                self.assertEqual(fields.GroupStatus.DELETED,
                                 model_update[0]['status'])
                is_group_a_cg_snap_type.return_value = True
                model_update = (
                    self.driver.delete_group_snapshot(self.ctxt,
                                                      group_snapshot,
                                                      snapshots))
                self.assertEqual(fields.GroupSnapshotStatus.DELETED,
                                 model_update[0]['status'])
                for volume in model_update[1]:
                    self.assertEqual(fields.SnapshotStatus.DELETED,
                                     volume['status'])

    @ddt.data(({'volume_type': 'mm'}),
              ({'volume_type': 'gm'}),
              ({'volume_type': 'gmcv'})
              )
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    def test_create_group_from_src_grp_with_invalid_vol(self, vol_spec):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])

        pool = _get_test_pool()

        if vol_spec['volume_type'] == 'mm':
            vol_type_id = self.mm_type.id
        if vol_spec['volume_type'] == 'gm':
            vol_type_id = self.gm_type.id
        if vol_spec['volume_type'] == 'gmcv':
            vol_type_id = self.gmcv_default_type.id

        # create group in db
        src_group = testutils.create_group(
            self.ctxt, volume_type_ids=[vol_type_id],
            group_type_id=self.rccg_type.id)

        model_update = self.driver.create_group(self.ctxt, src_group)

        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create volumes in db
        src_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))
        src_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))

        src_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), src_group.id)

        add_volumes = [src_vol1, src_vol2]
        del_volumes = []

        # add volumes to group
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                              src_group,
                                                              add_volumes,
                                                              del_volumes)

        # Clone group for source group
        clone_group = (
            testutils.create_group(
                self.ctxt, volume_type_ids=[vol_type_id],
                group_type_id=self.rccg_type.id))

        # Create volumes in db
        clone_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), clone_group.id)

        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_group_from_src, self.ctxt,
                          clone_group, clone_volumes, None, None,
                          src_group, src_volumes)

        # Delete group
        model_update = self.driver.delete_group(self.ctxt, clone_group,
                                                [clone_vol1, clone_vol2])
        self.assertEqual(fields.GroupStatus.DELETED, model_update[0]['status'])

    @ddt.data(({'volume_type': 'mm'}),
              ({'volume_type': 'gm'}),
              ({'volume_type': 'gmcv'})
              )
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_create_grp_from_grp_snapshot_invalid(self, vol_spec,
                                                  is_group_a_cg_snap_type):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        is_group_a_cg_snap_type.return_value = False

        pool = _get_test_pool()

        if vol_spec['volume_type'] == 'mm':
            vol_type_id = self.mm_type.id
        if vol_spec['volume_type'] == 'gm':
            vol_type_id = self.gm_type.id
        if vol_spec['volume_type'] == 'gmcv':
            vol_type_id = self.gmcv_default_type.id

        # create group in db
        src_group = testutils.create_group(
            self.ctxt, volume_type_ids=[vol_type_id],
            group_type_id=self.rccg_type.id)

        model_update = self.driver.create_group(self.ctxt, src_group)

        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create volumes in db
        src_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))
        src_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))

        add_volumes = [src_vol1, src_vol2]
        del_volumes = []

        # add volumes to group
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                              src_group,
                                                              add_volumes,
                                                              del_volumes)

        # Clone group for source group
        clone_group = (
            testutils.create_group(
                self.ctxt, volume_type_ids=[vol_type_id],
                group_type_id=self.rccg_type.id))

        # Create volumes in db
        clone_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))

        # Exception raised while creating group snapshot
        self.assertRaises(exception.VolumeDriverException,
                          self._create_group_snapshot, src_group.id,
                          group_type_id=self.rccg_type.id)

        # Delete group
        model_update = self.driver.delete_group(self.ctxt, clone_group,
                                                [clone_vol1, clone_vol2])
        self.assertEqual(fields.GroupStatus.DELETED, model_update[0]['status'])

    @ddt.data(({'volume_type': 'mm'}),
              ({'volume_type': 'gm'}),
              ({'volume_type': 'gmcv'})
              )
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall',
                new=testutils.ZeroIntervalLoopingCall)
    @mock.patch('cinder.volume.volume_utils.is_group_a_cg_snapshot_type')
    def test_create_grp_from_empty_grp_snapshot_inv(self, vol_spec,
                                                    is_group_a_cg_snap_type):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        is_group_a_cg_snap_type.return_value = False

        pool = _get_test_pool()

        if vol_spec['volume_type'] == 'mm':
            vol_type_id = self.mm_type.id
        if vol_spec['volume_type'] == 'gm':
            vol_type_id = self.gm_type.id
        if vol_spec['volume_type'] == 'gmcv':
            vol_type_id = self.gmcv_default_type.id

        # create group in db
        src_group = testutils.create_group(
            self.ctxt, volume_type_ids=[vol_type_id],
            group_type_id=self.rccg_type.id)

        model_update = self.driver.create_group(self.ctxt, src_group)

        self.assertEqual(fields.ReplicationStatus.ENABLED,
                         model_update['replication_status'])

        # Create volumes in db
        src_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))
        src_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=src_group.id,
                                    host='openstack@svc#%s' % pool))

        self.driver.create_volume(src_vol1)
        self.driver.create_volume(src_vol2)

        add_volumes = [src_vol1, src_vol2]
        del_volumes = []

        # add volumes to group
        (model_update, add_volumes_update,
            remove_volumes_update) = self.driver.update_group(self.ctxt,
                                                              src_group,
                                                              add_volumes,
                                                              del_volumes)

        # Clone group for source group
        clone_group = (
            testutils.create_group(
                self.ctxt, volume_type_ids=[vol_type_id],
                group_type_id=self.rccg_type.id))

        # Create volumes in db
        clone_vol1 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_vol2 = (
            testutils.create_volume(self.ctxt,
                                    volume_type_id=vol_type_id,
                                    group_id=clone_group.id,
                                    host='openstack@svc#%s' % pool))
        clone_volumes = self.db.volume_get_all_by_generic_group(
            self.ctxt.elevated(), clone_group.id)

        # Create group snapshot
        group_snapshot, snapshots = self._create_group_snapshot_in_db(
            src_group.id)

        # Create group from source as group snapshot
        self.assertRaises(exception.VolumeDriverException,
                          self.driver.create_group_from_src, self.ctxt,
                          clone_group, clone_volumes, group_snapshot,
                          snapshots, None, None)

        # Delete group
        model_update = self.driver.delete_group(self.ctxt, clone_group,
                                                [clone_vol1, clone_vol2])
        self.assertEqual(fields.GroupStatus.DELETED, model_update[0]['status'])

    @ddt.data(({'volume_type': 'gm_type'}),
              ({'volume_type': 'mm_type'}),
              ({'volume_type': 'gmcv_default_type'}),
              ({'volume_type': 'non_replica_type'})
              )
    def test_update_replication_properties_on_create_volume(self, vol_spec):
        self.driver.configuration.set_override('replication_device',
                                               [self.rep_target])
        self.driver.do_setup(self.ctxt)
        vol_type = getattr(self, vol_spec['volume_type'])

        with mock.patch.object(
                storwize_svc_common.StorwizeSVCCommonDriver,
                '_update_replication_properties') as update_rep_properties:
            volume, model_update = self._create_test_volume(vol_type)
            if vol_type == self.non_replica_type:
                self.assertFalse(update_rep_properties.called)
            else:
                self.assertTrue(update_rep_properties.called)
            self.driver.delete_volume(volume)

        # Create metro mirror replication.
        volume, model_update = self._create_test_volume(vol_type)
        if 'metadata' in model_update:
            expected_sync_attr_value = ''
            expected_freeze_time = ''
            expected_primary = 'master'
            expected_mirroring_state = 'inconsistent_copying'
            self.assertEqual(model_update['metadata']['Sync'],
                             expected_sync_attr_value)
            self.assertEqual(model_update['metadata']['Freeze Time'],
                             expected_freeze_time)
            self.assertEqual(model_update['metadata']['Primary'],
                             expected_primary)
            self.assertEqual(model_update['metadata']['Mirroring State'],
                             expected_mirroring_state)
        self.driver.delete_volume(volume)
