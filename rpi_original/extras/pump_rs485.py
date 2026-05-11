import serial
import time

#Setup RS485 Connection
ser = serial.Serial(port = "/dev/ttyAMA0", baudrate=9600, timeout = 1)
ser.flushInput()



while True:
	ser.write('st0111111011111111,0,5, !')
	print 'st0111111011111111,0,5, !'	
	received = ser.readline()
	print "Completed Command:" + received	

	time.sleep(7)

	ser.write('st !')
        received = ser.readline()
        print "Completed Command:" + received

	time.sleep(5)

        ser.write('st1111111011111111,0,5, !')
        print 'st1111111011111111,0,5, !'
        received = ser.readline()
        print "Completed Command:" + received

        time.sleep(7)

        ser.write('st !')
        received = ser.readline()
        print "Completed Command:" + received

        time.sleep(5)


#while True:
#        if( usart.inWaiting()>0 ):
#                received = usart.read()
#                inData += received
#                print inData
#                if (received == '!'):
#                        time.sleep(.1)
#                        print inData
#                        time.sleep(.1)
#                        usart.write('turb4095,4095,4095,4095,4095,4095,4095,4095,4095,$
#                        inData = ""

