#!/usr/bin/env python3

from .sql import *
from .lists import Lists

import re
from scapy.all import sniff,ARP,wrpcap,rdpcap,sr
from pathlib import Path
from time import sleep
from os import remove
from tabulate import tabulate
from multiprocessing.pool import Pool
from dns import reversename, resolver

# TODO: To make this more like an API, move printing/coloring/output structuring
# to the interface section

def validate_file_presence(func):
    '''Determine if a file is found on the local filesystem.
    '''

    def wrapper(fname,*args,**kwargs):

        p = Path(fname)
        if p.exists() and p.is_file():
            return func(fname,*args,**kwargs)
        else:
            raise Exception(
                f'File not found: {fname}'
            )

    return wrapper


def validate_packet_unpack(func):
    '''Validate a packet to be ARP and return the
    ARP layer.
    '''

    def wrapper(packet,*args,**kwargs):

        arp = validate_packet(packet)

        if not arp:
            return False
        else:
            return func(unpack_arp(arp), *args, **kwargs)
        

    return wrapper


def unpack_packets(func):
    '''Unpack a list of packets to src/dst addresses.
    '''
    
    def wrapper(packets, *args, **kwargs):

        packets = [unpack_packet(packet) for packet in packets]

        return func(packets, *args, **kwargs)

    return wrapper

# =========
# FUNCTIONS
# =========

def arp_request(interface,target,verbose=0,retry=0,timeout=1):
    '''Attempt to make an ARP request for the target. Returns
    the MAC address for the target if successful, None otherwise.
    '''

    results, unanswered = sr(
        ARP(
            op=1,
            pdst=target,
        ),
        iface=interface,
        retry=retry,
        verbose=verbose,
        timeout=timeout
    )

    if results:
        return results[0][1].hwsrc
    else:
        return None

def validate_packet(packet,unpack=True):
    '''Validate a packet to be of type ARP. Leave unpack to True and
    the returned object will be ARP instead of Boolean.
    '''

    if ARP in packet:
        arp = packet.getlayer('ARP')
        if arp.op == 1:
            if unpack: return arp
            else: return True

    return False

def unpack_arp(arp):
    '''Validate a packet while returning the target and sender
    in a tuple: `(target,sender)`
    '''

    return arp.psrc,arp.pdst

def unpack_packet(packet):
    '''Extract and return the ARP layer from a packet object.
    '''

    return unpack_arp(packet.getlayer('ARP'))

def get_output(db_session,order_by=desc,sender_lists=None,
        target_lists=None,ptr=False,color_profile=None,
        resolve=True,active=False):
    '''Extract transaction records from the database and return
    them formatted as a table.
    '''

    # Getting all transaction objects
    transactions = db_session.query(Transaction) \
            .order_by(desc(Transaction.count)) \
            .all()

    if not transactions:
        output = '- No accepted ARP requests captured\n' \
        '- If this is unexpected, check your whitelist/blacklist configuration'
        return output

    # Organize all the records by sender IP
    rowdict = {}
    for t in transactions:
            
        sender = t.sender.value
        target = t.target.value

        if sender_lists and not sender_lists.check(sender):
            continue
        if target_lists and not target_lists.check(target):
            continue

        # Flag to determine if the sender is new
        if sender not in rowdict: new_sender = True
        else: new_sender = False

        # Include sender only on new instances
        if new_sender: row = [sender,target]
        else: row = ['',target]

        # Stale IP
        if active:

            sploit_char = ''
            if not t.target.mac_address and t.target.arp_resolve_attempted:

                if color_profile and color_profile.target_emoji:
                    sploit_char = color_profile.target_emoji
                else:
                    sploit_char = 'X'

            row.append(sploit_char)

        # Target count
        row.append(str(t.count))

        # Reverse name resolution
        if resolve:

            sptr = ''
            tptr = ''

            if t.sender.ptr: sptr = t.sender.ptr[0].value
            if t.target.ptr: tptr = t.target.ptr[0].value

            if new_sender: row.append(sptr)
            else: row.append('')

            row.append(tptr)

        if new_sender: rowdict[sender] = [row]
        else: rowdict[sender].append(row)

    # Restructure dictionary into a list of rows
    rows = []
    counter = 0

    for sender,irows in rowdict.items():
        counter += 1

        # Color odd rows slightly darker
        if color_profile:

            if counter % 2:
                rows += [color_profile.style_odd([v for v in r]) for r in irows]
            else:
                rows += [color_profile.style_even(r) for r in irows]

        # Just add the rows otherwise
        else: rows += irows

    # Build the header
    headers = ['Sender','Target','ARP#']
    if active: headers.insert(2,'TS')
    if resolve: headers += ['Sender PTR','Target PTR']

    # Color the headers
    if color_profile: headers = color_profile.style_header(headers)

    # Return the output as a table
    return tabulate(
            rows,
            headers=headers)

