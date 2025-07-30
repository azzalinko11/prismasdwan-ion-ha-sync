#!/usr/bin/env python
"""
Script to copy VLAN interfaces (SVIs or Subinterfaces) and static routes from one ION to the other)
Author: Aaron @ PANW 
Version: 1.0.0b3 (Modified for static routes, VRF and put fucntions for static routes and interfaces)
"""
##############################################################################
# Import Libraries
##############################################################################
import prisma_sase
import argparse
import sys
import copy

##############################################################################
# Prisma SD-WAN Auth Token
##############################################################################
try:
    from prismasase_settings import PRISMASASE_CLIENT_ID, PRISMASASE_CLIENT_SECRET, PRISMASASE_TSG_ID
except ImportError:
    PRISMASASE_CLIENT_ID = None
    PRISMASASE_CLIENT_SECRET = None
    PRISMASASE_TSG_ID = None
    print("WARNING: prismasase_settings.py not found. Ensure PRISMASASE_CLIENT_ID, PRISMASASE_CLIENT_SECRET, and PRISMASASE_TSG_ID are set as environment variables or hardcoded.")
    if not (PRISMASASE_CLIENT_ID and PRISMASASE_CLIENT_SECRET and PRISMASASE_TSG_ID):
        print("ERR: Prisma SASE credentials (CLIENT_ID, CLIENT_SECRET, TSG_ID) are not configured.")
        sys.exit(1)

#############################################################################
# Global Variables
#############################################################################
elem_id_name = {}
elem_name_id = {}
elemid_siteid = {}
elem_id_model = {}
element_vrfs_by_id = {}    
element_vrfs_by_name = {}  
element_interfaces_by_id = {}  
element_interfaces_by_name = {} 
global_id_vfr = {}
global_name_vfr = {}

# Attributes to always remove from payload before POST/PUT (read-only from GET)
# 'id' and '_etag' are handled conditionally based on POST/PUT
BASE_DELETE_ATTRS = ["_created_on_utc", "_updated_on_utc", "_content_length", "_status_code", "_request_id", "_debug", "_info", "_warning", "_error"]

def create_dicts(sase_session):
    """
    Populates global dictionaries for elements, VRFs, and Interfaces.
    Interfaces and VRFs are stored with element-specific context for accurate translation.
    """
    #
    # Elements
    #
    resp = sase_session.get.elements()
    if resp.cgx_status:
        elemlist = resp.cgx_content.get("items", [])
        for elem in elemlist:
            elem_id_name[elem["id"]] = elem["name"]
            elem_name_id[elem["name"]] = elem["id"]
            elemid_siteid[elem["id"]] = elem["site_id"]
            elem_id_model[elem["id"]] = elem["model_name"]
            
            element_vrfs_by_id[elem["id"]] = {}
            element_vrfs_by_name[elem["id"]] = {}
            element_interfaces_by_id[elem["id"]] = {}
            element_interfaces_by_name[elem["id"]] = {}
    else:
        print("ERR: Could not retrieve elements during dictionary creation.")
        prisma_sase.jd_detailed(resp)
        sys.exit(1)

    #
    # VRF
    #
    respvrf = sase_session.get.vrfcontexts()
    if respvrf.cgx_status:
        vrflist = respvrf.cgx_content.get("items", [])
        for vrf in vrflist:
            global_id_vfr[vrf["id"]] = vrf["name"]
            global_name_vfr[vrf["name"]] = vrf["id"]
    else:
        print("ERR: Could not retrieve VRFs during dictionary creation.")
        prisma_sase.jd_detailed(respvrf)
        sys.exit(1)

    #
    # Interface (Populate element-specific interface dictionaries)
    #
    for elem_id, elem_name in elem_id_name.items():
        site_id = elemid_siteid.get(elem_id)
        if site_id: # Ensure we have a site_id for the element
            respint = sase_session.get.interfaces(element_id=elem_id, site_id=site_id)
            if respint.cgx_status:
                intlist = respint.cgx_content.get("items", [])
                for intf in intlist:
                    # Populate element-specific interface dictionaries
                    element_interfaces_by_id[elem_id][intf["id"]] = intf["name"]
                    element_interfaces_by_name[elem_id][intf["name"]] = intf["id"]
            else:
                print(f"ERR: Could not retrieve interfaces for element {elem_name} (ID: {elem_id}) during dictionary creation.")
                prisma_sase.jd_detailed(respint)
                # Continue to next element if one fails, but log the error
        else:
            print(f"WARN: No site ID found for element {elem_name} (ID: {elem_id}). Skipping interface retrieval for this element.")
    return

