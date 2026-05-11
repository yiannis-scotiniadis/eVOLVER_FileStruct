from flask import Flask, request, jsonify, abort
import json, thread, serial
from time import sleep

#Setup RS485 Connection
ser = serial.Serial(port = "/dev/ttyAMA0", baudrate=9600, timeout = 2)
ser.flushInput()
ser.close()

#Setup Flask
app = Flask(__name__)
app.debug = False

#Initialize experimental parameters
param = {}

#Initialize dictionary for config/data storage
config = {"parameter": "config"}
data = {"parameter": "data"}

#ConfigtoArduino adds headers and endings to the strings to send config to correct Arduino 
def ConfigtoArduino(key, header, ending, method):
	myList = config[key]
	ser.open()
	ser.flushInput()
	if method == 'all':
		myString = ','.join(map(str, myList)) 
		output = header + myString + ',' + ending
		ser.write(output)
	if method == 'indiv':
		for x in myList.split(','):
			output = header + x  + ending
			ser.write(output)
	print output
	

#DatafromArduino adds list of current values to data dictionary
def DatafromArduino(key, header, ending):
	try:
		received = ser.readline()
		print received
		if (received[0:4] == header) and (received[-3:] == ending):
			dataList = [int(s) for s in received[4:-4].split(",")]
			data[key] = dataList
			sleep(.1)
		else:
			data[key] = 'NaN'
	except ValueError:
		data[key] = 'NaN'	
	ser.close()

#Define parameters initializes the parameters and assigns addresses to query them
@app.route("/DefineParameters/", methods=['GET', 'POST'])
def DefineParameters():
        if request.method == 'POST':
		param.clear()
                param.update(request.json)
                return ""

        elif request.method == 'GET':
                print("redo as a POST request please")
                return ""

#Update Raspberry Configuration File
@app.route("/update/", methods=['GET', 'POST'])
def update():
    	if request.method == 'POST':
        	config.update(request.json)
        	return ""

	elif request.method == 'GET':
        	print("redo as a POST request please")
        	return ""

#Relay data to client
@app.route("/query/", methods=['GET'])
def query():
	if request.method == 'GET':
    	
		return jsonify(**data)

#Push updates to Arduino and grab data from Arduino
@app.route("/pushtoArduino/")
def pushtoArduino():
	if request.method == 'GET':
		for key, value in param.iteritems():
			ConfigtoArduino(key, value[0], ' !', value[2])
			DatafromArduino(key, value[1], 'end')

#		ConfigtoArduino('stir', 'zv', ' !')
#		ConfigtoArduino('temp', 'xr', ' !')
#		DatafromArduino('temp', 'temp', 'end')
#		ConfigtoArduino('OD', 'we', ' !')
#		DatafromArduino('OD', 'turb', 'end')

		return ""
	

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80)