def create_db(dbfile,overwrite=False):
    '''Initialize the database file and return a session
    object.
    '''

    engine = create_engine(f'sqlite:///{dbfile}')
    Session = sessionmaker()
    Session.configure(bind=engine)

    pth = Path(dbfile)

    # Remove the file if specified
    if pth.exists() and overwrite:
        remove(pth)

    # Don't clobber pre-existing database files
    if not Path(dbfile).exists() or overwrite:
        Base.metadata.create_all(engine)

    return Session()

def reverse_resolve(ip):
    '''Attempt reverse name resolution on an IP address. Returns
    `None` upon exception, which occurs when an address without a
    PTR record is requested.
    '''
    
    try:
        rev_name = reversename.from_address(ip)
        return resolver.query(rev_name,'PTR')[0].__str__()
    except:
        return None

@validate_packet_unpack
def filter_packet(packet,sender_lists=None,target_lists=None):
    '''Filter an individual packet. This should be executed in the `lambda`
    supplied to `do_sniff`. `sender_lists` and `target_lists` should be 
    objects of type `List()`.
    '''

    if not packet: return False
    sender,target = packet

    if sender_lists:
        if not sender_lists.check(sender):
            return False
    if target_lists:
        if not target_lists.check(target):
            return False

    return packet

def get_or_create_ip(ip, db_session, resolve=False, ptr=None,
        interface=None, active=False):
    '''Get or create an IP object from the SQLite database. Also
    handles:

    - Reverse Name Resolution
    - ARP resolution
    '''

    db_ip = db_session.query(IP).filter(IP.value==ip).first()

    if not db_ip:

        ip = IP(value=ip)
        db_session.add(ip)
        db_session.commit()

        # Obtain and set the PTR record for a given IP
        if ptr:

            db_session.add(
                PTR(ip_id=ip.id,value=ptr)
            )

        # Attempt to resolve the PTR address
        elif resolve:

            ptr = reverse_resolve(ip.value)

            if ptr:

                db_session.add(
                    PTR(ip_id=ip.id,value=ptr)
                )

        # Handle ARP request if active is enabled
        if active and not ip.arp_resolve_attempted:

            hwaddr = arp_request(interface,
                ip.value)
            if hwaddr: ip.mac_address = hwaddr
            ip.arp_resolve_attempted = True
            db_session.commit()

        db_session.commit()

    else: ip = db_ip

    return ip

@unpack_packets
def handle_packets(packets,db_session,resolve=False,
        active=False,interface=None):
    '''Handle packets capture from the interface.
    '''

    if active:

        if not interface:
            raise Exception(
                'Active ARP resolution requires an interface ip'
            )

    for packet in packets:

        sender,target = packet

        # GET/CREATE database objects
        sender = get_or_create_ip(packet[0],db_session,resolve)
        target = get_or_create_ip(packet[1],db_session,resolve,
            active=active, interface=interface)

        # Determine if a transaction record for the 
          # target/sender pair exists
            # if not, create it
            # else, get and increment it
        transaction = db_session.query(Transaction) \
            .filter(
                Transaction.sender_ip_id==sender.id,
                Transaction.target_ip_id==target.id
            ).first()

        if not transaction:

            transaction = Transaction(sender_ip_id=sender.id,
                    target_ip_id=target.id)
            db_session.add(transaction)

        else:

            transaction.count += 1

        db_session.commit()

def do_sniff(interfaces,redraw_frequency,sender_lists,target_lists):
    '''Start the sniffer while filtering for WHO-HAS broadcast requests.
    '''

    return sniff(iface=interfaces,
        lfilter=lambda pkt: filter_packet(pkt,sender_lists,target_lists),
        count=redraw_frequency
    )
   
