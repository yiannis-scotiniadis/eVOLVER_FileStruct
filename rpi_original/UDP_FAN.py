#!/usr/bin/python

import SocketServer
import struct
import socket
import time
import os.path

class MatlabUDPHandler(SocketServer.BaseRequestHandler):

    def handle(self):
        data = self.request[0]
        socket = self.request[1]
#        print "%s wrote:" % self.client_address[0]


        if data == 'clear':
                log = open(completeName, "w")
        else:
                log = open(completeName,"w")
                log.write("%s\n" % data)
#        print data
        log.close()

        #log = open(completeName,"r")
        #values = log.read()
        #log.close()

#        socket.sendto("Message Recieved", self.client_address)

print "Setting up ports!"
save_path = '/home/pi/eVOLVER_UDP/'
completeName = os.path.join(save_path, "fan_config.txt")
log = open(completeName, "w")
BEAGLE, PORT =  "0.0.0.0", 5551
server = SocketServer.UDPServer((BEAGLE, PORT), MatlabUDPHandler)
print "Ready to Listen!"
while (1):
        server.handle_request()
        #server.serve_forever()

