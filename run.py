#!/usr/bin/env python3
"""Exports vCenter objects and imports them into Netbox via Python3"""

import asyncio
import atexit
from socket import gaierror
from datetime import date, datetime
from urllib.parse import quote_plus
from ipaddress import ip_network
import argparse
import aiodns
import requests
from pyVim.connect import SmartConnectNoSSL, Disconnect
from pyVmomi import vim
import settings
from logger import log
from templates.netbox import Templates, truncate, format_slug



def compare_dicts(dict1, dict2, dict1_name="d1", dict2_name="d2", path=""):
    """
    Compares the key value pairs of two dictionaries returns whether they match.

    dict1 keys and values are compared against dict2. dict2 may have keys and
    values that dict1 does not evaluate against.
    :param dict1: Primary dictionary to compare against :param dict2:
    :type dict1: dict
    :param dict2: Dictionary being compared to by :param dict1:
    :type dict2: dict
    :param dict1_name: Friendly name of :param dict1: for log messages
    :type dict1_name: str
    :param dict2_name: Friendly name of :param dict1: for log messages
    :type dict2_name: str
    :param path: Used to keep state of nested dictionary traversal
    :type path: str
    :return: `True` if :param dict2: matches all keys and values in :param dict2: else `False`
    :rtype: bool
    """
    # Setup paths to track key exploration. The path parameter is used to allow
    # recursive comparisions and track what's being compared.
    result = True
    for key in dict1.keys():
        dict1_path = "{}{}[{}]".format(dict1_name, path, key)
        dict2_path = "{}{}[{}]".format(dict2_name, path, key)
        if key not in dict2.keys():
            log.debug("%s not a valid key in %s.", dict1_path, dict2_path)
            result = False
        elif isinstance(dict1[key], dict) and isinstance(dict2[key], dict):
            log.debug(
                "%s and %s contain dictionary. Evaluating.", dict1_path,
                dict2_path
                )
            result = compare_dicts(
                dict1[key], dict2[key], dict1_name, dict2_name,
                path="[{}]".format(key)
                )
        elif isinstance(dict1[key], list) and isinstance(dict2[key], list):
            log.debug(
                "%s and %s key '%s' contains list. Validating dict1 items "
                "exist in dict2.", dict1_path, dict2_path, key
                )
            if not all([bool(item in dict2[key]) for item in dict1[key]]):
                log.debug(
                    "Mismatch: %s value is '%s' while %s value is '%s'.",
                    dict1_path, dict1[key], dict2_path, dict2[key]
                    )
                result = False
        # Hack for NetBox v2.6.7 requiring integers for some values
        elif key in ["status", "type"]:
            if dict1[key] != dict2[key]["value"]:
                log.debug(
                    "Mismatch: %s value is '%s' while %s value is '%s'.",
                    dict1_path, dict1[key], dict2_path, dict2[key]["value"]
                    )
                result = False
        elif dict1[key] != dict2[key]:
            log.debug(
                "Mismatch: %s value is '%s' while %s value is '%s'.",
                dict1_path, dict1[key], dict2_path, dict2[key]
                )
            result = False
        if result:
            log.debug("%s and %s values match.", dict1_path, dict2_path)
        else:
            log.debug("%s and %s values do not match.", dict1_path, dict2_path)
            return result
    log.debug("Final dictionary compare result: %s", result)
    return result


def format_ip(ip_addr):
    """
    Formats IPv4 addresses and subnet to IP with CIDR standard notation.

    :param ip_addr: IP address with subnet; example `192.168.0.0/255.255.255.0`
    :type ip_addr: str
    :return: IP address with CIDR notation; example `192.168.0.0/24`
    :rtype: str
    """
    ip = ip_addr.split("/")[0]
    cidr = ip_network(ip_addr, strict=False).prefixlen
    result = "{}/{}".format(ip, cidr)
    log.debug("Converted '%s' to CIDR notation '%s'.", ip_addr, result)
    return result


def format_tag(tag):
    """
    Format string to comply to NetBox tag format and max length.

    :param tag: The text which should be formatted
    :type tag: str
    :return: Tag which complies to the NetBox required tag format and max length
    :rtype: str
    """
    # If the tag presented is an IP address then no modifications are required
    try:
        ip_network(tag)
    except ValueError:
        # If an IP was not provided then assume fqdn
        split_tag = tag.split(".")
        # Handle cases where the tag would match the general `vCenter` tag
        if split_tag[0].lower() == "vcenter" and len(split_tag) > 1:
            tag = "-".join(split_tag)
        elif split_tag[0].lower() == "vcenter" and len(split_tag) == 1:
            tag = "vcenter-server"
        else:
            tag = split_tag[0]
        tag = truncate(tag, max_len=100)
    return tag


def format_vcenter_conn(conn):
    """
    Formats :param conn: into the expected connection string for vCenter.

    This supports the use of per-host credentials without breaking previous
    deployments during upgrade.

    :param conn: vCenter host connection details provided in settings.py
    :type conn: dict
    :returns: A dictionary containing the host details and credentials
    :rtype: dict
    """
    try:
        if bool(conn["USER"] != "" and conn["PASS"] != ""):
            log.debug(
                "Host specific vCenter credentials provided for '%s'.",
                conn["HOST"]
                )
        else:
            log.debug(
                "Host specific vCenter credentials are not defined for '%s'.",
                conn["HOST"]
                )
            conn["USER"], conn["PASS"] = settings.VC_USER, settings.VC_PASS
    except KeyError:
        log.debug(
            "Host specific vCenter credential key missing for '%s'. Falling "
            "back to global.", conn["HOST"]
            )
        conn["USER"], conn["PASS"] = settings.VC_USER, settings.VC_PASS
    return conn


def is_banned_asset_tag(text):
    """
    Determines whether the text is a banned asset tag through various tests.

    :param text: Text to be checked against banned asset tags
    :type text: str
    :return: `True` if a banned asset tag else `False`
    :rtype: bool
    """
    # Is asset tag in banned list?
    text = text.lower()
    banned_tags = [
        "Default string", "NA", "N/A", "None", "Null", "oem", "o.e.m",
        "to be filled by o.e.m.", "Unknown", " ", ""
        ]
    banned_tags = [t.lower() for t in banned_tags]
    if text.strip() in banned_tags:
        result = True
    # Does it exceed the max allowed length for NetBox asset tags?
    elif len(text) > 50:
        result = True
    # Does asset tag contain all spaces?
    elif text.replace(" ", "") == "":
        result = True
    # Apparently a "good" asset tag :)
    else:
        result = False
    return result


