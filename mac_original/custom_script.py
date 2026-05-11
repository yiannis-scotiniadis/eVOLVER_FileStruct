import eVOLVER_module
import numpy as np
import os.path


# This code will operate eVOLVER as a pulsatile chemostat, with user-specified temperature, stir rate, dilution rate, dilution start time, and dilution start OD.
# The code waits for the specified time, permits the culture to grow up to the specified starting OD, then initiates repeated dilutions according to specified dilution rate.
# Last Updated: Chris Mancuso 07/24/18

def choose_name():
    ##### Sets name of experiment folder, make new name for each experiment, otherwise files will be overwritten

    ##### USER DEFINED FIELDS #####

    exp_name = 'N_expt_6-29-19'
    return exp_name

    ##### END OF USER DEFINED FIELDS #####

def test (OD_data, temp_data, vials, elapsed_time, exp_name):

    ##### USER DEFINED VARIABLES #####

    temp_input = 34 #degrees C, can be scalar or 16-value numpy array
    stir_input = 11 #try 8,10,12; see paper for rpm conversion, can be scalar or 16-value numpy array

    # lower_thresh = np.array([0.2] * len(vials))
    lower_thresh = np.array([0.4,0.4,0.4,0.4,0.4,0.4,0.4,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2,0.2])
    upper_thresh = np.array([99999,99999,99999,99999,99999,99999,99999,0.5,99999,99999,99999,99999,99999,99999,99999,99999])

    ##### END OF USER DEFINED VARIABLES #####


    ##### Calibration Values:
    # Be sure to check that OD_cal.txt and temp_calibration.txt are up to date
    # Additional calibration values below, remain the same between experiments

    time_out = 5 #(sec) additional amount of time to run efflux pump
    pump_wait = 15 # (min) minimum amount of time to wait between pump events
    control = np.power(2,range(0,32)) #vial addresses
    flow_rate = np.array([0.95,1.1,0.975,0.85,0.95,1.05,1.05,1.05,1.025,1.125,1.0,1.0,1.05,1.15,1.1,1.025]) #ml/sec, paste from pump calibration
    volume =  25 #mL, determined by straw length

    save_path = os.path.dirname(os.path.realpath(__file__)) #save path

    ##### End of Calibration Values


    ##### Turbidostat Control Code Below #####


    for x in vials: #main loop through each vial

        # Update temperature configuration files for each vial
        tempconfig_path =  "%s/%s/temp_config/vial%d_tempconfig.txt" % (save_path,exp_name,x)
        temp_config = np.genfromtxt(tempconfig_path, delimiter=',')

        if (len(temp_config) is 2): #set temp at the beginning of the experiment, can clone for subsequent temp changes
                if np.isscalar(temp_input):
                    temp_val = temp_input
                else:
                    temp_val = temp_input[x]

                text_file = open(tempconfig_path,"a+")
                text_file.write("%f,%s\n" %  (elapsed_time, temp_val))
                text_file.close()

        # Update turbidostat configuration files for each vial

        # initialize OD and find OD path

        ODset_path =  "%s/%s/ODset/vial%d_ODset.txt" % (save_path,exp_name,x)
        data = np.genfromtxt(ODset_path, delimiter=',')
        ODset = data[len(data)-1][1]


        OD_path =  "%s/%s/OD/vial%d_OD.txt" % (save_path,exp_name,x)
        data = np.genfromtxt(OD_path, delimiter=',')
        average_OD = 0

        # Determine whether turbidostat dilutions are needed

        if len(data) > 7:
            for n in range(1,6):
                average_OD = average_OD + (data[len(data)-n][1]/5)


            if (average_OD > upper_thresh[x]) and (ODset != lower_thresh[x]):
                text_file = open(ODset_path,"a+")
                text_file.write("%f,%s\n" %  (elapsed_time, lower_thresh[x]))
                text_file.close()
                ODset = lower_thresh[x]

            if (average_OD < (lower_thresh[x]+(upper_thresh[x] - lower_thresh[x])/2)) and (ODset != upper_thresh[x]):
                text_file = open(ODset_path,"a+")
                text_file.write("%f,%s\n" %  (elapsed_time, upper_thresh[x]))
                text_file.close()
                ODset = upper_thresh[x]

            if average_OD > ODset:

                time_in = - (np.log(lower_thresh[x]/average_OD)*volume)/flow_rate[x]

                if time_in > 20:
                    time_in = 20

                save_path = os.path.dirname(os.path.realpath(__file__))
                file_path =  "%s/%s/pump_log/vial%d_pump_log.txt" % (save_path,exp_name,x)
                data = np.genfromtxt(file_path, delimiter=',')
                last_pump = data[len(data)-1][0]
                if ((elapsed_time- last_pump)*60) >= pump_wait:
                    MESSAGE = "%s,0,%d," % ("{0:b}".format(control[x]+control[x+16]) , time_in)
                    eVOLVER_module.fluid_command(MESSAGE, x, elapsed_time, pump_wait *60, exp_name, time_in, 'y')
                    MESSAGE = "%s,0,%d," % ("{0:b}".format(control[x+16]) , time_out)
                    eVOLVER_module.fluid_command(MESSAGE, x, elapsed_time, 0, exp_name, time_out,'n')

    #Update stir rate for all vials
    if np.isscalar(stir_input):
        stir_val = str(stir_input)
        STIR_MESSAGE = (stir_val + ',')*16
    else:
        stir_val = np.array2string(stir_input, separator = ',') + ','
        stir_val = stir_val.replace(' ','')
        stir_val = stir_val.replace('[','')
        stir_val = stir_val.replace(']','')
        STIR_MESSAGE = stir_val

    eVOLVER_module.stir_rate(STIR_MESSAGE)

    # end of test() fxn
