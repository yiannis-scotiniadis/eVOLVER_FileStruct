import socket
import os.path
import shutil
import time
import pickle
import numpy as np
import numpy.matlib
import matplotlib
import scipy.signal
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.ioff()

def read_OD(vials):
    od_cal = np.genfromtxt("OD_cal.txt", delimiter=',')
    UDP_IP = "192.168.1.2"
    UDP_PORT = 5554
    MESSAGE='2125,2125,2125,2125,2125,2125,2125,2125,2125,2125,2125,2125,2125,2125,2125,2125,'  #Limit 2200
    sock = socket.socket(socket.AF_INET, # Internet
                     socket.SOCK_DGRAM) # UDP
    sock.settimeout(5)
    sock.sendto(MESSAGE, (UDP_IP, UDP_PORT))
    try:
        data, addr = sock.recvfrom(1024)
        data = data.split(',')
        for x in vials:
            try:
                data[x] = np.real(od_cal[2,x] - ((np.log10((od_cal[1,x]-od_cal[0,x])/(float(data[x]) - od_cal[0,x])-1))/od_cal[3,x]))
            except ValueError:
                print "OD Read Error"
                data = 'empty'
                break
    except socket.timeout:
        print "UDP Timeout (OD)"
        data = 'empty'
    return data

def update_temp(vials,exp_name):
    temp_cal = np.genfromtxt("temp_calibration.txt", delimiter=',')
    save_path = os.path.dirname(os.path.realpath(__file__))
    MESSAGE = ""
    for x in vials:
        file_path =  "%s/%s/temp_config/vial%d_tempconfig.txt" % (save_path,exp_name,x)
        data = np.genfromtxt(file_path, delimiter=',')
        temp_set = data[len(data)-1][1]
        temp_set = int((temp_set - temp_cal[1][x])/temp_cal[0][x])
        MESSAGE += str(temp_set)
        MESSAGE += ","

    UDP_IP = "192.168.1.2"
    UDP_PORT = 5553
    sock = socket.socket(socket.AF_INET, # Internet
                     socket.SOCK_DGRAM) # UDP
    sock.settimeout(5)
    sock.sendto(MESSAGE, (UDP_IP, UDP_PORT))
    try:
        data, addr = sock.recvfrom(1024)
        data = data.split(',')
        for x in vials:
            try:
                data[x] = (float(data[x]) * temp_cal[0][x]) + temp_cal[1][x]
            except ValueError:
                print "Temp Read Error"
                data = 'empty'
                break
    except socket.timeout:
        data = 'empty'
    return data

def fluid_command(MESSAGE, vial, elapsed_time, pump_wait, exp_name, time_on, file_write):
    UDP_IP = "192.168.1.2"
    UDP_PORT = 5552
    sock = socket.socket(socket.AF_INET, # Internet
                     socket.SOCK_DGRAM) # UDP
    sock.settimeout(5)
    sock.sendto(MESSAGE, (UDP_IP, UDP_PORT))
    save_path = os.path.dirname(os.path.realpath(__file__))
    file_path =  "%s/%s/pump_log/vial%d_pump_log.txt" % (save_path,exp_name,vial)
    if file_write == 'y':
        text_file = open(file_path,"a+")
        text_file.write("%f,%s\n" %  (elapsed_time, time_on))
        text_file.close()

def update_chemo(vials,exp_name,bolus):
    UDP_IP = "192.168.1.2"
    UDP_PORT = 5552
    sock = socket.socket(socket.AF_INET, # Internet
                     socket.SOCK_DGRAM) # UDP
    sock.settimeout(5)
    sock.sendto("clear", (UDP_IP, UDP_PORT))
    save_path = os.path.dirname(os.path.realpath(__file__))
    MESSAGE = "c,"
    for x in vials:
        file_path =  "%s/%s/chemo_config/vial%d_chemoconfig.txt" % (save_path,exp_name,x)
        data = np.genfromtxt(file_path, delimiter=',')
        chemo_set = data[len(data)-1][2]
        MESSAGE += str(int(chemo_set))
        MESSAGE += ","
    MESSAGE += str(int(bolus))
    MESSAGE += ","
    sock.sendto(MESSAGE, (UDP_IP, UDP_PORT))

def stir_rate (MESSAGE):
    UDP_IP = "192.168.1.2"
    UDP_PORT = 5551
    sock = socket.socket(socket.AF_INET, # Internet
                     socket.SOCK_DGRAM) # UDP
    sock.settimeout(5)
    sock.sendto(MESSAGE, (UDP_IP, UDP_PORT))