def go():
    #############################################################################
    # Begin Script
    #############################################################################
    """Main function to copy VLAN interfaces and static routes."""
    parser = argparse.ArgumentParser(description="{0}.".format("Prisma SD-WAN Configuration Copy Script"))
    config_group = parser.add_argument_group('Config', 'Details for the ION devices you wish to update')
    config_group.add_argument("--src_element", "-S", help="Source Element Name", default=None, required=True)
    config_group.add_argument("--dst_element", "-D", help="Destination Element Name", default=None, required=False)
    config_group.add_argument("--parent_interface", "-P", help="Parent Interface Name (currently unused in main logic)", default=None)
    #############################################################################
    # Parse Arguments
    #############################################################################
    args = vars(parser.parse_args())
    src_element = args.get("src_element")
    dst_element = args.get("dst_element")
    parent_interface = args.get("parent_interface")

    if dst_element == src_element:
        print("ERR: Source and Destination Elements cannot be the same.")
        sys.exit(1)

    ##############################################################################
    # Login
    ##############################################################################
    sase_session = prisma_sase.API()
    sase_session.interactive.login_secret(client_id=PRISMASASE_CLIENT_ID,
                                          client_secret=PRISMASASE_CLIENT_SECRET,
                                          tsg_id=PRISMASASE_TSG_ID)
    if sase_session.tenant_id is None:
        print("ERR: Login Failure. Please ensure your service account credentials are valid.")
        sys.exit(1)
    
    ##############################################################################
    # Create Translation Dicts
    ##############################################################################
    print("Building Translation Dicts...")
    create_dicts(sase_session=sase_session)

    ##############################################################################
    # Validate Element Names and Retrieve IDs
    ##############################################################################
    if src_element not in elem_name_id:
        print(f"ERR: Source Element '{src_element}' not found! Please provide a valid name.")
        sys.exit(1)
    if dst_element not in elem_name_id:
        print(f"ERR: Destination Element '{dst_element}' not found! Please provide a valid name.")
        sys.exit(1)

    src_eid = elem_name_id[src_element]
    src_sid = elemid_siteid[src_eid]
    dst_eid = elem_name_id[dst_element]
    dst_sid = elemid_siteid[dst_eid]

    print(f"Copying from Source Element: {src_element} (ID: {src_eid}) to Destination Element: {dst_element} (ID: {dst_eid})")

    ##############################################################################
    ### Copying Static Routes
    ##############################################################################

    # Retrieve static routes from SOURCE
    print("\n--- Copying Static Routes ---")
    src_static_routes_resp = sase_session.get.staticroutes(site_id=src_sid, element_id=src_eid)
    if src_static_routes_resp.cgx_status:
        source_static_routes_list = src_static_routes_resp.cgx_content.get("items", [])
        print(f"Retrieved {len(source_static_routes_list)} static routes from source.")
    else:
        print("ERR: Could not retrieve static routes from source element.")
        prisma_sase.jd_detailed(src_static_routes_resp)
        sys.exit(1)

    # Retrieve static routes from DESTINATION for existing check (idempotent copy)
    dst_static_routes_resp = sase_session.get.staticroutes(site_id=dst_sid, element_id=dst_eid)
    if dst_static_routes_resp.cgx_status:
        destination_static_routes_list = dst_static_routes_resp.cgx_content.get("items", [])
        # Create a lookup dictionary for destination routes by destination_prefix for efficient checking
        dst_routes_by_prefix = {route["destination_prefix"]: route for route in destination_static_routes_list}
        print(f"Retrieved {len(destination_static_routes_list)} static routes from destination for comparison.")
    else:
        print("ERR: Could not retrieve static routes from destination element to check for existing routes. Exiting to prevent unintended duplicates or errors.")
        prisma_sase.jd_detailed(dst_static_routes_resp)
        sys.exit(1)

    for source_route in source_static_routes_list:
        route_payload = copy.deepcopy(source_route)

        # Remove common read-only attributes
        for attr in BASE_DELETE_ATTRS:
            route_payload.pop(attr, None)

        source_vrf_context_id = route_payload.get("vrf_context_id")
        if source_vrf_context_id: 
            vrf_name = global_id_vfr.get(source_vrf_context_id)
            if vrf_name:
                new_vrf_context_id = global_name_vfr.get(vrf_name)
                if new_vrf_context_id:
                    route_payload["vrf_context_id"] = new_vrf_context_id
                else:
                    print(f"WARN: VRF '{vrf_name}' (Source ID: {source_vrf_context_id}) not found in global VRF list (destination mapping). Skipping route {route_payload['destination_prefix']}")
                    continue 
            else:
                print(f"WARN: Could not find name for source VRF ID {source_vrf_context_id}. Skipping route {route_payload['destination_prefix']}")
                continue 

        if route_payload.get("nexthops"):
            for nexthop in route_payload["nexthops"]:
                source_nexthop_interface_id = nexthop.get("nexthop_interface_id")
                if source_nexthop_interface_id:
                    interface_name = element_interfaces_by_id[src_eid].get(source_nexthop_interface_id)
                    if interface_name:
                        # Get the *new* interface ID from the destination element's interfaces
                        new_nexthop_interface_id = element_interfaces_by_name[dst_eid].get(interface_name)
                        if new_nexthop_interface_id:
                            nexthop["nexthop_interface_id"] = new_nexthop_interface_id
                        else:
                            print(f"WARN: Interface '{interface_name}' (Source ID: {source_nexthop_interface_id}) found on source, but no matching interface found on destination element '{dst_element}'. Skipping route {route_payload['destination_prefix']}")
                            # If a nexthop interface can't be found, the route is likely invalid on the destination.
                            continue # Skip this route if the destination interface cannot be found
                    else:
                        print(f"WARN: Could not find name for source interface ID {source_nexthop_interface_id} on source element '{src_element}'. Skipping route {route_payload['destination_prefix']}")
                        continue # Skip this route if source interface name lookup failed
                # If nexthop_ip is used, it's copied as-is.

        # Determine if we need to POST (create) or PUT (update)
        destination_prefix_to_check = route_payload["destination_prefix"]
        existing_dst_route = dst_routes_by_prefix.get(destination_prefix_to_check)
        
        if existing_dst_route:
            # If the route exists on the destination, UPDATE it
            route_id_to_update = existing_dst_route["id"]
            
            # Get the current ETag from the existing destination route
            current_etag = existing_dst_route.get("_etag")
            if current_etag is not None:
                route_payload["_etag"] = current_etag # Add the current ETag for the PUT operation
            else:
                print(f"WARN: ETag not found for existing destination route {route_payload['name']} (ID: {route_id_to_update}). Attempting PUT without ETag, which might fail.")
            
            # Ensure the 'id' in the payload is the destination route's ID
            route_payload["id"] = route_id_to_update 

            print(f"  Attempting to UPDATE static route: {route_payload['destination_prefix']}...")
            resp = sase_session.put.staticroutes(site_id=dst_sid, element_id=dst_eid, staticroute_id=route_id_to_update, data=route_payload)
            if resp.cgx_status:
                print(f"  SUCCESS: Static route {route_payload['destination_prefix']} UPDATED on destination element.")
            else:
                print(f"  ERR: Could not UPDATE static route {route_payload['destination_prefix']} on destination element.")
                prisma_sase.jd_detailed(resp)
        else:
            route_payload.pop("id", None) # Ensure 'id' is removed for POST operations
            route_payload.pop("_etag", None) # Ensure '_etag' is removed for POST operations as it's not applicable

            print(f"  Attempting to CREATE static route: {route_payload['destination_prefix']}...")
            resp = sase_session.post.staticroutes(site_id=dst_sid, element_id=dst_eid, data=route_payload)
            if resp.cgx_status:
                print(f"  SUCCESS: Static route {route_payload['destination_prefix']} CREATED on destination element.")
            else:
                print(f"  ERR: Could not CREATE static route {route_payload['destination_prefix']} on destination element.")
                prisma_sase.jd_detailed(resp)
    
    ##############################################################################
    ### Copying VLAN Interfaces (SVIs or Subinterfaces)
    ##############################################################################

    print("\n--- Copying VLAN Interfaces (SVIs or Subinterfaces) ---")
    interfaces = sase_session.get.interfaces(site_id=src_sid, element_id=src_eid)
    if interfaces.cgx_status:
        interface_list = interfaces.cgx_content.get("items", [])
        print(f"Retrieved {len(interface_list)} interfaces from source.")
    else:
        print("ERR: Could not retrieve interfaces from source element.")
        prisma_sase.jd_detailed(interfaces)
        sys.exit(1)

    # Retrieve existing VLAN interfaces from destination to prevent duplicates
    dst_interfaces_resp = sase_session.get.interfaces(site_id=dst_sid, element_id=dst_eid)
    if dst_interfaces_resp.cgx_status:
        destination_interface_list = dst_interfaces_resp.cgx_content.get("items", [])
        dst_interfaces_by_name = {intf["name"]: intf for intf in destination_interface_list if intf["type"] in ["vlan", "subinterface"]}
        print(f"Retrieved {len(dst_interfaces_by_name)} existing VLAN interfaces from destination for comparison.")
    else:
        print("ERR: Could not retrieve VLAN interfaces from destination element to check for existing interfaces. Exiting to prevent unintended duplicates or errors.")
        prisma_sase.jd_detailed(dst_interfaces_resp)
        sys.exit(1)

    for intf in interface_list:
        if intf["type"] == "vlan" or intf["type"] == "subinterface":
            intf_payload = copy.deepcopy(intf)
            
            # Remove common read-only attributes
            for attr in BASE_DELETE_ATTRS:
                intf_payload.pop(attr, None)

            existing_dst_intf = dst_interfaces_by_name.get(intf_payload["name"])

            if existing_dst_intf:
                # If the interface exists on the destination, UPDATE it
                intf_id_to_update = existing_dst_intf["id"]
                current_etag = existing_dst_intf.get("_etag")

                if current_etag is not None:
                    intf_payload["_etag"] = current_etag
                else:
                    print(f"WARN: ETag not found for existing destination interface {intf_payload['name']}. Attempting PUT without ETag, which might fail.")
                
                intf_payload["id"] = intf_id_to_update

                print(f"  Attempting to UPDATE interface: {intf_payload['name']} (Type: {intf_payload['type']})...")
                resp = sase_session.put.interfaces(site_id=dst_sid, element_id=dst_eid, interface_id=intf_id_to_update, data=intf_payload)
                if resp.cgx_status:
                    print(f"  SUCCESS: Interface {intf_payload['name']} UPDATED on destination element.")
                else:
                    print(f"  ERR: Could not UPDATE interface {intf_payload['name']} on destination element.")
                    prisma_sase.jd_detailed(resp)
            else:
                # If the interface does not exist, CREATE it
                intf_payload.pop("id", None)
                intf_payload.pop("_etag", None)

                print(f"  Attempting to CREATE interface: {intf_payload['name']} (Type: {intf_payload['type']})...")
                resp = sase_session.post.interfaces(site_id=dst_sid, element_id=dst_eid, data=intf_payload)
                if resp.cgx_status:
                    print(f"  SUCCESS: Interface {intf_payload['name']} CREATED on destination element.")
                else:
                    print(f"  ERR: Could not CREATE interface {intf_payload['name']} on destination element.")
                    prisma_sase.jd_detailed(resp)
    return

if __name__ == "__main__":
    go()