def main():
    """Main function ran when the script is called directly."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c", "--cleanup", action="store_true",
        help="Remove all vCenter synced objects which support tagging. This "
             "is helpful if you want to start fresh or stop using this script."
        )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose output. This overrides the log level in the "
             "settings file. Intended for debugging purposes only."
        )
    args = parser.parse_args()
    if args.verbose:
        log.setLevel("DEBUG")
        log.debug("Log level has been overriden by the --verbose argument.")
    for vc_host in settings.VC_HOSTS:
        try:
            start_time = datetime.now()
            nb = NetBoxHandler(vc_conn=vc_host)
            if args.cleanup:
                nb.remove_all()
                log.info(
                    "Completed removal of vCenter instance '%s' objects. Total "
                    "execution time %s.",
                    vc_host["HOST"], (datetime.now() - start_time)
                    )
            else:
                nb.verify_dependencies()
                nb.sync_objects(vc_obj_type="datacenters")
                nb.sync_objects(vc_obj_type="clusters")
                nb.sync_objects(vc_obj_type="hosts")
                nb.sync_objects(vc_obj_type="virtual_machines")
                nb.set_primary_ips()
                # Optional tasks
                if settings.POPULATE_DNS_NAME:
                    nb.set_dns_names()
                log.info(
                    "Completed sync with vCenter instance '%s'! Total "
                    "execution time %s.", vc_host["HOST"],
                    (datetime.now() - start_time)
                    )
        except (ConnectionError, requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as err:
            log.warning(
                "Critical connection error occurred. Skipping sync with '%s'.",
                vc_host["HOST"]
                )
            log.debug("Connection error details: %s", err)
            continue


def queue_dns_lookups(ips):
    """
    Queue handler for reverse DNS lokups.

    :param ips: A list of IP addresses to queue for PTR lookup.
    :type ips: list
    :return: IP addresses and their respective PTR record
    :rtype: dict
    """
    loop = asyncio.get_event_loop()
    resolver = aiodns.DNSResolver(loop=loop)
    if settings.CUSTOM_DNS_SERVERS and settings.DNS_SERVERS:
        resolver.nameservers = settings.DNS_SERVERS
    queue = asyncio.gather(*(reverse_lookup(resolver, ip) for ip in ips))
    results = loop.run_until_complete(queue)
    return results


async def reverse_lookup(resolver, ip):
    """
    Queries for PTR record of the IP provided with async support.

    :param resolver: aiodns resolver instance set to the asyncio loop
    :type resolver: aiodns.DNSResolver
    :param ip: IP address to request PTR record for.
    :type ip: str
    :return: IP Address and its PTR record
    :rtype: tuple
    """
    result = (ip, "")
    allowed_chars = "abcdefghijklmnopqrstuvwxyz0123456789-."
    log.info("Requesting PTR record for %s.", ip)
    try:
        resp = await resolver.gethostbyaddr(ip)
        # Make sure records comply to NetBox and DNS expected format
        if all([bool(c.lower() in allowed_chars) for c in resp.name]):
            result = (ip, resp.name.lower())
            log.debug("PTR record for %s is '%s'.", ip, result[1])
        else:
            log.debug(
                "Invalid characters detected in PTR record '%s'. Nulling.",
                resp.name
                )
    except aiodns.error.DNSError as err:
        log.info("Unable to find record for %s: %s", ip, err.args[1])
    return result


def verify_ip(ip_addr):
    """
    Verify input is expected format and checks against allowed networks.

    Allowed networks can be defined in the settings IPV4_ALLOWED and
    IPV6_ALLOWED variables.
    :param ip_addr: IP address to check for format and whether its within allowed networks
    :type ip_addr: str
    :return: `True` if valid IP and within the allowed networks else `False`
    :rtype: bool
    """
    result = False
    try:
        log.debug(
            "Validating IP '%s' is properly formatted and within allowed "
            "networks.",
            ip_addr
            )
        # Strict is set to false to allow host address checks
        version = ip_network(ip_addr, strict=False).version
        global_nets = settings.IPV4_ALLOWED if version == 4 \
                      else settings.IPV6_ALLOWED
        # Check whether the network is within the global allowed networks
        log.debug(
            "Checking whether IP address '%s' is within %s.", ip_addr,
            global_nets
            )
        net_matches = [
            ip_network(ip_addr, strict=False).overlaps(ip_network(net))
            for net in global_nets
            ]
        result = any(net_matches)
    except ValueError as err:
        log.debug("Validation of %s failed. Received error: %s", ip_addr, err)
    log.debug("IP '%s' validation returned a %s status.", ip_addr, result)
    return result


class vCenterHandler:
    """
    Handles vCenter connection state and object data collection

    :param vc_conn: Connection details for a vCenter host defined in settings.py
    :type vc_conn: dict
    :param nb_api_version: NetBox API version that objects must conform to
    :type nb_api_version: float
    """
    def __init__(self, vc_conn, nb_api_version):
        self.nb_api_version = nb_api_version
        self.vc_session = None # Used to hold vCenter session state
        self.vc_host = vc_conn["HOST"]
        self.vc_port = vc_conn["PORT"]
        self.vc_user = vc_conn["USER"]
        self.vc_pass = vc_conn["PASS"]
        self.tags = ["Synced", "vCenter", format_tag(self.vc_host)]
        # vCenter hosts which are not assigned to a cluster
        self.standalone_hosts = []

    def authenticate(self):
        """Create a session to vCenter and authenticate against it"""
        log.info(
            "Attempting authentication to vCenter instance '%s'.",
            self.vc_host
            )
        try:
            vc_instance = SmartConnectNoSSL(
                host=self.vc_host,
                port=self.vc_port,
                user=self.vc_user,
                pwd=self.vc_pass,
                )
            atexit.register(Disconnect, vc_instance)
            self.vc_session = vc_instance.RetrieveContent()
            log.info(
                "Successfully authenticated to vCenter instance '%s'.",
                self.vc_host
                )
        except (gaierror, vim.fault.InvalidLogin, OSError) as err:
            if isinstance(err, OSError):
                err = "System unreachable."
            err_msg = (
                "Unable to connect to vCenter instance '{}' on port {}. "
                "Reason: {}".format(self.vc_host, self.vc_port, err)
                )
            log.critical(err_msg)
            raise ConnectionError(err_msg)

    def create_view(self, vc_obj_type):
        """
        Create a view scoped to the vCenter object type desired.

        This should be called before collecting data about vCenter object types.
        :param vc_obj_type: vCenter object type to extract, must be key in vc_obj_views
        :type vc_obj_type: str
        """
        # Mapping of object type keywords to view types
        vc_obj_views = {
            "datacenters": vim.Datacenter,
            "clusters": vim.ClusterComputeResource,
            "hosts": vim.HostSystem,
            "virtual_machines": vim.VirtualMachine,
            }
        # Ensure an active vCenter session exists
        if not self.vc_session:
            log.info("No existing vCenter session found.")
            self.authenticate()
        return self.vc_session.viewManager.CreateContainerView(
            self.vc_session.rootFolder, # View starting point
            [vc_obj_views[vc_obj_type]], # Object types to look for
            True # Should we recurively look into view
            )

    def get_objects(self, vc_obj_type):
        """
        Collects vCenter objects of type and returns NetBox formated objects.

        :param vc_obj_type: vCenter object type to extract, must be key in obj_type_map
        :type vc_obj_type: str
        :return: Extracted vCenter objects of :param vc_obj_type: in NetBox format
        :rtype: dict
        """
        log.info(
            "Collecting vCenter %s objects.",
            vc_obj_type[:-1].replace("_", " ") # Format virtual machines
            )
        # Mapping of vCenter object types to NetBox object types
        obj_type_map = {
            "datacenters": ["cluster_groups"],
            "clusters": ["clusters"],
            "hosts": [
                "manufacturers", "device_types", "platforms", "devices", "interfaces",
                "ip_addresses"
                ],
            "virtual_machines": [
                "platforms", "virtual_machines", "virtual_interfaces",
                "ip_addresses"
                ]
            }
        results = {}
        # Setup use of NetBox templates
        nbt = Templates(api_version=self.nb_api_version)
        # Initialize keys expected to be returned
        for nb_obj_type in obj_type_map[vc_obj_type]:
            results.setdefault(nb_obj_type, [])
        # Create vCenter view for object collection
        container_view = self.create_view(vc_obj_type)
        for obj in container_view.view:
            try:
                obj_name = obj.name
                log.info(
                    "Collecting info about vCenter %s '%s' object.",
                    vc_obj_type, obj_name
                    )
                if vc_obj_type == "datacenters":
                    results["cluster_groups"].append(nbt.cluster_group(
                        name=obj_name
                        ))
                elif vc_obj_type == "clusters":
                    results["clusters"].append(nbt.cluster(
                        name=obj_name,
                        ctype="VMware ESXi",
                        group=obj.parent.parent.name,
                        tags=self.tags
                        ))
                elif vc_obj_type == "hosts":
                    obj_manuf_name = truncate(
                        obj.summary.hardware.vendor, max_len=50
                        )
                    obj_model = truncate(obj.summary.hardware.model, max_len=50)
                    obj_platform = truncate(
                        getattr(obj.summary.config.product, "fullName", "VMware ESXi"), 
                        max_len=100
                        )
                    # NetBox Platforms, Manufacturers and Device Types are susceptible to
                    # duplication as they are parents to multiple objects
                    # To avoid unnecessary querying we check to make sure they
                    # haven't already been collected
                    if not obj_manuf_name in [
                            res["name"] for res in results["manufacturers"]]:
                        log.debug(
                            "Collecting info to create NetBox manufacturers "
                            "object."
                            )
                        results["manufacturers"].append(nbt.manufacturer(
                            name=obj_manuf_name
                            ))
                    else:
                        log.debug(
                            "Manufacturers object '%s' already exists. "
                            "Skipping.", obj_manuf_name
                            )
                    if not obj_model in [
                            res["model"] for res in
                            results["device_types"]]:
                        log.debug(
                            "Collecting info to create NetBox device_types "
                            "object."
                            )
                        results["device_types"].append(nbt.device_type(
                            manufacturer=obj_manuf_name,
                            model=obj_model,
                            part_number=obj_model,
                            tags=self.tags
                            ))
                    else:
                        log.debug(
                            "Device Type object '%s' already exists. "
                            "Skipping.", obj_model
                            )
                    if not obj_platform in [
                            res["name"] for res in results["platforms"]]:
                        log.debug(
                            "Collecting info to create NetBox platforms "
                            "object."
                            )
                        results["platforms"].append(nbt.platform(
                            name=obj_platform,
                            ))
                    else:
                        log.debug(
                            "Platform object '%s' already exists. "
                            "Skipping.", obj_platform
                            ) 
                    log.debug(
                        "Collecting info to create NetBox devices object."
                        )
                    # Attempt to find serial number and asset tag
                    hw_idents = { # Scan throw identifiers to find S/N
                        identifier.identifierType.key:
                        identifier.identifierValue
                        for identifier in
                        obj.summary.hardware.otherIdentifyingInfo
                        }
                    # Serial Number
                    if "EnclosureSerialNumberTag" in hw_idents.keys():
                        serial_number = hw_idents["EnclosureSerialNumberTag"]
                    elif "ServiceTag" in hw_idents.keys() \
                    and " " not in hw_idents["ServiceTag"]:
                        serial_number = hw_idents["ServiceTag"]
                    else:
                        serial_number = None
                    # Asset Tag
                    asset_tag = None
                    if settings.ASSET_TAGS:
                        try:
                            if "AssetTag" in hw_idents.keys():
                                candidate_at = hw_idents["AssetTag"].lower()
                                if not is_banned_asset_tag(candidate_at):
                                    log.debug(
                                        "Received asset tag '%s' from vCenter.",
                                        candidate_at
                                        )
                                    asset_tag = candidate_at
                                else:
                                    log.debug(
                                        "Banned asset tag string. Nulling."
                                        )
                            else:
                                log.debug(
                                    "No asset tag detected for device '%s'.",
                                    obj_name
                                    )
                            log.debug("Final decided asset tag: %s", asset_tag)
                        except AttributeError as error:
                            log.debug(
                                "Received error when checking asset tag for "
                                " device '%s'. Error: %s",
                                obj_name, error
                                )
                    # Cluster
                    cluster = obj.parent.name
                    # We throw the cluster away if it matches the ESXi host
                    # name as it is standalone.
                    if cluster == obj_name:
                        # Store the host so that we can check VMs against it
                        self.standalone_hosts.append(cluster)
                        cluster = "Standalone ESXi Host"
                    # Create NetBox device
                    results["devices"].append(nbt.device(
                        name=truncate(obj_name, max_len=64),
                        device_role=settings.DEVICE_ROLE_HOST,
                        device_type=obj_model,
                        platform=obj_platform,
                        site="vCenter",
                        serial=serial_number,
                        asset_tag=asset_tag,
                        cluster=cluster,
                        status=int(
                            1 if obj.summary.runtime.connectionState ==
                            "connected" else 0
                            ),
                        tags=self.tags
                        ))
                    # Iterable object types
                    # Physical Interfaces
                    log.debug(
                        "Collecting info to create NetBox interfaces object."
                        )
                    for pnic in obj.config.network.pnic:
                        nic_name = truncate(pnic.device, max_len=64)
                        log.debug(
                            "Collecting info for physical interface '%s'.",
                            nic_name
                            )
                        # Try multiple methods of finding link speed
                        link_speed = pnic.spec.linkSpeed
                        if link_speed is None:
                            try:
                                link_speed = "{}Mbps ".format(
                                    pnic.validLinkSpecification[0].speedMb
                                    )
                            except IndexError:
                                log.debug(
                                    "No link speed detected for physical "
                                    "interface '%s'.", nic_name
                                    )
                        else:
                            link_speed = "{}Mbps ".format(
                                pnic.spec.linkSpeed.speedMb
                                )
                        results["interfaces"].append(nbt.device_interface(
                            device=truncate(obj_name, max_len=64),
                            name=nic_name,
                            itype=32767,  # Other
                            mac_address=pnic.mac,
                            # Interface speed is placed in the description as it
                            # is irrelevant to making connections and an error
                            # prone mapping process
                            description="{}Physical Interface".format(
                                link_speed
                                ),
                            enabled=bool(link_speed),
                            tags=self.tags,
                            ))
                    # Virtual Interfaces
                    for vnic in obj.config.network.vnic:
                        nic_name = truncate(vnic.device, max_len=64)
                        log.debug(
                            "Collecting info for virtual interface '%s'.",
                            nic_name
                            )
                        results["interfaces"].append(nbt.device_interface(
                            device=truncate(obj_name, max_len=64),
                            name=nic_name,
                            itype=0,  # Virtual
                            mac_address=vnic.spec.mac,
                            mtu=vnic.spec.mtu,
                            tags=self.tags,
                            ))
                        # IP Addresses
                        ip_addr = vnic.spec.ip.ipAddress
                        log.debug(
                            "Collecting info for IP Address '%s'.",
                            ip_addr
                            )
                        results["ip_addresses"].append(nbt.ip_address(
                            address="{}/{}".format(
                                ip_addr, vnic.spec.ip.subnetMask
                                ),
                            device=truncate(obj_name, max_len=64),
                            assigned_object=nic_name,
                            tags=self.tags,
                            ))
                elif vc_obj_type == "virtual_machines":
                    # Virtual Machines
                    log.debug(
                        "Collecting info for virtual machine '%s'", obj_name
                        )
                    # Cluster
                    cluster = obj.runtime.host.parent.name
                    if cluster in self.standalone_hosts:
                        log.debug(
                            "VM is assigned to a standalone ESXi host. Setting "
                            "cluster to 'Standalone ESXi Host'."
                            )
                        cluster = "Standalone ESXi Host"

                    #Guest Tools + Platform
                    if (obj.guest.toolsVersionStatus == 'guestToolsNotInstalled' or
                        obj.guest.toolsRunningStatus == 'guestToolsNotRunning'):
                        guest_tools = False
                        platform = obj.summary.config.guestFullName
                    else:
                        guest_tools = True
                        platform = obj.guest.guestFullName
                        if platform is None or len(platform) <= 0:
                            platform = obj.summary.config.guestFullName
                    if platform is not None:
                        # Add new platform object if it doesn't already exist
                        platform = truncate(platform, max_len=100)
                        if platform not in (
                                res["name"] for res in results["platforms"]):
                            results["platforms"].append(nbt.platform(
                                name=platform,
                                ))
                    results["virtual_machines"].append(nbt.virtual_machine(
                        name=truncate(obj_name, max_len=64),
                        cluster=cluster,
                        comments=getattr(obj.config, "annotation", None),
                        status=int(
                            1 if obj.runtime.powerState == "poweredOn" else 0
                            ),
                        role=settings.DEVICE_ROLE_VM,
                        platform=platform,
                        memory=obj.config.hardware.memoryMB,
                        disk=int(sum([
                            comp.capacityInKB for comp in
                            obj.config.hardware.device
                            if isinstance(comp, vim.vm.device.VirtualDisk)
                            ]) / 1024 / 1024),  # Kilobytes to Gigabytes
                        vcpus=obj.config.hardware.numCPU,
                        tags=self.tags
                        ))
                    # If VMware Tools is not detected then we cannot reliably
                    # collect interfaces and IP addresses
                    if guest_tools:
                        for index, nic in enumerate(obj.guest.net):
                            # Interfaces
                            nic_name = truncate(getattr(nic, "network", "vNIC{}".format(index)), 64)
                            log.debug(
                                "Collecting info for virtual interface '%s'.",
                                nic_name
                                )
                            results["virtual_interfaces"].append(
                                nbt.vm_interface(
                                    virtual_machine=obj_name,
                                    name=nic_name,
                                    mac_address=nic.macAddress,
                                    enabled=nic.connected,
                                    tags=self.tags
                                ))
                            # IP Addresses
                            if nic.ipConfig is not None:
                                for ip in nic.ipConfig.ipAddress:
                                    ip_addr = "{}/{}".format(
                                        ip.ipAddress, ip.prefixLength
                                        )
                                    log.debug(
                                        "Collecting info for IP Address '%s'.",
                                        ip_addr
                                        )
                                    results["ip_addresses"].append(
                                        nbt.ip_address(
                                            address=ip_addr,
                                            virtual_machine=obj_name,
                                            assigned_object=nic_name,
                                            tags=self.tags
                                            ))
            except AttributeError as err:
                log.warning(
                    "Unable to collect necessary data for vCenter %s %s"
                    "object. Received error: %s", vc_obj_type, obj, err
                    )
        container_view.Destroy()
        log.debug(
            "Collected %s vCenter %s object%s.", len(results),
            vc_obj_type[:-1].replace("_", " "),
            "s" if len(results) != 1 else "",
            )
        return results

class NetBoxHandler:
    """
    Handles NetBox connection state and interaction with API

    :param vc_conn: Connection details for a vCenter host defined in settings.py
    :type vc_conn: dict
    """

    instance_tags = None
    instance_interfaces = {}
    instance_virtual_interfaces = {}

    def __init__(self, vc_conn):
        self.nb_api_url = "http{}://{}{}/api/".format(
            ("s" if not settings.NB_DISABLE_TLS else ""), settings.NB_FQDN,
            (":{}".format(settings.NB_PORT) if settings.NB_PORT != 443 else "")
            )
        self.nb_session = self._create_nb_session()
        self.nb_api_version = self._get_api_version()
        # NetBox object type relationships when working in the API
        self.obj_map = {
            "cluster_groups": {
                "api_app": "virtualization",
                "api_model": "cluster-groups",
                "key": "name",
                "prune": False,
                "taggable": False
                },
            "cluster_types": {
                "api_app": "virtualization",
                "api_model": "cluster-types",
                "key": "name",
                "prune": False,
                "taggable": False
                },
            "clusters": {
                "api_app": "virtualization",
                "api_model": "clusters",
                "key": "name",
                "prune": True,
                "prune_pref": 2,
                "taggable": True
                },
            "device_roles": {
                "api_app": "dcim",
                "api_model": "device-roles",
                "key": "name",
                "prune": False,
                "taggable": False
                },
            "device_types": {
                "api_app": "dcim",
                "api_model": "device-types",
                "key": "model",
                "prune": True,
                "prune_pref": 3,
                "taggable": True
                },
            "devices": {
                "api_app": "dcim",
                "api_model": "devices",
                "key": "name",
                "prune": True,
                "prune_pref": 4,
                "taggable": True
                },
            "interfaces": {
                "api_app": "dcim",
                "api_model": "interfaces",
                "key": "name",
                "prune": True,
                "prune_pref": 5,
                "taggable": True
                },
            "ip_addresses": {
                "api_app": "ipam",
                "api_model": "ip-addresses",
                "key": "address",
                "prune": True,
                "prune_pref": 8,
                "taggable": False
                },
            "manufacturers": {
                "api_app": "dcim",
                "api_model": "manufacturers",
                "key": "name",
                "prune": False,
                "taggable": False
                },
            "platforms": {
                "api_app": "dcim",
                "api_model": "platforms",
                "key": "name",
                "prune": False,
                "taggable": False
                },
            "prefixes": {
                "api_app": "ipam",
                "api_model": "prefixes",
                "key": "prefix",
                "prune": False,
                "taggable": False
                },
            "sites": {
                "api_app": "dcim",
                "api_model": "sites",
                "key": "name",
                "prune": True,
                "prune_pref": 1,
                "taggable": False
                },
            "tags": {
                "api_app": "extras",
                "api_model": "tags",
                "key": "slug",
                "prune": False,
                "taggable": False
                },
            "virtual_machines": {
                "api_app": "virtualization",
                "api_model": "virtual-machines",
                "key": "name",
                "prune": True,
                "prune_pref": 6,
                "taggable": True
                },
            "virtual_interfaces": {
                "api_app": "virtualization",
                "api_model": "interfaces",
                "key": "name",
                "prune": True,
                "prune_pref": 7,
                "taggable": True
                },
            }
        self.vc_tag = format_tag(vc_conn["HOST"])
        self.vrf_id = vc_conn.get("VRF_ID")
        self.vc = vCenterHandler(
            format_vcenter_conn(vc_conn), nb_api_version=self.nb_api_version
            )

    def _create_nb_session(self):
        """
        Creates a session with NetBox

        :return: `True` if session created else `False`
        :rtype: bool
        """
        header = {"Authorization": "Token {}".format(settings.NB_API_KEY)}
        session = requests.Session()
        session.headers.update(header)
        self.nb_session = session
        log.info("Created new HTTP Session for NetBox.")
        return session

    def _get_api_version(self):
        """
        Determines the current NetBox API Version

        :return: NetBox API version
        :rtype: float
        """
        with self.nb_session.get(
                self.nb_api_url, timeout=10,
                verify=(not settings.NB_INSECURE_TLS)) as resp:
            result = float(resp.headers["API-Version"])
        log.info("Detected NetBox API v%s.", result)
        return result

    def get_primary_ip(self, nb_obj_type, nb_id):
        """
        Collects the primary IP of a NetBox device or virtual machine.

        :param nb_obj_type: NetBox object type; must match key in self.obj_map
        :type nb_obj_type: str
        :param nb_id: NetBox object ID of parent object where IP is configured
        :type nb_id: int
        :return: Primary IP and ID of the requested NetBox device or virtual machine
        :rtype: dict
        """
        query_key = str(
            "device_id" if nb_obj_type == "devices" else "virtual_machine_id"
            )
        req = self.request(
            req_type="get", nb_obj_type="ip_addresses",
            query="?{}={}".format(query_key, nb_id)
            )
        log.debug("Found %s child IP addresses.", req["count"])
        if req["count"] > 0:
            result = {
                "address": req["results"][0]["address"],
                "id": req["results"][0]["id"]
                }
            log.debug(
                "Selected %s (ID: %s) as primary IP.", result["address"],
                result["id"]
                )
        else:
            result = None
        return result

    def request(self, req_type, nb_obj_type, data=None, query=None, nb_id=None):

        max_retry_attempts = 3

        for _ in range(max_retry_attempts):

            try:
                result = self.single_request(req_type, nb_obj_type, data, query, nb_id)
            except (SystemExit, ConnectionError, requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout):
                log.warning("Request failed, trying again.")
                continue
            else:
                break
        else:
            raise SystemExit(
                    log.critical("Giving up after %d retries.", max_retry_attempts)
                    )

        return result

    def single_request(self, req_type, nb_obj_type, data=None, query=None, nb_id=None):
        """
        HTTP requests and exception handler for NetBox

        :param req_type: HTTP method type (GET, POST, PUT, PATCH, DELETE)
        :type req_type: str
        :param nb_obj_type: NetBox object type, must match keys in self.obj_map
        :type nb_obj_type: str
        :param data: NetBox object key value pairs
        :type data: dict, optional
        :param query: Filter for GET method requests
        :type query: str, optional
        :param nb_id: NetBox Object ID used when modifying an existing object
        :type nb_id: int, optional
        :return: Netbox objects and their corresponding data
        :rtype: dict
        """
        result = None
        # Generate URL
        url = "{}{}/{}/{}{}".format(
            self.nb_api_url,
            self.obj_map[nb_obj_type]["api_app"], # App that model falls under
            self.obj_map[nb_obj_type]["api_model"], # Data model
            query if query else "",
            "{}/".format(nb_id) if nb_id else ""
            )
        log.debug(
            "Sending %s to '%s' with data '%s'.", req_type.upper(), url, data
            )
        req = getattr(self.nb_session, req_type)(
            url, json=data, timeout=10, verify=(not settings.NB_INSECURE_TLS)
            )
        # Parse status
        log.debug("Received HTTP Status %s.", req.status_code)
        if req.status_code == 200:
            log.debug(
                "NetBox %s request OK; returned %s status.", req_type.upper(),
                req.status_code
                )
            result = req.json()
            if req_type == "get":
                # NetBox returns 50 results by default, this ensures all results
                # are bundled together
                while req.json()["next"] is not None:
                    url = req.json()["next"]
                    log.debug(
                        "NetBox returned more than 50 objects. Sending %s to "
                        "%s for additional objects.", req_type.upper(), url
                        )
                    req = getattr(self.nb_session, req_type)(url, timeout=10)
                    result["results"] += req.json()["results"]
        elif req.status_code in [201, 204]:
            log.info(
                "NetBox successfully %s %s object.",
                "created" if req.status_code == 201 else "deleted",
                nb_obj_type,
                )
            # return patched data
            if req.status_code == 201:
                result = req.json()
        elif req.status_code == 400:
            if req_type == "post":
                log.warning(
                    "NetBox failed to create %s object. A duplicate record may "
                    "exist or the data sent is not acceptable.", nb_obj_type
                    )
                log.debug(
                    "NetBox %s status reason: %s", req.status_code, req.text
                    )
            elif req_type == "patch":
                log.warning(
                    "NetBox failed to modify %s object with status %s. The "
                    "data sent may not be acceptable.", nb_obj_type,
                    req.status_code
                    )
                log.debug(
                    "NetBox %s status reason: %s", req.status_code, req.text
                    )
            else:
                raise SystemExit(
                    log.critical(
                        "Well this in unexpected. Please report this. "
                        "%s request received %s status with body '%s' and "
                        "response '%s'.",
                        req_type.upper(), req.status_code, data, req.json()
                        )
                    )
            log.debug("Unaccepted request data: %s", data)
        elif req.status_code == 409 and req_type == "delete":
            log.warning(
                "Received %s status when attemping to delete NetBox object "
                "(ID: %s). If you have more than 1 vCenter host configured "
                "this may be deleted on the final pass. Otherwise check the "
                "object dependencies.",
                req.status_code, nb_id
                )
            log.debug("NetBox %s status body: %s", req.status_code, req.json())
        else:
            raise SystemExit(
                log.critical(
                    "Well this in unexpected. Please report this. "
                    "%s request received %s status with body '%s' and response "
                    "'%s'.",
                    req_type.upper(), req.status_code, data, req.text
                    )
                )
        return result

    def update_tag_data(self, tag_list):
        """
        updates missing tags in NetBox and reformats list of tags to list of tag dicts

        :param tag_list: list of tags
        :type tag_list: list
        :return: list of reformatted tags
        :rtype: list
        """
        nbt = Templates(api_version=self.nb_api_version)

        tagdata_updated = False

        for tag in tag_list:

            if isinstance(tag, dict):
                tag = tag.get("name")

                if tag is None:
                    continue

            if tag not in [d.get("name") for d in self.instance_tags]:
                log.info(
                    "Netbox tag '%s' object not found. Requesting creation.",
                    tag,
                )
                self.request(
                    req_type="post",
                    nb_obj_type="tags",
                    data=nbt.tag(name=tag)
                )

                tagdata_updated = True

        # update tag information
        if tagdata_updated:
            self.get_instance_tags()

        return [{"name": d } for d in tag_list]

    def obj_exists(self, nb_obj_type, vc_data):
        """
        Checks whether a NetBox object exists and matches the vCenter object.

        If object does not exist or does not match the vCenter object it will
        be created or updated.

        :param nb_obj_type: NetBox object type, must match keys in self.obj_map
        :type nb_obj_type: str
        :param vc_data: Extracted object of :param nb_obj_type: from vCenter
        :type vc_data: dict
        """
        # NetBox Device Types objects do not have names to query; we catch
        # and use the model instead
        query_key = self.obj_map[nb_obj_type]["key"]

        # Create a query specific to the device parent/child relationship when
        # working with interfaces
        if nb_obj_type == "interfaces":
            query = "?device={}&{}={}".format(
                vc_data["device"]["name"], query_key, vc_data[query_key]
                )
        elif nb_obj_type == "virtual_interfaces":
            query = "?virtual_machine={}&{}={}".format(
                quote_plus(vc_data["virtual_machine"]["name"]), query_key,
                quote_plus(vc_data[query_key])
                )
        elif self.vrf_id is not None and nb_obj_type in ("ip_addresses", "prefixes"):
            query = "?{}={}&vrf_id={}".format(query_key, vc_data[query_key], self.vrf_id)
        else:
            query = "?{}={}".format(
                query_key, vc_data[query_key]
                )

        # Add support for filtering objects by vCenter tags
        if self.obj_map[nb_obj_type]["taggable"]:
            query = query + "&?tag={}".format(self.vc_tag)

        # request object from NetBox
        req = self.request(
            req_type="get", nb_obj_type=nb_obj_type,
            query=query
            )

        # add interface id and type to ip_address object
        if nb_obj_type == "ip_addresses":
            nic_name = vc_data["assigned_object"]["name"]
            if  vc_data["assigned_object"].get("virtual_machine"):
                name = vc_data["assigned_object"]["virtual_machine"]["name"]
                int_data = self.instance_virtual_interfaces["{}/{}".format(name, nic_name)]
                int_type = 'virtualization.vminterface'
            if  vc_data["assigned_object"].get("device"):
                name = vc_data["assigned_object"]["device"]["name"]
                int_data = self.instance_interfaces["{}/{}".format(name, nic_name)]
                int_type = 'dcim.interface'

            if int_data is not None:
                vc_data["assigned_object_id"] = int_data.get("id")
                vc_data["assigned_object_type"] = int_type

        # A single matching object is found so we compare its values to the new
        # object
        if req["count"] == 1:
            log.debug(
                "NetBox %s object '%s' already exists. Comparing values.",
                nb_obj_type, vc_data[query_key]
                )
            nb_data = req["results"][0]

            # Remove site from existing NetBox host objects to allow for
            # user modifications
            if nb_obj_type == "devices":
                del vc_data["site"]
                log.debug(
                    "Removed site from %s object before sending update "
                    "to NetBox.", vc_data[query_key]
                    )
            # Remove platform from existing NetBox vm objects to allow for
            # user modifications
            if nb_obj_type == "virtual_machine" and nb_data["platform"] is not None:
                del vc_data["platform"]
                log.debug(
                    "Removed platform from %s object before sending update "
                    "to NetBox.", vc_data[query_key]
                    )
            # Remove type from existing NetBox interface objects to allow for
            # user modifications
            if nb_obj_type == "interfaces":
                del vc_data["type"]
                log.debug(
                    "Removed type from %s object before sending update "
                    "to NetBox.", vc_data[query_key]
                    )
            if "interfaces" in nb_obj_type:
                self.update_interface_data(nb_obj_type, nb_data)

            # reduce NetBox tags to names only
            if nb_data.get("tags"):
                nb_data["tags"] = [{"name": d["name"]} for d in nb_data["tags"]]

            # add missing tags before assigning them
            if "tags" in vc_data:

                vc_data["tags"] = self.update_tag_data(vc_data["tags"])
            
            # Objects that have been previously tagged as orphaned but then
            # reappear in vCenter need to be stripped of their orphaned status
            if "tags" in nb_data and "Orphaned" in [d.get("name") for d in nb_data["tags"]]:
                log.info(
                    "NetBox %s object '%s' is currently marked as orphaned "
                    "but has reappeared in vCenter. Updating NetBox.",
                    nb_obj_type, vc_data[query_key]
                    )
                self.request(
                    req_type="patch", nb_obj_type=nb_obj_type, data=vc_data,
                    nb_id=nb_data["id"]
                    )
            elif compare_dicts(
                    vc_data, nb_data, dict1_name="vc_data",
                    dict2_name="nb_data"):
                log.info(
                    "NetBox %s object '%s' match current values. Moving on.",
                    nb_obj_type, vc_data[query_key]
                    )
            else:
                # prevent reassignment of ip_addresses which have been assigned
                # to a different virtual_machine in NetBox
                if nb_obj_type == "ip_addresses" and nb_data.get("assigned_object") is not None:

                    vc_object_name = None
                    if vc_data.get("assigned_object") is not None:
                        if vc_data.get("assigned_object").get("device"):
                            vc_object_name = vc_data["assigned_object"]["device"]["name"]
                        if vc_data.get("assigned_object").get("virtual_machine"):
                            vc_object_name = vc_data["assigned_object"]["virtual_machine"]["name"]

                    if nb_data["assigned_object"].get("virtual_machine") is not None:
                        if nb_data["assigned_object"]["virtual_machine"]["name"] != vc_object_name:

                            log.warning(
                                "NetBox %s object '%s' for '%s' is already "
                                "assigned to a different virtual_machine '%s'. "
                                "Skipping for safety.",
                                nb_obj_type, vc_data[query_key], vc_object_name,
                                nb_data["assigned_object"]["virtual_machine"]["name"]
                            )
                            return

                    if nb_data["assigned_object"].get("device") is not None:
                        if nb_data["assigned_object"]["device"]["name"] != vc_object_name:

                            log.warning(
                                "NetBox %s object '%s' for '%s' is already "
                                "assigned to a different device '%s'. "
                                "Skipping for safety.",
                                nb_obj_type, vc_data[query_key], vc_object_name,
                                nb_data["assigned_object"]["device"]["name"]
                            )
                            return

                log.info(
                    "NetBox %s object '%s' do not match current values.",
                    nb_obj_type, vc_data[query_key]
                    )
                # Issue #1: Ensure existing and new tags are merged together
                # This allows users to add alternative tags or sync from
                # multiple vCenter instances
                if "tags" in vc_data:
                    log.debug("Merging tags between vCenter and NetBox object.")
                    vc_data["tags"] = list(
                        {x['name']:x for x in vc_data["tags"] + nb_data["tags"]}.values()
                        )
                # Remove site from existing NetBox host objects to allow for
                # user modifications
                if nb_obj_type == "devices":
                    del vc_data["site"]
                    log.debug(
                        "Removed site from %s object before sending update "
                        "to NetBox.", vc_data[query_key]
                        )
                respsone = self.request(
                    req_type="patch", nb_obj_type=nb_obj_type, data=vc_data,
                    nb_id=nb_data["id"]
                    )
                if "interfaces" in nb_obj_type:
                    self.update_interface_data(nb_obj_type, respsone)

        elif req["count"] > 1:
            log.warning(
                "Search for NetBox %s object '%s' returned %s results but "
                "should have only returned 1. Please manually review and "
                "report this if the data is accurate. Skipping for safety.",
                nb_obj_type, vc_data[query_key], req["count"]
                )
        else:

            if "tags" in vc_data:
                vc_data["tags"] = self.update_tag_data(vc_data["tags"])

            log.info(
                "Netbox %s '%s' object not found. Requesting creation.",
                nb_obj_type,
                vc_data[query_key],
                )
            respsone = self.request(
                req_type="post", nb_obj_type=nb_obj_type, data=vc_data
                )

            if "interfaces" in nb_obj_type:
                self.update_interface_data(nb_obj_type, respsone)

    def set_primary_ips(self):
        """Sets the Primary IP of vCenter hosts and Virtual Machines."""
        for nb_obj_type in ("devices", "virtual_machines"):
            log.info(
                "Collecting NetBox %s objects to set Primary IPs.",
                nb_obj_type[:-1].replace("_", " ")
                )
            obj_key = self.obj_map[nb_obj_type]["key"]
            # Collect all parent objects that support Primary IPs
            parents = self.request(
                req_type="get", nb_obj_type=nb_obj_type,
                query="?tag={}".format(format_slug(self.vc_tag))
                )
            log.info("Collected %s NetBox objects.", parents["count"])
            for nb_obj in parents["results"]:
                update_object = False
                parent_id = nb_obj["id"]
                nb_obj_name = nb_obj[obj_key]
                new_pri_ip = self.get_primary_ip(nb_obj_type, parent_id)
                # Skip the rest if we don't have a usable IP
                if new_pri_ip is None:
                    log.info(
                        "No usable IPs were found for NetBox '%s' object. "
                        "Moving on.",
                        nb_obj_name
                        )
                    continue
                if nb_obj["primary_ip"] is None:
                    log.info(
                        "No existing Primary IP found for NetBox '%s' object.",
                        nb_obj_name
                        )
                    update_object = True
                else:
                    log.info(
                        "Primary IP already set for NetBox '%s' object. "
                        "Comparing.",
                        nb_obj_name
                        )
                    old_pri_ip = nb_obj["primary_ip"]["id"]
                    if old_pri_ip != new_pri_ip["id"]:
                        log.info(
                            "Existing Primary IP does not match latest check. "
                            "Requesting update."
                            )
                        update_object = True
                    else:
                        log.info(
                            "Existing Primary IP matches latest check. Moving "
                            "on."
                            )
                if update_object:
                    log.info(
                        "Setting NetBox '%s' object primary IP to %s.",
                        nb_obj_name, new_pri_ip["address"]
                        )
                    ip_version = str(
                        "primary_ip{}".format(
                            ip_network(
                                new_pri_ip["address"], strict=False
                                ).version
                        ))
                    data = {ip_version: new_pri_ip["id"]}
                    self.request(
                        req_type="patch", nb_obj_type=nb_obj_type,
                        nb_id=parent_id, data=data
                        )

    def set_dns_names(self):
        """
        Performs a reverse DNS lookup on IP addresses and populates DMS name.
        """
        log.info("Collecting NetBox IP address objects to set DNS Names.")
        # Grab all the IPs from NetBox related tagged from the vCenter host
        ip_objs = self.request(
            req_type="get", nb_obj_type="ip_addresses",
            query="?tag={}".format(format_slug(self.vc_tag))
            )["results"]
        log.info("Collected %s NetBox IP address objects.", len(ip_objs))
        # We take the IP address objects and make a map of relevant details to
        # compare against and use later
        nb_objs = {}
        for obj in ip_objs:
            ip = obj["address"].split("/")[0]
            nb_objs[ip] = {
                "id": obj["id"],
                "dns_name": obj["dns_name"]
                }
        ips = [ip["address"].split("/")[0] for ip in ip_objs]
        ptrs = queue_dns_lookups(ips)
        # Having collected the IP address objects from NetBox already we can
        # avoid individual checks for updates by comparing the objects and PTRs
        log.info("Comparing latest PTR records against existing NetBox data.")
        for ip, ptr in ptrs:
            if ptr != nb_objs[ip]["dns_name"]:
                log.info(
                    "Mismatch! The latest PTR for '%s' is '%s' while NetBox "
                    "has '%s'. Requesting update.", ip, ptr,
                    nb_objs[ip]["dns_name"]
                    )
                self.request(
                    req_type="patch", nb_obj_type="ip_addresses",
                    nb_id=nb_objs[ip]["id"], data={"dns_name": ptr}
                    )
            else:
                log.info("NetBox has the latest PTR for '%s'. Moving on.", ip)

    def sync_objects(self, vc_obj_type):
        """
        Collects objects from vCenter and syncs them to NetBox.

        Some object types do not support tags so they will be a one-way sync
        meaning orphaned objects will not be removed from NetBox.
        :param vc_obj_type: vCenter object type to extract, must be key in obj_type_map
        :type vc_obj_type: str
        """
        # Collect data from vCenter
        log.info(
            "Initiated sync of vCenter %s objects to NetBox.",
            vc_obj_type[:-1]
            )
        vc_objects = self.vc.get_objects(vc_obj_type=vc_obj_type)
        # Determine each NetBox object type collected from vCenter
        nb_obj_types = list(vc_objects.keys())
        for nb_obj_type in nb_obj_types:
            log.info(
                "Starting sync of %s vCenter %s object%s to NetBox %s "
                "object%s.",
                len(vc_objects[nb_obj_type]),
                vc_obj_type,
                "s" if len(vc_objects[nb_obj_type]) != 1 else "",
                nb_obj_type,
                "s" if len(vc_objects[nb_obj_type]) != 1 else "",
                )
            for obj in vc_objects[nb_obj_type]:
                # Check to ensure IP addresses pass all checks before syncing
                # to NetBox
                if nb_obj_type == "ip_addresses":
                    ip_addr = obj["address"]
                    if verify_ip(ip_addr):
                        log.debug(
                            "IP %s has passed necessary pre-checks.",
                            ip_addr
                            )
                        # Update IP address to CIDR notation for comparsion
                        # with existing NetBox objects
                        obj["address"] = format_ip(ip_addr)
                        # Search for parent prefix to assign VRF and tenancy
                        prefix = self.search_prefix(obj["address"])
                        # Update placeholder values with matched values
                        obj["vrf"] = prefix["vrf"]
                        obj["tenant"] = prefix["tenant"]
                    else:
                        log.debug(
                            "IP %s has failed necessary pre-checks. Skipping "
                            "sync to NetBox.", ip_addr,
                            )
                        continue
                self.obj_exists(nb_obj_type=nb_obj_type, vc_data=obj)
            log.info(
                "Finished sync of %s vCenter %s object%s to NetBox %s "
                "object%s.",
                len(vc_objects[nb_obj_type]),
                vc_obj_type,
                "s" if len(vc_objects[nb_obj_type]) != 1 else "",
                nb_obj_type,
                "s" if len(vc_objects[nb_obj_type]) != 1 else "",
                )
        # Send vCenter objects to the pruner
        if settings.NB_PRUNE_ENABLED:
            self.prune_objects(vc_objects, vc_obj_type)

    def prune_objects(self, vc_objects, vc_obj_type):
        """
        Collects NetBox objects and checks if they still exist in vCenter.

        If NetBox objects are not found in the supplied vc_objects data then
        they will go through a pruning process.

        :param vc_data: Nested dict of extracted vCenter objects sorted by NetBox object type keys
        :type vc_objects: dict
        :param vc_obj_type: vCenter object type to extract, must be key in obj_type_map
        :type vc_obj_type: str
        """
        # Determine qualifying object types based on object map
        nb_obj_types = [t for t in vc_objects if self.obj_map[t]["prune"]]
        # Sort qualify NetBox object types by prune priority. This ensures
        # we do not have issues with deleting due to orphaned dependencies.
        nb_obj_types = sorted(
            nb_obj_types, key=lambda t: self.obj_map[t]["prune_pref"],
            reverse=True
            )
        for nb_obj_type in nb_obj_types:
            log.info(
                "Comparing existing NetBox %s objects to current vCenter "
                "objects for pruning eligibility.", nb_obj_type
                )
            nb_objects = self.request(
                req_type="get", nb_obj_type=nb_obj_type,
                # Tags need to always be searched by slug
                query="?tag={}".format(format_slug(self.vc_tag))
                )["results"]
            # Certain vCenter object types map to multiple NetBox types. We
            # define the relationships to compare against for these situations.
            if vc_obj_type == "hosts":
                if nb_obj_type == "interfaces":
                    nb_objects = [
                        obj for obj in nb_objects
                            if obj["device"] is not None
                    ]
                elif nb_obj_type == "ip_addresses":
#                    pprint(nb_objects)
                    nb_objects = [
                        obj for obj in nb_objects
                            if obj.get("assigned_object") is not None and obj.get("assigned_object").get("device") is not None
                    ]
            # Issue 33: As of NetBox v2.6.11 it is not possible to filter
            # virtual interfaces by tag. Therefore we filter post collection.
            elif vc_obj_type == "virtual_machines":
                if nb_obj_type == "virtual_interfaces":
                    nb_objects = [
                        obj for obj in nb_objects
                            if self.vc_tag in obj["tags"]
                    ]
                    log.debug(
                        "Found %s virtual interfaces with tag '%s'.",
                        len(nb_objects), self.vc_tag
                    )
                elif nb_obj_type == "ip_addresses":
                    nb_objects = [
                        obj for obj in nb_objects
                            if obj.get("assigned_object") is not None and obj.get("assigned_object").get("virtual_machine") is not None
                    ]
            # From the vCenter objects provided collect only the names/models of
            # each object from the current type we're comparing against
            query_key = self.obj_map[nb_obj_type]["key"]
            vc_obj_values = [obj[query_key] for obj in vc_objects[nb_obj_type]]
            orphans = [
                obj for obj in nb_objects if obj[query_key] not in vc_obj_values
                ]
            log.info(
                "Comparison completed. %s %s orphaned NetBox object%s did not "
                "match.",
                len(orphans), nb_obj_type, "s" if len(orphans) != 1 else ""
                )
            log.debug("The following objects did not match: %s", orphans)
            # Pruned items are checked against the prune timer
            # All pruned items are first tagged so it is clear why they were
            # deleted, and then those items which are greater than the max age
            # will be deleted permanently
            for orphan in orphans:
                log.info(
                    "Processing orphaned NetBox %s '%s' object.",
                    nb_obj_type, orphan[query_key]
                    )
                if "Orphaned" not in [d.get("name") for d in orphan["tags"]]:
                    log.info(
                        "No tag found. Adding 'Orphaned' tag to %s '%s' "
                        "object.",
                        nb_obj_type, orphan[query_key]
                        )
                    log.debug("Merging existing tags with `Orphaned`.")
                    tags = {
                        "tags": list(
                            {'name':x['name']} for x in orphan["tags"]
                        ) + [{"name": "Orphaned"}]
                        }
                    self.request(
                        req_type="patch", nb_obj_type=nb_obj_type,
                        nb_id=orphan["id"],
                        data=tags
                        )
                else:
                    # Check if the orphan has gone past the max prune timer and
                    # needs to be deleted
                    # Dates are in YY, MM, DD format
                    current_date = date.today()
                    # Some objects do not have a last_updated field so we must
                    # handle that gracefully and send for deletion
                    del_obj = False
                    try:
                        modified_date = date(
                            int(orphan["last_updated"][:4]), # Year
                            int(orphan["last_updated"][5:7]), # Month
                            int(orphan["last_updated"][8:10]) # Day
                            )
                        # Calculated timedelta then converts it to the days
                        # integer
                        days_orphaned = (current_date - modified_date).days
                        # Support keeping orphaned objects if they contan the
                        # right tag for #89
                        if bool(days_orphaned >= settings.NB_PRUNE_DELAY_DAYS
                                and "Manual" in orphan["tags"]):
                            log.info(
                                "The %s '%s' object has exceeded the %s day "
                                "max for orphaned objects however it contains "
                                "the 'Manual' tag. Skipping deletion.",
                                nb_obj_type, orphan[query_key],
                                settings.NB_PRUNE_DELAY_DAYS
                                )
                        elif days_orphaned >= settings.NB_PRUNE_DELAY_DAYS:
                            log.info(
                                "The %s '%s' object has exceeded the %s day "
                                "max for orphaned objects. Sending it for "
                                "deletion.",
                                nb_obj_type, orphan[query_key],
                                settings.NB_PRUNE_DELAY_DAYS
                                )
                            del_obj = True
                        else:
                            log.info(
                                "The %s '%s' object has been orphaned for %s "
                                "of %s max days. Proceeding to next object.",
                                nb_obj_type, orphan[query_key], days_orphaned,
                                settings.NB_PRUNE_DELAY_DAYS
                                )
                    except KeyError as err:
                        log.debug(
                            "The %s '%s' object does not have a %s "
                            "field. Sending it for deletion.",
                            nb_obj_type, orphan[query_key], err
                            )
                        del_obj = True
                    if del_obj:
                        self.request(
                            req_type="delete", nb_obj_type=nb_obj_type,
                            nb_id=orphan["id"],
                            )

    def search_prefix(self, ip_addr):
        """
        Queries Netbox for the parent prefix of any supplied IP address.

        :param ip_addr: IP address
        :type ip_addr: str
        :return: The VRF and tenant name of the prefix containing :param ip_addr:
        :rtype: dict
        """
        result = {"tenant": None, "vrf": None}
        query = ("?contains={}".format(ip_addr)
                 if self.vrf_id is None
                 else "?contains={}&vrf_id={}".format(ip_addr, self.vrf_id))
        try:
            prefix_obj = self.request(
                req_type="get", nb_obj_type="prefixes", query=query
                )["results"][-1] # -1 used to choose the most specific result
            prefix = prefix_obj["prefix"]
            for key in result:
                # Ensure the data returned was not null.
                try:
                    result[key] = {"name": prefix_obj[key]["name"]}
                except TypeError:
                    log.debug(
                        "No %s key was found in the parent prefix. Nulling.",
                        key
                        )
                    result[key] = None
            log.debug(
                "IP address %s is a child of prefix %s with the following "
                "attributes: %s", ip_addr, prefix, result
                )
        except IndexError:
            log.debug("No parent prefix was found for IP %s.", ip_addr)
        return result

    def verify_dependencies(self):
        """
        Validates that all prerequisite NetBox objects exist and creates them.
        """
        nbt = Templates(api_version=self.nb_api_version)
        tag_dependencies = [
            nbt.tag(
                name="Orphaned",
                color="607d8b",
                description="The source system which has previously"
                            "provided the object no longer "
                            "states it exists.{}".format(
                                " An object with the 'Orphaned' tag will "
                                "remain in this state until it ages out "
                                "and is automatically removed."
                                ) if settings.NB_PRUNE_ENABLED else ""
            ),
            nbt.tag(
                name=self.vc_tag,
                description="Objects synced from vCenter host "
                            "{}. Be careful not to modify the name or "
                            "slug.".format(self.vc_tag)
            ),
            nbt.tag(
                name="vCenter",
                description="Created and used by vCenter NetBox sync."
            ),
            nbt.tag(
                name="Synced",
                description="Created and used by vCenter NetBox sync."
            )
        ]

        log.info("Creating Tags if not present")
        for tag in tag_dependencies:
            self.obj_exists(nb_obj_type="tags", vc_data=tag)

        log.info("Finished creating tags.")

        # retrieve tags from current NetBox instance
        self.get_instance_tags()

        dependencies = {
            "manufacturers": [
                {"name": "VMware", "slug": "vmware"},
                ],
            "platforms": [
                {"name": "VMware ESXi", "slug": "vmware-esxi"},
                ],
            "sites": [{
                "name": "vCenter",
                "slug": "vcenter",
                "comments": "A default virtual site created to house objects "
                            "that have been synced from vCenter.",
                "tags": ["Synced", "vCenter"]
                }],
            "cluster_types": [
                {"name": "VMware ESXi", "slug": "vmware-esxi"}
                ],
            "clusters": [{
                "name": "Standalone ESXi Host",
                "type": {"name": "VMware ESXi"},
                "comments": "A default cluster created to house standalone "
                            "ESXi hosts and VMs that have been synced from "
                            "vCenter.",
                "tags": ["Synced", "vCenter"]
                }],
            "device_roles": [
                {
                    "name": settings.DEVICE_ROLE_VM,
                    "vm_role": True
                }],
            }
        if settings.DEVICE_ROLE_VM != settings.DEVICE_ROLE_HOST:
            dependencies["device_roles"].append({
                "name": settings.DEVICE_ROLE_HOST,
            })
        # For each dependency of each type verify object exists
        log.info("Verifying all prerequisite objects exist in NetBox.")
        for dep_type in dependencies:
            log.debug(
                "Checking that NetBox has necessary %s objects.", dep_type[:-1]
                )
            for dep in dependencies[dep_type]:
                self.obj_exists(nb_obj_type=dep_type, vc_data=dep)
        log.info("Finished verifying prerequisites.")

    def get_instance_tags(self):
        """
        retrieve all tags from this NetBox instance
        """

        log.debug("Retrieve all tags from this NetBox instance")

        req = self.request(
            req_type="get", nb_obj_type="tags")

        self.instance_tags = req.get("results")

    def update_interface_data(self, interface_type, interface_data):
        """
        Add NetBox interface to local lookup list so we are able to
        assign a interface to a NetBox ip_addresses ressource
        """

        object_name = None
        if interface_type == "virtual_interfaces":
            object_name = interface_data.get("virtual_machine").get("name")
        if interface_type == "interfaces":
            object_name = interface_data.get("device").get("name")

        interface_name = interface_data.get("name")

        if object_name is not None and interface_name is not None:
            key = "{}/{}".format(object_name, interface_name)
            if interface_type == "virtual_interfaces":
                self.instance_virtual_interfaces[key] = interface_data
            if interface_type == "interfaces":
                self.instance_interfaces[key] = interface_data

    def remove_all(self):
        """
        Searches NetBox for all synced objects and then removes them.

        This is intended to be used in the case you wish to start fresh or stop
        using the script.
        """
        log.info("Preparing for removal of all NetBox synced vCenter objects.")
        nb_obj_types = [
            t for t in self.obj_map if self.obj_map[t]["prune"]
            ]
        # Honor preference, highest to lowest
        nb_obj_types = sorted(
            nb_obj_types, key=lambda t: self.obj_map[t]["prune_pref"],
            reverse=True
            )
        for nb_obj_type in nb_obj_types:
            log.info(
                "Collecting all current NetBox %s objects to prepare for "
                "deletion.", nb_obj_type
                )
            query = "?tag={}".format(
                # vCenter site is a global dependency so we change the query
                "vcenter" if nb_obj_type == "sites"
                else format_slug(self.vc_tag)
                )
            nb_objects = self.request(
                req_type="get", nb_obj_type=nb_obj_type,
                query=query
                )["results"]
            query_key = self.obj_map[nb_obj_type]["key"]
            # NetBox virtual interfaces do not currently support filtering
            # by tags. Therefore we collect all virtual interfaces and
            # filter them post collection.
            if nb_obj_type == "virtual_interfaces":
                log.debug("Collected %s virtual interfaces pre-filtering.", len(nb_objects))
                nb_objects = [
                    obj for obj in nb_objects if self.vc_tag in obj["tags"]
                    ]
                log.debug(
                    "Filtered to %s virtual interfaces with '%s' tag.",
                    len(nb_objects), self.vc_tag
                    )
            log.info(
                "Deleting %s NetBox %s objects.", len(nb_objects), nb_obj_type
                )
            for obj in nb_objects:
                log.info(
                    "Deleting NetBox %s '%s' object.", nb_obj_type,
                    obj[query_key]
                    )
                self.request(
                    req_type="delete", nb_obj_type=nb_obj_type,
                    nb_id=obj["id"],
                    )


if __name__ == "__main__":
    main()
