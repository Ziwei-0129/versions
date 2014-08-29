# -*- coding: utf-8 -*-
"""
ioHub
.. file: ioHub/net.py

Copyright (C) 2012-2013 iSolver Software Solutions
Distributed under the terms of the GNU General Public License (GPL version 3 or any later version).

.. moduleauthor:: Sol Simpson <sol@isolver-software.com> + contributors, please see credits section of documentation.
.. fileauthor:: Sol Simpson <sol@isolver-software.com>
"""

from gevent import socket,sleep,Greenlet
import msgpack
import struct
from weakref import proxy
from psychopy.iohub.util import NumPyRingBuffer as RingBuffer
from psychopy.iohub import Computer
getTime=Computer.getTime

MAX_PACKET_SIZE=64*1024

class SocketConnection(object):
    def __init__(self,local_host=None,local_port=None,remote_host=None,remote_port=None,rcvBufferLength=1492, broadcast=False, blocking=0, timeout=0):
        self._local_port= local_port
        self._local_host = local_host
        self._remote_host= remote_host
        self._remote_port = remote_port
        self._rcvBufferLength=rcvBufferLength
        self.lastAddress=None
        self.sock=None
        self.initSocket()

        self.coder=msgpack
        self.packer=msgpack.Packer()
        self.unpacker=msgpack.Unpacker(use_list=True)
        self.pack=self.packer.pack
        self.feed=self.unpacker.feed
        self.unpack=self.unpacker.unpack

    def initSocket(self,broadcast=False,blocking=0, timeout=0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        if broadcast is True:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack('@i', 1))

        if blocking is not 0:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self.sock.settimeout(timeout)
        self.sock.setblocking(blocking)

    def sendTo(self,data,address=None):
        if address is None:
            address=self._remote_host, self._remote_port
        d=self.pack(data)
        byte_count=len(d)
        self.sock.sendto(d,address)
        return byte_count

    def receive(self):
        try:
            data, address = self.sock.recvfrom(self._rcvBufferLength)
            self.lastAddress=address
            self.feed(data)
            result=self.unpack()
            if result[0] == 'IOHUB_MULTIPACKET_RESPONSE':
                num_packets=result[1]

                for p in xrange(num_packets-1):
                    data, address = self.sock.recvfrom(self._rcvBufferLength)
                    self.feed(data)

                data, address = self.sock.recvfrom(self._rcvBufferLength)
                self.feed(data)
                result=self.unpack()
            return result,address
        except Exception as e:
            print "Error during SocketConnection.receive: ",e
            raise e

    def close(self):
        self.sock.close()


class UDPClientConnection(SocketConnection):
    def __init__(self,remote_host='127.0.0.1',remote_port=9000,rcvBufferLength = MAX_PACKET_SIZE,broadcast=False,blocking=1, timeout=1):
        SocketConnection.__init__(self,remote_host=remote_host,remote_port=remote_port,rcvBufferLength=rcvBufferLength,broadcast=broadcast,blocking=blocking, timeout=timeout)
    def initSocket(self,**kwargs):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, MAX_PACKET_SIZE)

##### TIME SYNC CLASS ######
 
class ioHubTimeSyncConnection(UDPClientConnection):
    """
    A special purpose version of the UDPClientConnection class which has the only
    job of sending and receiving time sync rmessage requests and responses with a remote
    ioHub Server instance.
    """
    def __init__(self,remote_address):        
        self.remote_iohub_address=tuple(remote_address)
        
        UDPClientConnection.__init__(self,remote_host=self.remote_iohub_address[0],remote_port=self.remote_iohub_address[1],rcvBufferLength=MAX_PACKET_SIZE,broadcast=False,blocking=1, timeout=1)

        self.sync_batch_size=5
    
    def sync(self):
        sync_count=self.sync_batch_size
        sync_data=['SYNC_REQ',]

        feed=self.feed
        unpack=self.unpack
        pack=self.pack

        recvfrom=self.sock.recvfrom
        rcvBufferLength=self._rcvBufferLength

        remote_address=self.remote_iohub_address
        sendto=self.sock.sendto
        
        min_delay=1000.0
        min_local_time=0.0
        min_remote_time=0.0
                
        for s in xrange(sync_count):
            # send sync request
            sync_start=Computer.currentSec()
            sendto(pack(sync_data),remote_address)                    
            sync_start2=Computer.currentSec()
            
            # get reply
            feed(recvfrom(rcvBufferLength)[0])
            sync_rep,remote_time=unpack()
            sync_end=Computer.currentSec()
            rtt=sync_end-(sync_start+sync_start2)/2.0

            old_delay=min_delay
            min_delay=min(min_delay,rtt)
            
            if old_delay!=min_delay:
                min_local_time=(sync_end+sync_start)/2.0
                min_remote_time=remote_time
                
        return min_delay, min_local_time, min_remote_time

