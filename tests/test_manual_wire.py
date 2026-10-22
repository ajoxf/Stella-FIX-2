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
        def sendall(self, raw):
            fields=parse_fix_message(raw.decode('ascii'));self.sent.append(fields)
            if fields['35'] not in ('A','D'): return
            self.seq+=1
            reply=[('35','A' if fields['35']=='A' else '8'),('34',str(self.seq)),('49','TT'),('56',fields['49'])]
            if fields['35']=='D':
                reply += [('11',fields['11']),('37','TT-ORDER'),('39','2'),('150','2'),('17','FILL1'),('14','1'),('151','0'),('31',fields['44']),('32','1'),('6',fields['44'])]
            self.frames.append(encode_fix_message(reply))
            if fields['35']=='A':      # TT: "Recovery is complete" after the logon
                self.seq+=1
                self.frames.append(encode_fix_message([('35','B'),('34',str(self.seq)),('49','TT'),('56',fields['49']),('148','Recovery Complete'),('33','1'),('58','Recovery is complete')]))
        def recv(self, _):
            if self.frames:return self.frames.pop(0)
            time.sleep(.005);raise socket.timeout()
    peers=[]
    def connect(*args,**kwargs):
        peer=Peer();peers.append(peer);return peer
    monkeypatch.setattr(socket,'create_connection',connect)
    monkeypatch.setenv('WIRE_PASSWORD','test-secret')
    v=VenueConfig(name='UAT',host='example',port=1,fix_version='FIX.4.2',sender_comp_id='CLIENT',target_comp_id='TT',password_env='WIRE_PASSWORD',account='TEST',use_tls=False)
    gateway=FixGateway(v)
    gateway.start()
    try:
        deadline=time.monotonic()+2
        while gateway.state().value!='LOGGED_ON' and time.monotonic()<deadline:time.sleep(.01)
        assert gateway.state().value=='LOGGED_ON'
        gateway.terminal.watch['1']={'security_id':'1','symbol':'ES','description':'Test contract','exchange':'CME','tick_size':'0.25','parameters':{},'display_factor':'1'}
        preview=gateway.terminal.preview({'security_id':'1','side':'BUY','quantity':'1','price':'-0.25','order_type':'LIMIT','tif':'DAY'})
        result=gateway.terminal.submit({'token':preview['token'],'confirmed':True})
        deadline=time.monotonic()+2
        while gateway.terminal.orders[result['order_id']]['status']!='FILLED' and time.monotonic()<deadline:time.sleep(.01)
        assert gateway.terminal.orders[result['order_id']]['status']=='FILLED'
        order=next(f for p in peers for f in p.sent if f['35']=='D')
        assert order['44']=='-0.25' and order['48']=='1' and order['1028']=='Y'
        assert len(gateway.terminal.snapshot()['fills'])==1
    finally:
        gateway.stop()