def async_sniff(interface, redraw_frequency, sender_lists,
        target_lists, dbfile, analysis_output_file=None, resolve=False, 
        color_profile=None, verbose=False, active=False):
    '''This function should be started in a distinct process, allowing
    the one to CTRL^C during execution and gracefully exit the sniffer.
    Not starting the sniffer in a distinct process results in it blocking
    forever or until an inordinate number of keyboard interrupts occur.
    '''

    # Handle new database file. When verbose, alert user that a new
    # capture must occur prior to printing results.
    if not Path(dbfile).exists():

        if verbose: print(
            '- Initializing capture\n- This may take time depending '\
            'on network traffic and filter configurations'
        )
        sess = create_db(dbfile)

    # Dump the existing database to stdout prior to sniffing.
    else:
        
        sess = create_db(dbfile)
        print('\x1b[2J\x1b[H')
        print(
            get_output(
                sess,
                sender_lists=sender_lists,
                target_lists=target_lists,
                resolve=resolve,
                color_profile=color_profile,
                active=active
            )
        )

    # Capture packets
    packets = do_sniff(interface,
            redraw_frequency, sender_lists,
            target_lists)

    # Handle packets (to the db they go, yo)
    handle_packets(packets,
            sess,
            resolve,
            active,
            interface,
    )

    # output,packets
    return get_output(
            sess,
            sender_lists=sender_lists,
            target_lists=target_lists,
            resolve=resolve,
            color_profile=color_profile,
            active=active),packets

def analyze(database_output_file, sender_lists=None, target_lists=None,
        analysis_output_file=None, pcap_files=[], sqlite_files=[],
        color_profile=None, resolve=True, *args, **kwargs):
    '''Create a new database and populate it with records stored in
    each type of input file.
    '''

    outdb_sess = create_db(database_output_file,overwrite=True)

    # ===================
    # HANDLE SQLITE FILES
    # ===================

    '''
    Import the source database to the new database by reading in
    each transaction. Note that new id values are assigned to
    each IP in the process.
    '''
    for sfile in sqlite_files:
        
        isess = create_db(sfile)

        # ====================================================
        # ITERATE OVER EACH TRANSACTION AND TRANSFER TO NEW DB
        # ====================================================

        for t in isess.query(Transaction).all():

            # ===============
            # HANDLE POINTERS
            # ===============

            if t.sender.ptr: sptr = t.sender.ptr[0].value
            else: sptr=None

            if t.target.ptr: tptr = t.target.ptr[0].value
            else: tptr=None

            # =================
            # HANDLE IP OBJECTS
            # =================

            sender = get_or_create_ip(t.sender.value,outdb_sess,ptr=sptr)
            target = get_or_create_ip(t.target.value,outdb_sess,ptr=tptr)

            # ======================
            # HANDLE THE TRANSACTION
            # ======================

            transaction = outdb_sess.query(Transaction).filter(
                Transaction.sender_ip_id==sender.id,
                Transaction.target_ip_id==target.id).first()

            if transaction:

                it = outdb_sess.query(Transaction).filter(
                    Transaction.sender_ip_id==sender.id,
                    Transaction.target_ip_id==target.id
                ).first()

                it.count += t.count

            else:

                outdb_sess.add(
                    Transaction(
                        sender_ip_id=sender.id,
                        target_ip_id=target.id,
                        count=t.count
                    )
                )

            outdb_sess.commit()

    # =====================
    # HANDLE EACH PCAP FILE
    # =====================

    '''
    This is much easier than SQLITE files since we can just use
    Scapy to slurp the packets from each target file, filter each
    packet, and then use `handle_packets` to populate the database.
    '''

    for pfile in pcap_files:

        # Assure that each packet is ARP WHO-HAS
        packets = [p for p in rdpcap(pfile) if filter_packet(p)]

        # Insert the records
        handle_packets(
            packets,
            outdb_sess
        )

    return get_output(
            outdb_sess,
            sender_lists=sender_lists,
            target_lists=target_lists,
            color_profile=color_profile,
            resolve=resolve)