def parse_data(data, elapsed_time, vials, exp_name, file_name):
    save_path = os.path.dirname(os.path.realpath(__file__))
    if data == 'empty':
        print "%s Data Empty! Skipping data log..." % file_name
    else:
        for x in vials:
            file_path =  "%s/%s/%s/vial%d_%s.txt" % (save_path,exp_name,file_name,x,file_name)
            text_file = open(file_path,"a+")
            text_file.write("%f,%s\n" %  (elapsed_time, data[x]))
            text_file.close()

def initialize_exp(exp_name, vials):
    save_path = os.path.dirname(os.path.realpath(__file__))
    dir_path =  "%s/%s" % (save_path,exp_name)
    exp_continue = raw_input('Continue from existing experiment? (y/n): ')
    if exp_continue == 'n':

        start_time = time.time()
        if os.path.exists(dir_path):
            exp_overwrite = raw_input('Directory aleady exists. Overwrite with new experiment? (y/n): ')
            if exp_overwrite == 'y':
                shutil.rmtree(dir_path)
            else:
                print 'Change experiment name in custom_script.py and then restart...'
                exit() #exit

        os.makedirs("%s/OD" % dir_path)
        os.makedirs("%s/temp" % dir_path)
        os.makedirs("%s/pump_log" % dir_path)
        os.makedirs("%s/temp_config" % dir_path)
        os.makedirs("%s/ODset" % dir_path)
        os.makedirs("%s/chemo_config" % dir_path)

        OD_read = read_OD(vials)
        exp_blank = raw_input('Calibrate vials to blank? (y/n): ')
        if exp_blank == 'y':
            OD_initial = OD_read
        else:
            OD_initial = np.zeros(len(vials))

        for x in vials:
            OD_path =  "%s/OD/vial%d_OD.txt" % (dir_path,x)
            text_file = open(OD_path,"w").close()
            temp_path =  "%s/temp/vial%d_temp.txt" % (dir_path,x)
            text_file = open(temp_path,"w").close()
            tempconfig_path =  "%s/temp_config/vial%d_tempconfig.txt" % (dir_path,x)
            text_file = open(tempconfig_path,"w")
            text_file.write("Experiment: %s vial %d, %r\n" % (exp_name, x, time.strftime("%c")))
            text_file.write("0,30\n")
            text_file.close()
            pump_path =  "%s/pump_log/vial%d_pump_log.txt" % (dir_path,x)
            text_file = open(pump_path,"w")
            text_file.write("Experiment: %s vial %d, %r\n" % (exp_name, x, time.strftime("%c")))
            text_file.write("0,0\n")
            text_file.close()

            ODset_path =  "%s/ODset/vial%d_ODset.txt" % (dir_path,x)
            text_file = open(ODset_path,"w")
            text_file.write("Experiment: %s vial %d, %r\n" % (exp_name, x, time.strftime("%c")))
            text_file.write("0,0\n")
            text_file.close()

            chemoconfig_path =  "%s/chemo_config/vial%d_chemoconfig.txt" % (dir_path,x)
            text_file = open(chemoconfig_path,"w")
            #text_file.write("Experiment: %s vial %d, %r\n" % (exp_name, x, time.strftime("%c")))
            text_file.write("0,0,0\n")
            text_file.write("0,0,0\n")
            text_file.close()

    else:
        pickle_path =  "%s/%s/%s.pickle" % (save_path,exp_name,exp_name)
        with open(pickle_path) as f:
            loaded_var  = pickle.load(f)
        x = loaded_var
        start_time = x[0]
        OD_initial = x[1]
    return start_time, OD_initial

def stop_all_pumps():
    UDP_IP = "192.168.1.2"
    UDP_PORT = 5552
    sock = socket.socket(socket.AF_INET, # Internet
                     socket.SOCK_DGRAM) # UDP
    sock.settimeout(5)
    sock.sendto('clear', (UDP_IP, UDP_PORT))
    #run all pumps 0 seconds
    MESSAGE = 't,11111111111111111111111111111111,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
    sock.sendto(MESSAGE, (UDP_IP, UDP_PORT))
    print 'All Pumps Stopped!'


def save_var(exp_name, start_time, OD_initial):
    save_path = os.path.dirname(os.path.realpath(__file__))
    pickle_path =  "%s/%s/%s.pickle" % (save_path,exp_name,exp_name)
    with open(pickle_path, 'w') as f:
        pickle.dump([start_time, OD_initial], f)

if __name__ == '__main__':
    temp_data = update_temp('23423423')
    print temp_data