class ioHubTimeGreenSyncManager(Greenlet):
    """
    The time syncronization manager class used within an ioHub Server when a
    ioHubRemoteEventSubscriber device is running. The time syncronization manager
    monitors and calculates the ongoing offset and drift between the local ioHub
    instance and a remote ioHub instance running on another computer that is 
    publishing events that are being received by the local ioHubRemoteEventSubscriber.
    """
    
    def __init__(self,remote_address,sync_state_target):
        Greenlet.__init__(self)
        self.initial_sync_interval=0.2
        self._remote_address=remote_address
        self._sync_socket=ioHubTimeSyncConnection(remote_address)
        self.sync_state_target=proxy(sync_state_target)

    def _run(self):
        self._running=True

        self._sync(False)
        self._sync(False)
        while self._running is True:
            sleep(self.initial_sync_interval)
            self._sync()
        self._close()
        
    def _sync(self,calc_drift_and_offset=True):
        if self._sync_socket:
            min_delay, min_local_time, min_remote_time=self._sync_socket.sync()     
            sync_state_target=self.sync_state_target
            sync_state_target.RTTs.append(min_delay)
            sync_state_target.L_times.append(min_local_time)
            sync_state_target.R_times.append(min_remote_time)
            
            if calc_drift_and_offset is True:
                l1=sync_state_target.L_times[-2]
                l2=sync_state_target.L_times[-1]
                r1=sync_state_target.R_times[-2]
                r2=sync_state_target.R_times[-1]
                self.sync_state_target.drifts.append((r2-r1)/(l2-l1))
    
                l=sync_state_target.L_times[-1]
                r=sync_state_target.R_times[-1]
                self.sync_state_target.offsets=(r-l)

    def _close(self):           
        if self._sync_socket:        
            self._running=False
            self._sync_socket.close()
            self._sync_socket=None
            
    def __del__(self):
        self._close()   


class ioHubTimeSyncManager(object):
    def __init__(self,remote_address,sync_state_target):
        self.initial_sync_interval=0.2
        self._remote_address=remote_address
        self._sync_socket=ioHubTimeSyncConnection(remote_address)
        self.sync_state_target=proxy(sync_state_target)
        
    def sync(self,calc_drift_and_offset=True):
        if self._sync_socket:
            min_delay, min_local_time, min_remote_time=self._sync_socket.sync()     
            sync_state_target=self.sync_state_target
            sync_state_target.RTTs.append(min_delay)
            sync_state_target.L_times.append(min_local_time)
            sync_state_target.R_times.append(min_remote_time)
            
            if calc_drift_and_offset is True:
                l1=sync_state_target.L_times[-2]
                l2=sync_state_target.L_times[-1]
                r1=sync_state_target.R_times[-2]
                r2=sync_state_target.R_times[-1]
                self.sync_state_target.drifts.append((r2-r1)/(l2-l1))
    
                l=sync_state_target.L_times[-1]
                r=sync_state_target.R_times[-1]
                self.sync_state_target.offsets=(r-l)

    def close(self):           
        if self._sync_socket:        
            self._running=False
            self._sync_socket.close()
            self._sync_socket=None
            
    def __del__(self):
        self.close()   

class TimeSyncState(object):
    """
    Container class used by an ioHubSyncManager to hold the data necessary to
    calculate the current time base offset and drift between an ioHub Server
    and a ioHubRemoteEventSubscriber client.
    """
    RTTs=RingBuffer(10)
    L_times=RingBuffer(10)
    R_times=RingBuffer(10)
    drifts=RingBuffer(20)
    offsets=RingBuffer(20)
    
    def getDrift(self):
        """
        Current drift between two time bases.
        """
        return self.drifts.mean()
        
    def getOffset(self):
        """
        Current offset between two time bases.
        """
        return self.offsets.mean()

    def getAccuracy(self):
        """
        Current accuracy of the time syncronization, as calculated as the 
        average of the last 10 round trip time sync request - response delays
        divided by two.
        """
        return self.RTTs.mean()/2.0
        
    def local2RemoteTime(self,local_time=None):
        """
        Converts a local time (sec.msec format) to the corresponding remote
        computer time, using the current offset and drift measures.
        """        
        if local_time is None:
            local_time=Computer.currentSec()
        return self.getDrift()*local_time+self.getOffset()
          
    def remote2LocalTime(self,remote_time):
        """
        Converts a remote computer time (sec.msec format) to the corresponding local
        time, using the current offset and drift measures.       
        """
        return (remote_time-self.getOffset())/self.getDrift()