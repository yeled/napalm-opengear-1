
"""Napalm driver for OpenGear Linux"""

import re
import socket

from napalm.base.helpers import textfsm_extractor
from napalm.base.helpers import mac, ip
from napalm.base.netmiko_helpers import netmiko_args
from napalm.base.utils import py23_compat

from napalm.base import NetworkDriver
from napalm.base.exceptions import ConnectionException

class OpenGearDriver(NetworkDriver):

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        self.device = None
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout

        if optional_args is None:
            optional_args = {}

        self.netmiko_optional_args = netmiko_args(optional_args)

    def _send_command(self, command):
        try:
            if isinstance(command, list):
                for cmd in command:
                    output = self.device.send_command(cmd)
                    if "% Invalid" not in output:
                        break
            else:
                output = self.device.send_command(command)
            return output
        except (socket.error, EOFError) as e:
            raise ConnectionException(str(e))

    def open(self):
        """Open a connection to the device."""
        self.device = self._netmiko_open(
            'linux',
            netmiko_optional_args=self.netmiko_optional_args,
        )

    def close(self):
        self._netmiko_close()

    def _get_sshkeys(self, username):
        # There's an ;echo because OpenGear don't put a \n at the end of authorized_keys

        sshkeys = []
        try:
            for line in self._send_command("cat /etc/config/users/" + username + "/.ssh/authorized_keys;echo").splitlines():
                if 'No such file' in line:
                    break
                sshkeys.append(line)
        except (RuntimeError, TypeError, NameError):
            pass
        return sshkeys

    def get_users(self):
        command = "config -g config.users|grep username"
        output = self._send_command(command)

        users = {}
        for line in output.splitlines():
            user = {
                'password': '',
                'sshkeys': [],
                'level': 0,
            }
            username = line.split()[1]
            # returns a list, or None
            user['sshkeys'] = self._get_sshkeys(username)

            users[username] = user

        return users

    def get_arp_table(self, vrf=u''):
        if vrf:
            msg = "VRF support has not been added for this getter on this platform."
            raise NotImplementedError(msg)

        command = "arp -van"
        arp_entries = self._send_command(command)
        arp_entries = textfsm_extractor(self, 'show_arp', arp_entries)

        table = []
        for idx, arp in enumerate(arp_entries):
            entry = {
                'interface': arp['interface'],
                'ip': ip(arp['ip']),
                'mac': mac(arp['mac']),
            }

            table.append(entry)

        return table

    def get_config(self, retrieve='all'):
        config = {
            'startup': u'Not implemented',
            'running': u'',
            'candidate': u'Not implemented',
        }

        if retrieve in ['all', 'running']:
            config['running'] = self._send_command("config -g config")

        return config

    def is_alive(self):
        null = chr(0)
        if self.device is None:
            return {'is_alive': False}

        try:
            # Try sending ASCII null byte to maintain the connection alive
            self.device.write_channel(null)
            return {'is_alive': self.device.remote_conn.transport.is_active()}
        except (socket.error, EOFError):
            # If unable to send, we can tell for sure that the connection is unusable
            return {'is_alive': False}

        return {'is_alive': False}

    def get_interfaces(self):
        iface_entries = ['eth0', 'eth1']

        interfaces = {}
        for i, entry in enumerate(iface_entries):
            iface_link = self._send_command("ip link show " + str(entry))

            # init interface entry with default values
            iface = {
                'is_enabled':   True,
                'is_up':        False,
                'description':  '',
                'mac_address':  '',
                'last_flapped': 0.0,  # in seconds
                'speed':        0,    # in megabits
            }
            for line in iface_link.splitlines():
                if 'state UP' in line:
                    iface['is_up'] = True
                elif 'link/ether' in line:
                    iface['mac_address'] = line.split()[1].strip()

            iface_eth = self._send_command("ethtool " + str(entry))
            for line in iface_eth.splitlines():
                if 'Speed:' in line:
                    iface['speed'] = line.split()[1].strip('Mb/s')

            interfaces[entry] = iface

        return interfaces

    def get_facts(self):
        facts = {
            'uptime': -1,
            'vendor': u'Unknown',
            'os_version': 'Unknown',
            'serial_number': 'Unknown',
            'model': 'Unknown',
            'hostname': 'Unknown',
            'fqdn': 'Unknown',
            'interface_list': [],
        }

        show_ver = self._send_command("cat /etc/version; config -g config.system.model")
        # OpenGear/IM72xx Version 4.3.1 75de795e -- Wed Sep 12 18:12:26 UTC 2018

        for line in show_ver.splitlines():
            if line.startswith('OpenGear/'):
                facts['vendor'] = line.split('/')[0].strip()
                facts['os_version'] = line.split()[2].strip()
            elif line.startswith('config.system.model'):
                facts['model'] = line.split()[1].strip()

        facts['serial_number'] = self._send_command("showserial")

        show_int = self._send_command("ifconfig |awk '/^[a-z]/ {print $1}'")
        for i, int in enumerate(show_int.split('\n')):
            facts['interface_list'].append(int)

        # get uptime from proc
        config = self._send_command("cat /proc/uptime")
        for line in config.splitlines():
            facts['uptime'] = line.split()[0]
            break

        # get hostname from running config
        config = self._send_command("config -g config.system.name")
        for line in config.splitlines():
            if line.startswith('config.system.name'):
                facts['fqdn'] = re.sub('^config.system.name ', '', line)
                facts['hostname'] = facts['fqdn'].split('.')[0]
                break

        return facts