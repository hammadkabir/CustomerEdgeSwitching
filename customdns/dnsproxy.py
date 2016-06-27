import asyncio
import logging

from customdns.dnsutils import *

LOGLEVEL = logging.DEBUG

class DNSProxy(asyncio.DatagramProtocol):
    def __init__(self, soa_list = [], cb_soa = None, cb_nosoa = None):
        self._logger = logging.getLogger('DNSProxy')
        self._logger.setLevel(LOGLEVEL)
        self.soa_list = soa_list
        self.cb_soa = cb_soa
        self.cb_nosoa = cb_nosoa

    def connection_made(self, transport):
        self._transport = transport
        
    def error_received(self, exc):
        addr = transport.get_extra_info('sockname')
        self._logger.warning('Error received @{0}:{1} {2}'.format(addr[0], addr[1], exc))
    
    def datagram_received(self, data, addr):
        self._logger.warning('Data received from {0}:{1} ({2} bytes) "{3}"'.format(addr[0], addr[1], len(data), data))

        try:
            query = dns.message.from_wire(data)
            fqdn = query.question[0].name.to_text()
            cb_f = self.callback_send
            if self._name_in_soa(fqdn):
                self.cb_soa(query, addr, cb_f)
            else:
                self.cb_nosoa(query, addr, cb_f)
        except Exception as e:
            self._logger.warning('Exception on message: {0}:{1} ({2} bytes) "{3}"\n{4}'.format(addr[0], addr[1], len(data), data, e))
            import traceback, sys
            print('-' * 60)
            traceback.print_exc(file=sys.stdout)
            print('-' * 60)
            return

    def callback_send(self, query, addr, response=None):
        if response is None:
            self._send_error(query, addr, dns.rcode.REFUSED)
        else:
            self._send_msg(response, addr)
            
    def _name_in_soa(self, name):
        """ Return True if the name belongs to any registered SOA """
        for soa in self.soa_list:
            if name.endswith(soa):
                return True
        return False
        
    def _send_msg(self, dnsmsg, addr):
        self._transport.sendto(dnsmsg.to_wire(), addr)

    def _send_error(self, query, addr, rcode):
        response = dns.message.make_response(query, recursion_available=True)
        response.set_rcode(rcode)
        self._send_msg(response, addr)