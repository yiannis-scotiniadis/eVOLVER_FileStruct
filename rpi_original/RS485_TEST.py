import serial
import time
port = '/dev/ttyAMA0'
inData = ""
usart = serial.Serial(port,9600)
usart.flushInput()

print 'serial test: BaudRate = 9600'
usart.write('please enter the character:\n')

#while True:
#	usart.write('Got it!')
#	print 'Got it!'
#	time.sleep(1)

while True:
        if( usart.inWaiting()>0 ):
                received = usart.read()
                inData += received
                print inData
                if (received == '!'):
			time.sleep(.1)
                        print inData
			time.sleep(.1)
                        usart.write('turb4095,4095,4095,4095,4095,4095,4095,4095,4095,4095,4095,4095,4095,4095,4095,4095 !')
                        inData = ""
