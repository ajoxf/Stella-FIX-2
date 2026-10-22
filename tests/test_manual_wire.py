"""Exercise reviewed orders through the real encoder/receiver, with a fake peer."""
import socket
import time

from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway, encode_fix_message, parse_fix_message


def test_review_send_and_execution_use_native_fix_wire(monkeypatch):
    class Peer:
        def __init__(self): self.frames=[]; self.sent=[]; self.seq=0
        def settimeout(self, _): pass
        def close(self): pass
        def frame(self, fields):
            self.seq += 1
            self.frames.append(encode_fix_message([('35', fields[0][1]), ('34', str(self.seq)),
                                                   ('49', 'STELLAR'), ('56', self.comp)] + fields[1:]))
        def sendall(self, raw):
            fields=parse_fix_message(raw.decode('ascii'));self.sent.append(fields)
            self.comp = fields['49']
            if fields['35']=='A':
                self.frame([('35','A'),('98','0'),('108','30')])
            elif fields['35']=='D':
                # Stellar: the acknowledgement, then the trade — which carries
                # no ClOrdID, only Stellar's OrderID, with AvgPx "0".
                self.frame([('35','8'),('11',fields['11']),('37','76253459'),('17','ORD:1'),
                            ('150','0'),('39','0'),('14','0'),('151','1')])
                self.frame([('35','8'),('37','76253459'),('17','13297209'),('527','12059714'),
                            ('150','F'),('39','2'),('14','0'),('151','0'),('31',fields['44']),
                            ('32','1'),('6','0'),('442','1')])
        def recv(self, _):
            if self.frames:return self.frames.pop(0)
            time.sleep(.005);raise socket.timeout()
    peers=[]
    def connect(*args,**kwargs):
        peer=Peer();peers.append(peer);return peer
    monkeypatch.setattr(socket,'create_connection',connect)
    monkeypatch.setenv('WIRE_PASSWORD','test-secret')
    v=VenueConfig(name='UAT',host='example',port=1,fix_version='FIX.4.4',sender_comp_id='CLIENT',
                  target_comp_id='STELLAR',password_env='WIRE_PASSWORD',account='TEST',
                  username='USER',use_tls=False)
    gateway=FixGateway(v)
    gateway.start()
    try:
        deadline=time.monotonic()+2
        while gateway.state().value!='LOGGED_ON' and time.monotonic()<deadline:time.sleep(.01)
        assert gateway.state().value=='LOGGED_ON'
        gateway.terminal.watch['1']={'security_id':'1','symbol':'ES','description':'Test contract',
                                     'exchange':'XCME','cfi':'FXXXXX','maturity':'202612',
                                     'tick_size':'0.25','parameters':{},'display_factor':'1'}
        preview=gateway.terminal.preview({'security_id':'1','side':'BUY','quantity':'1','price':'-0.25','order_type':'LIMIT','tif':'DAY'})
        result=gateway.terminal.submit({'token':preview['token'],'confirmed':True})
        deadline=time.monotonic()+2
        while gateway.terminal.orders[result['order_id']]['status']!='FILLED' and time.monotonic()<deadline:time.sleep(.01)
        order_state=gateway.terminal.orders[result['order_id']]
        assert order_state['status']=='FILLED' and order_state['avg_price']==-0.25
        order=next(f for p in peers for f in p.sent if f['35']=='D')
        assert order['8']=='FIX.4.4' and order['56']=='STELLAR'
        assert order['44']=='-0.25' and order['55']=='ES' and order['461']=='FXXXXX'
        assert order['200']=='202612' and '48' not in order and '1028' not in order
        assert len(gateway.terminal.snapshot()['fills'])==1
    finally:
        gateway.stop()
