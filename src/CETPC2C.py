#!/usr/bin/python3.5

import asyncio
import logging
import signal
import socket
import sys
import random
import time
import traceback
import json
import ssl
import copy
import cetpManager
import C2CTransaction
import H2HTransaction
import CETPH2H
import CETPTransports

LOGLEVEL_CETPC2CLayer          = logging.INFO

class CETPC2CLayer:
    """
    Instantiates client CETP Transports towards remote CES, based on the NAPTR records.
    Manages, registers/unregisters the CETP Transports
        - Management of CETPTransport failover, seamless to H2H-layer.
        - Performs resource cleanup upon CES-to-CES connectivity loss.
    
    Ensures timely negotiation of the CES-to-CES policies, AND forwards C2C-level CETP message to corresponding C2CTransaction, in post-c2c-negotiation phase.
    Provides an API to forward messages to/from H2H Layer, once CES-to-CES is negotiated
    """
    def __init__(self, loop, naptr_list=[], cetp_h2h=None, l_cesid=None, r_cesid=None, cetpstate_mgr=None, policy_mgr=None, policy_client=None, ces_params=None, \
                 cetp_security=None, cetp_mgr=None, conn_table=None, interfaces=None, name="CETPC2CLayer"):
        self._loop                      = loop
        self.naptr_list                 = naptr_list
        self.cetp_h2h                   = cetp_h2h                          # H2H layer manager for remote-cesid 
        self.l_cesid                    = l_cesid
        self.r_cesid                    = r_cesid
        self.cetpstate_mgr              = cetpstate_mgr
        self.policy_client              = policy_client
        self.policy_mgr                 = policy_mgr
        self.ces_params                 = ces_params
        self.cetp_security              = cetp_security
        self.cetp_mgr                   = cetp_mgr
        self.interfaces                 = interfaces
        self.conn_table                 = conn_table
        self.q                          = asyncio.Queue()        # Enqueues the messages from CETP Transport
        self.max_transport_conn         = ces_params["max_rces_transports"]
        self.max_naptrs_per_msg         = 10
        self.pending_tasks              = []                                # oCESC2CLayer specific
        self.initiated_transports       = []
        self.connected_transports       = []
        self.remote_ces_eps             = []                                # Registers ip, port, and protocol info of remote ep.
        self.trusted_rces_eps           = []
        self.unverified_ep_addr         = []
        self.c2c_transaction_list       = []
        self.transport_c2c_binding      = {}
        self.transport_rtt              = {}
        self.transport_lastseen         = {}
        
        self.c2c_negotiated             = False
        self._closure_signal            = False
        self.c2c_connectivity           = False
        self.active_transport           = None
        self._logger                    = logging.getLogger(name)
        self._logger.setLevel(LOGLEVEL_CETPC2CLayer)
        self._logger.info("Initiating CES2CESLayer towards cesid '{}'".format(r_cesid) )


    def add_naptr_records(self, naptr_rrs):
        """ Triggers new transport connections to remote CES, based on addressing in NAPTR records.
            AND, Enforces upper limit on number of transport links to remote CES.
        """
        try:
            if len(naptr_rrs)>self.max_naptrs_per_msg:
                return None            # Could be source of DoS: creating highly traffic load

            if len(self.connected_transports) >= self.max_transport_conn:
                return
            
            possible_transports = self.max_transport_conn - len(self.connected_transports)
            
            for naptr_rr in naptr_rrs:
                dst_id, r_cesid, r_ip, r_port, r_transport = naptr_rr                   # Assumption: All NAPTRs point towards one 'r_cesid'.    (Destination domain is reachable via one CES only)
                key = (r_ip, r_port, r_transport)
                
                if (key in self.remote_ces_eps) or (key in self.unverified_ep_addr):
                    continue
                else:
                    self.remote_ces_eps.append( key )
                    self._logger.info(" Initiating a new transport.")
                    if not self.remote_endpoint_malicious_history(r_cesid, r_ip):
                        asyncio.ensure_future(self.initiate_transport(r_transport, r_ip, r_port, delay=0))        # Delay parameter prevents H2H negotiation from suffering delay due to triggering of transport/C2C-negotiation
                
                        possible_transports -= 1
                        if possible_transports == 0:
                            return
                
        except Exception as ex:
            self._logger.warning("Exception in parsing the NAPTR records: '{}'".format(ex))
            return None

    def _pre_process(self, msg):
        """ Checks whether inbound message conforms to CETP packet format. """
        try:
            cetp_msg = json.loads(msg)
            inbound_sstag, inbound_dstag, ver = cetp_msg['SST'], cetp_msg['DST'], cetp_msg['VER']
            sstag, dstag    = inbound_dstag, inbound_sstag
            cetp_ver = self.ces_params["CETPVersion"]
            
            if ( (sstag==0) and (dstag ==0)) or (sstag < 0) or (dstag < 0):
                self._logger.error(" Session tag values are invalid")
                return False
            
            if ver!=cetp_ver:
                self._logger.info(" CETP version is not supported.")
                return False
            
            return (sstag, dstag, cetp_msg)

        except Exception as ex:
            self._logger.error(" Exception '{}' in pre-processing the received message.".format(ex))
            return False
        
    
    def consume_transport_message(self, msg, transport):
        """ Consumes CETP messages queued by the CETP Transport. """
        try:
            outcome = self._pre_process(msg)
            if not outcome:     return                        # For repeated non-CETP packets, shall we terminate the connection?
            sstag, dstag, cetp_msg = outcome
            self._update_transport(transport)

            if not self.c2c_negotiated:
                self._logger.debug(" C2C-policy is not negotiated with remote CES '{}'".format(self.r_cesid))
                self.process_c2c(sstag, dstag, cetp_msg, transport)
            else:
                if self.is_c2c_transaction(sstag, dstag):
                    self._logger.debug(" Inbound packet belongs to a C2C transaction.")
                    self.process_c2c(sstag, dstag, cetp_msg, transport)
                else:
                    self.forward_h2h(cetp_msg, transport)                       # Forwarding packet to H2H-layer
                    
        except Exception as ex:
            self._logger.info("Exception in consuming messages from CETP Transport: {}".format(ex))
            

    def forward_h2h(self, cetp_msg, transport):
        self.cetp_h2h.consume_message_from_c2c(cetp_msg, transport)
        
    def is_c2c_transaction(self, sstag, dstag):
        """ Determines whether CETP message belongs to an ongoing or completed C2C Transaction of this C2C layer 
        """
        for c2c_transaction in self.c2c_transaction_list:
            c_sst, c_dst = c2c_transaction.sstag, c2c_transaction.dstag
            if (c_sst == sstag) & (c_dst == dstag):                                 # CETP message on a connected transaction, i.e. for C2C feedback & keepalive etc.
                return True                                                         # TBD - searching in a list of C2CTransaction, than an iterating for loop.

        for c2c_transaction in self.c2c_transaction_list:
            c_sst, c_dst = c2c_transaction.sstag, c2c_transaction.dstag
            if ( (c_sst == sstag) and (c_dst==0) and (dstag!=0) ):                  # Ongoing C2CTransaction - completed at iCES, but still in-complete at oCES
                return True
        return False


    def _add_c2c_transactions(self, c2c_transaction):
        self.c2c_transaction_list.append(c2c_transaction)
    
    def _remove_c2c_transactions(self, c2c_transaction):
        if c2c_transaction in self.c2c_transaction_list:
            self.c2c_transaction_list.remove(c2c_transaction)
        
    def get_c2c_transaction(self, transport):
        if transport in self.transport_c2c_binding:
            return self.transport_c2c_binding[transport]
    
    def _add_c2c_transport_binding(self, c2c_transaction, transport):
        self.transport_c2c_binding[transport] = c2c_transaction
    
    def _remove_c2c_transport_binding(self, transport):
        if transport in self.transport_c2c_binding:
            del self.transport_c2c_binding[transport]

    def register_c2c_transport(self, transport, c2c_transaction):
        """ Registers the C2C-Transaction established on a CETPTransport, AND their binding """
        self._add_c2c_transactions(c2c_transaction)
        self._add_connected_transport(transport)
        self._add_c2c_transport_binding(c2c_transaction, transport)
        (ip_addr, port), proto = transport.remotepeer, transport.proto
        transport_ep_info = (ip_addr, port, proto)
        self.remote_ces_eps.append(transport_ep_info)
        
    def unregister_c2c_transport(self, transport):
        """ Removes the C2C-Transaction established on a CETPTransport, AND their binding """
        if transport in self.transport_c2c_binding:
            c2c_transaction = self.get_c2c_transaction(transport)
            self._remove_c2c_transactions(c2c_transaction)
            self._remove_connected_transport(transport)
            self._remove_c2c_transport_binding(transport)
            (ip_addr, port), proto = transport.remotepeer, transport.proto
            transport_ep_info = (ip_addr, port, proto)
            self.remote_ces_eps.append(transport_ep_info)
            c2c_transaction.set_terminated()                            # To terminate the tasks scheduled within c2c-transaction.

    def assign_cetp_h2h_layer(self, cetp_h2h):
        """ Assigns the CETP-H2H layer to a pre-established C2C layer """
        self.cetp_h2h = cetp_h2h
        self.cetp_h2h.start_h2h_consumption()
        """
        t1 = asyncio.ensure_future(self.cetp_h2h.consume_h2h_requests())                       # For consuming DNS NAPTR-responses triggered by private hosts
        self.cetp_h2h.h2h_cetp_task = t1
        self.pending_tasks.append(t1)
        """
        
    def _record_transport(self, status, transport):
        """ Records the transport connection information to which the C2C negotiation succeeded """
        transport.report_c2c_negotiation(status)
        (ip_addr, port), proto = transport.remotepeer, transport.proto
        transport_ep_info = (ip_addr, port, proto)
        
        if transport_ep_info not in self.trusted_rces_eps:
            self.trusted_rces_eps.append(transport_ep_info)
    
    
    def _trigger_cetp_h2h(self, c2c_negotiated=True):
        """ Activates the CETP-H2H layer upon completion of C2C negotiation with remote CES """
        if (not self.c2c_connectivity) and c2c_negotiated:
            self.c2c_connectivity = True                        # Indicates that atleast one active transport link towards remote CES exists.
            self._start_h2h_cetp()
            self.cetp_h2h.c2c_negotiation_status()
    
    def process_c2c(self, sstag, dstag, cetp_msg, transport):
        """ Calls corresponding C2CTransaction method, depending on whether its an ongoing or completed C2C Transaction. """
        
        # For safety: # To improve: Don't check this if len(connected_transports)==len(c2c_transaction_list) - to prevent opening an H2HTransaction as C2CTransaction.
        if self.cetpstate_mgr.has_initiated_transaction( (sstag, 0) ):
            self._logger.info(" Continue resolving c2c-transaction (SST={}, DST={})".format(sstag, 0))
            o_c2c = self.cetpstate_mgr.get_initiated_transaction( (sstag, 0) )
            result = o_c2c.continue_c2c_negotiation(cetp_msg, transport)
            (status, cetp_resp) = result
            
            if status == True:
                self.c2c_negotiated = True
                self._record_transport(status, transport)
                self._trigger_cetp_h2h()
                
            elif status == False:
                if len(cetp_resp) > 0:  transport.send_cetp(cetp_resp)
                self._logger.debug(" Close the transport endpoint towards {}.".format(self.r_cesid))
                self.unregister_c2c_transport(transport)
                transport.close()
                
            elif status == None:
                if len(cetp_resp) > 0:
                    self._logger.info(" CES-to-CES is not negotiated yet.")
                    transport.send_cetp(cetp_resp)

        elif self.cetpstate_mgr.has_established_transaction( (sstag, dstag) ):
            self._logger.debug(" CETP for a negotiated C2C transaction (SST={}, DST={})".format(sstag, dstag))
            o_c2c = self.cetpstate_mgr.get_established_transaction( (sstag, dstag) )
            o_c2c.post_c2c_negotiation(cetp_msg, transport)


    @asyncio.coroutine
    def initiate_c2c_transaction(self, transport_obj):
        """ Initiates/Continues CES-to-CES negotiation """
        c2c_transaction  = C2CTransaction.oC2CTransaction(self._loop, l_cesid=self.l_cesid, r_cesid=self.r_cesid, cetpstate_mgr=self.cetpstate_mgr, transport=transport_obj, \
                                                          policy_mgr=self.policy_mgr, proto=transport_obj.proto, ces_params=self.ces_params, cetp_security=self.cetp_security, \
                                                          interfaces=self.interfaces, c2c_layer=self, conn_table=self.conn_table)
        self._add_c2c_transport_binding(c2c_transaction, transport_obj)
        self._add_c2c_transactions(c2c_transaction)
        cetp_resp = yield from c2c_transaction.initiate_c2c_negotiation()          # Shall be a coroutine.
        
        if cetp_resp!=None:
            transport_obj.send_cetp(cetp_resp)
        

    def get_c2c_dp_connection(self):
        pass

    def remote_endpoint_malicious_history(self, r_cesid, ip_addr):
        """ Function emulating the check whether 'r_cesid' & 'ip-address' have history of misbehavior """
        return False

    """ Methods for selecting an active transport-link between CES nodes """

    def _update_transport(self, transport):
        c2c_transaction = self.get_c2c_transaction(transport)
        c2c_transaction.update_last_seen()

    def send_cetp(self, msg):
        transport = self._select_transport2()
        #print("transport: ", transport)
        if transport!=None:
            transport.send_cetp(msg)

    def _select_transport2(self):
        """ Selects the outgoing CETP-transport based on: 
            (A) good health indicator - measured by timely arrival of C2C-keepalive response. (B) Lowest-RTT (measured by timing the C2C-keepalive)              
            Other possibilities: Selection based on: 1) load balancing b/w transports; OR 2) priority field in the inbound NAPTR
        """
        try:
            for transport in self.connected_transports:
                oc2c = self.get_c2c_transaction(transport)
                if oc2c !=None:
                    if oc2c.is_transport_active():
                        return transport
                
            self._logger.info(" No link has good health.")    
            return None                         # This shall never occur due to presence of TCP failover methods below.
    
        except Exception as ex:
            self._logger.error("Exception '{}' in selecting transport".format(ex))
            return None

    def select_transport(self):
        """ Selects the outgoing CETP-transport based on: 
            (A) good health indicator - measured by timely arrival of C2C-keepalive response. (B) Lowest-RTT (measured by timing the C2C-keepalive)              
            Other possibilities: Selection based on: 1) load balancing b/w transports; OR 2) priority field in the inbound NAPTR
        """
        try:
            if len(self.transport_rtt) < len(self.connected_transports):
                # Packet sending before first keepalive & when local CES doesn't have to send keepalive
                for transport in self.connected_transports:
                    oc2c = self.get_c2c_transaction(transport)
                    if oc2c.is_transport_active():
                        return transport
                    
                self._logger.info("No link has good health.")    # This shall never occur due to presence of TCP failover methods below.
                return None
    
            elif len(self.transport_rtt) == len(self.connected_transports):
                return self.active_transport
            
        except Exception as ex:
            self._logger.error(ex)
            return None


    def report_rtt(self, transport, rtt=None, last_seen=None):
        """ Method called upon reception of C2C keepalive response """
        if rtt != None:
            self.transport_rtt[transport] = rtt
            rtt_list = []
            for trans, rtt_value in self.transport_rtt.items():
                rtt_list.append(rtt_value)
            
            rtt_list.sort()
            smallest_rtt = rtt_list[0]
            #self.last_rtt_evaluation = time.time()
            #self.smallest_rtt = smallest_rtt
            if smallest_rtt == 2**32:
                #self._logger.info("All the links have bad health ")
                return
            
            for trans, rtt_value in self.transport_rtt.items():
                if rtt_value==smallest_rtt:
                    self.active_transport = trans
                    return
        else:
            self.transport_lastseen[transport] = last_seen
        


    """  ***************    ***************    ***************    *************** ************
    ******  Functions to handle transport-layer connection establishment (& termination) *****
    ***************    ***************  ***************  ******************* ************* """
    
    @asyncio.coroutine
    def initiate_transport(self, proto, ip_addr, port, delay=0):
        """ Description """
        if proto == 'tcp' or proto=="tls":
            triggered_at = time.time()
            self._logger.info(" Initiating CETPTransport towards cesid '{}' @({}, {})".format(self.r_cesid, ip_addr, port))
            transport_ins = CETPTransports.oCESTCPTransport(self, proto, self.r_cesid, self.ces_params, remote_addr=(ip_addr, port), loop=self._loop)
            timeout = self.ces_params["c2c_establishment_t0"]
            yield from asyncio.sleep(delay)
            
            if proto == "tls":
                self.ces_certificate_path   = self.ces_params['certificate']
                self.ces_privatekey_path    = self.ces_params['private_key']
                self.ca_certificate_path    = self.ces_params['ca_certificate']

                sc = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
                sc.verify_mode = ssl.CERT_REQUIRED
                sc.load_cert_chain(self.ces_certificate_path, self.ces_privatekey_path)
                sc.load_verify_locations(self.ca_certificate_path)
                #sc.check_hostname = True
                coro = self._loop.create_connection(lambda: transport_ins, ip_addr, port, ssl=sc)
            
            elif proto == "tcp":
                coro = self._loop.create_connection(lambda: transport_ins, ip_addr, port)
            
            try:
                self.initiated_transports.append(transport_ins)
                connect_task = asyncio.ensure_future(coro)
                yield from asyncio.wait_for(connect_task, timeout)
                
                connection_time = time.time() - triggered_at
                c2c_timeout = timeout - connection_time
                self._loop.call_later(c2c_timeout, self.is_c2c_negotiated, transport_ins, ip_addr, port, proto, timeout)

            except Exception as ex:
                self._logger.info(" Exception in {} transport towards '{}'".format(proto, self.r_cesid))                  # ex.errno == 111 -- means connection RST received
                self.remote_ces_eps.remove( (ip_addr, port, proto) )
                self.unregister_connected_transport(transport_ins)
                self.register_as_unreachable_cetp(ip_addr, port, proto)


    def is_c2c_negotiated(self, transport, ip_addr, port, proto, timeout):
        """ Closes CETPTransport, if C2C-negotiation doesn't complete in 'To' """
        if transport.c2c_negotiated == False:
            self._logger.error(" C2C policies to '{}' @ {}:{} not negotiated in To='{}' sec".format(self.r_cesid, ip_addr, port, timeout))
            self.register_as_unreachable_cetp(ip_addr, port, proto, across_ces=False)
            transport.close()

    def register_as_unreachable_cetp(self, ip_addr, port, proto, across_ces=True):
        if across_ces==True:
            self.cetp_security.register_as_unreachable_cetp(ip_addr, port, proto)
        
        if not self.has_unverified_addr(ip_addr, port, proto):
            key = (ip_addr, port, proto)
            self.unverified_ep_addr.append(key)
            self._loop.call_later(30, self.unregister_unreachable_cetp, ip_addr, port, proto)

    def unregister_unreachable_cetp(self, ip_addr, port, proto):
        if self.has_unverified_addr(ip_addr, port, proto):
            key = (ip_addr, port, proto)
            self.unverified_ep_addr.remove(key)
    
    def has_unverified_addr(self, ip_addr, port, proto):
        key = (ip_addr, port, proto)
        return key in self.unverified_ep_addr
    
        
    def report_connectivity(self, transport, status=True):
        """ Triggers next function (on transport connection success) OR resouce-cleanup (on transport connection termination) """ 
        if status == True:
            self._logger.debug(" CETP Transport is connected -> Exchange C2C policies.")
            self.register_connected_transport(transport)
            asyncio.ensure_future(self.initiate_c2c_transaction(transport))
        else:
            self._logger.info(" CETP Transport is disconnected.")
            self.unregister_c2c_transport(transport)
            self.unregister_connected_transport(transport)

    def _add_connected_transport(self, transport):
        self.connected_transports.append(transport)
        
    def _remove_connected_transport(self, transport):
        if transport in self.connected_transports:
            self.connected_transports.remove(transport)
            
    def register_connected_transport(self, transport):
        """ Registers the connected CETP Transport """
        self._add_connected_transport(transport)
        print("Number of connected transports: ", len(self.connected_transports))
        if transport in self.initiated_transports:
            self.initiated_transports.remove(transport)                     # Removing the connected transport from list of initiated transports.
            
    def unregister_connected_transport(self, transport):
        """ Unregisters the CETP Transport AND launches resource cleanup if all CETPtransport are down """
        (ip_addr, port), proto = transport.remotepeer, transport.proto
        key = (ip_addr, port, proto)
        if key in self.remote_ces_eps:
            self.remote_ces_eps.remove((ip_addr, port, proto))

        self._remove_connected_transport(transport)                         # Removing the transport from list of connected transports
        if transport in self.initiated_transports:
            self.initiated_transports.remove(transport)                     # Removing the transport from list of initiated transports
        
        if (len(self.initiated_transports) ==0) & (len(self.connected_transports) ==0):
            self._logger.info(" No initiated/connected transport towards '{}' -> Closing CETP-H2H and C2C layer".format(self.r_cesid))
            self.cetp_mgr.remove_c2c_layer(self.r_cesid)
            self.cetp_mgr.remove_cetp_endpoint(self.r_cesid)
            self.resource_cleanup()


    
    """ ***********************   **********************************************
    ****     Functions to handle transport-layer failover     ***** *****
    *********************** *********************** *********************** """
    
    def report_transport_health(self, transport, healthy=True):
        """ Method called upon reception of C2C keepalive response """
        if not healthy and (self.c2c_connectivity==True):
            self.c2c_connectivity = self.ready_to_send()
            if not self.c2c_connectivity:                               # No transport link to remote CES is active
                self._suspend_h2h_cetp()                                # Suspends the H2H queue consumption
                self._logger.info("Reconnecting to {} transport endpoints: ".format(len(self.trusted_rces_eps)))
                self._reconnect_transport_eps()                         # Reconnects to the remote transport endpoints. 
                                    
        elif healthy and (self.c2c_connectivity==False):
            self.c2c_connectivity =True
            self._start_h2h_cetp()                                      # Triggers the H2H queue consumption
        
        self._logger.info("Number of active connections: '{}'".format(self.active_connections()))
    
    
    def active_connections(self):
        """ Returns count of active connections """
        try:
            print("Connected transports: ", len(self.connected_transports))
            count = 0
            for transport in self.connected_transports:
                oc2c = self.get_c2c_transaction(transport)
                if oc2c.health_report:
                    count += 1
            return count
        except Exception as ex:
            self._logger.error(ex)

    def _start_h2h_cetp(self):
        self.cetp_h2h.start_h2h_consumption()
        
    def _suspend_h2h_cetp(self):
        self.cetp_h2h.suspend_h2h_consumption()

    def ready_to_send(self):
        """ returns True, if atleast one C2C-transport link to remote CES is active """
        try:
            for transport in self.connected_transports:
                oc2c = self.get_c2c_transaction(transport)
                if oc2c.health_report:
                    return True
            return False
        except Exception as ex:
            self._logger.error(ex)
            return False

    def _reconnect_transport_eps(self):
        """ Reconnects to the previously established transport enpoints """
        for (r_ip, r_port, r_transport) in self.trusted_rces_eps:
            try:
                self._logger.info(" Initiating a new transport endpoint.")
                if not self.remote_endpoint_malicious_history(self.r_cesid, r_ip):
                    asyncio.ensure_future(self.initiate_transport(r_transport, r_ip, r_port))
            except Exception as ex:
                self._logger.warning("Exception in initiating the transport endpoint: '{}'".format(ex))

    def is_connected(self):
        """ Returns Boolean True/False, indicating presence of atleast one transport connection towards remote CES """
        return self.c2c_connectivity
    
    def set_connectivity_params(self):
        """ Set by CETPManager for inbound CETP clients """
        self.c2c_negotiated = True
        self.c2c_connectivity = True


    """ *************  *************  *************  *****************
    ********* Some functionalities exposed by the CETP-C2C API *******
    *************  *************  ************* ********* ******** """

    def close_all_transport_connections(self):
        """ Closes all connected transports to remote CES """
        conn_transports = copy.copy(self.connected_transports)
        for transport in conn_transports:
            self._close_transport_connection(transport)
        
    def _close_transport_connection(self, transport):
        """ Closes the transport connection """
        c2c_transaction = self.get_c2c_transaction(transport)
        c2c_transaction.set_terminated()
        c2c_transaction.terminate_transport()
        
    def report_evidence(self, h_sstag, h_dstag, r_hostid, r_cesid, misbehavior_evidence):
        """ Reports misbehavior evidence observed in H2H (sstag, dstag) to the remote CES """
        c2c_transaction = self.get_active_c2c_link()
        c2c_transaction.report_misbehavior_evidence(h_sstag, h_dstag, r_hostid, misbehavior_evidence)

    def block_malicious_remote_host(self, r_hostid):
        c2c_transaction = self.get_active_c2c_link()
        c2c_transaction.block_remote_host(r_hostid)
        
    def drop_connection_to_local_domain(self, l_domain):
        c2c_transaction = self.get_active_c2c_link()
        c2c_transaction.drop_connection_to_local_domain(l_domain)
        
    def get_active_c2c_link(self):
        """ Returns c2c-transaction corresponding to an active signalling transport """
        transport = self._select_transport2()
        c2c_transaction = self.get_c2c_transaction(transport)
        return c2c_transaction


    """ ***********************    ***********************    ***********************
    ******* Functions to handle interrupt (Ctrl+C) and C2C-relation closure.  *******
    ***********************    ***********************   ***********************  """
    
    def set_closure_signal(self):
        self._closure_signal = True
        
    def resource_cleanup(self):
        """ Cancels the pending tasks and deletes the object """
        self.handle_interrupt()
        self.cetp_h2h.resource_cleanup()
        del(self)

    def handle_interrupt(self):
        self.set_closure_signal()
        self.cetp_h2h.set_closure_signal()
        self.cancel_pending_tasks()

    def cancel_pending_tasks(self):
        """ Terminates asyncio-tasks in c2c-layer towards remote CES """
        for tsk in self.pending_tasks:
            if not tsk.cancelled():
                #self._logger.info("Terminating asyncio-tasks.")            
                tsk.cancel()
    
